"""出口动作：发弹幕 + 语音播报。

发弹幕是这个系统里唯一"会留下痕迹、会被风控"的动作，所以全部收敛到这里，
并且统一走 DanmakuBudget 配额，任何调用方都绕不过去。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from bilibili_api.live import LiveRoom
from bilibili_api.utils.danmaku import Danmaku

from ..config import GuardConfig, SpeechConfig
from .response import is_risk, ok_result

log = logging.getLogger("biliauto.actions")


class DanmakuSender:
    def __init__(
        self,
        room: LiveRoom | None,
        guard: GuardConfig,
        budget,
        store,
        dry_run: bool = False,
    ) -> None:
        self.room = room
        self.guard = guard
        self.budget = budget
        self.store = store
        self.dry_run = dry_run

    def clamp(self, text: str) -> str:
        text = " ".join((text or "").split())
        if len(text) > self.guard.max_danmaku_len:
            text = text[: self.guard.max_danmaku_len]
        return text

    async def send(self, text: str, reply_mid: int | None = None) -> tuple[bool, str]:
        text = self.clamp(text)
        if not text:
            return False, "empty"

        ok, reason = self.budget.check(reply_mid or 0)
        if not ok:
            log.debug("配额拦截：%s", reason)
            return False, reason

        if self.dry_run or self.room is None:
            self.budget.note_sent(reply_mid or 0)
            log.info("[DRY-RUN] 本应发弹幕：%s", text)
            return True, "dry_run"

        try:
            resp = await self.room.send_danmaku(
                Danmaku(text=text), reply_mid=reply_mid
            )
        except Exception as e:
            from .response import error_of

            code, msg = error_of(e)
            self.budget.note_failed()
            if is_risk(code):
                log.warning("发弹幕触发风控（%s），冷却拉长", code)
            else:
                log.warning("发弹幕异常：%s", msg)
            return False, f"exception:{msg}"

        # send_danmaku 成功时返回 data 字段（不含 code），没抛异常即成功
        ok2, detail = ok_result(resp)
        if ok2:
            self.budget.note_sent(reply_mid or 0)
            self.store.incr_usage("danmaku_sent")
            log.info("已发送弹幕：%s", text)
            return True, "ok"

        self.budget.note_failed()
        log.warning("发弹幕被拒 %s", detail)
        return False, detail


class Speaker:
    """edge-tts 合成 + 按需播放。

    file 模式：只落 mp3，交给 OBS 媒体源/播放器。
    play 模式：转 wav 后用 winsound 播到系统默认输出设备
              （配合 VB-Audio Cable 即可让 OBS 直接"听到"AI 说话）。
    """

    def __init__(self, cfg: SpeechConfig) -> None:
        self.cfg = cfg
        self.cfg.out_dir.mkdir(parents=True, exist_ok=True)
        self._queue: asyncio.Queue[tuple[str, Path]] = asyncio.Queue(maxsize=4)
        self._player: asyncio.Task | None = None
        self._ffmpeg = self._find_ffmpeg()
        self._mixer = None

    @staticmethod
    def _find_ffmpeg() -> str | None:
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None

    async def say(self, text: str) -> Path | None:
        if not self.cfg.enabled or not text.strip():
            return None
        text = text.strip()[: self.cfg.max_chars]

        path = self.cfg.out_dir / f"say_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}.mp3"
        try:
            import edge_tts

            communicate = edge_tts.Communicate(
                text,
                self.cfg.voice,
                rate=self.cfg.rate,
                volume=self.cfg.volume,
                pitch=self.cfg.pitch,
            )
            await communicate.save(str(path))
        except Exception as e:
            log.warning("TTS 合成失败：%s", e)
            return None

        if self.cfg.mix_enabled:
            ok, why = self.ensure_mixer()
            if not ok:
                log.warning("混音不可用，本条语音跳过：%s", why)
                return path
            try:
                data, sr = self._decode_wav(path)
                self._mixer.feed(data, sr)
            except Exception as e:
                log.warning("混音喂数据失败：%s", e)
            return path

        if self.cfg.backend == "play":
            # 队列满了就丢掉最旧的：直播里宁可少说一句，也别卡成延迟弹幕
            if self._queue.full():
                try:
                    _, old = self._queue.get_nowait()
                    self._queue.task_done()
                    old.unlink(missing_ok=True)
                except Exception:
                    pass
            try:
                self._queue.put_nowait((text, path))
            except asyncio.QueueFull:
                pass
            if self._player is None or self._player.done():
                self._player = asyncio.create_task(self._play_loop())
        return path

    async def _play_loop(self) -> None:
        while True:
            try:
                text, path = await asyncio.wait_for(self._queue.get(), timeout=300)
            except asyncio.TimeoutError:
                return
            try:
                await asyncio.to_thread(self._play_blocking, path)
            except Exception as e:
                log.debug("播放失败：%s", e)
            finally:
                self._queue.task_done()
                path.unlink(missing_ok=True)

    def _play_blocking(self, mp3: Path) -> None:
        wav = mp3.with_suffix(".wav")
        if self._ffmpeg:
            subprocess.run(
                [self._ffmpeg, "-y", "-loglevel", "error", "-i", str(mp3), str(wav)],
                check=True,
                capture_output=True,
            )
        else:
            raise RuntimeError("缺少 ffmpeg，无法转 wav 播放")

        try:
            # 指定了输出设备就走 sounddevice：可以精确播到虚拟声卡（CABLE Input），
            # 不必改系统默认输出。—— 这是把语音送进 OBS / 直播姬的关键
            if self.cfg.device is not None and self._play_to_device(wav):
                return
            import winsound  # 仅 Windows

            winsound.PlaySound(str(wav), winsound.SND_FILENAME)
        finally:
            wav.unlink(missing_ok=True)

    def _play_to_device(self, wav: Path) -> bool:
        try:
            import wave

            import numpy as np
            import sounddevice as sd

            with wave.open(str(wav), "rb") as w:
                sr = w.getframerate()
                ch = w.getnchannels()
                width = w.getsampwidth()
                frames = w.readframes(w.getnframes())
            if width != 2:
                log.debug("只支持 16bit wav，退回默认设备")
                return False

            data = np.frombuffer(frames, dtype=np.int16)
            if ch > 1:
                data = data.reshape(-1, ch)
            sd.play(data, sr, device=self.cfg.device)
            sd.wait()
            return True
        except Exception as e:
            log.debug("指定设备播放失败，退回默认设备：%s", e)
            return False

    def ensure_mixer(self) -> tuple[bool, str]:
        """按需启动混音器（麦克风 + TTS 混成一路）。"""
        if not self.cfg.mix_enabled:
            return False, "混音未开启"
        if self._mixer is not None and self._mixer.running:
            return True, "ok"
        from ..audio import AudioMixer

        self._mixer = AudioMixer(
            input_device=self.cfg.mix_input,
            output_device=(
                self.cfg.mix_output
                if self.cfg.mix_output is not None
                else self.cfg.device
            ),
            mic_gain=self.cfg.mix_mic_gain,
            tts_gain=self.cfg.mix_tts_gain,
            duck=self.cfg.mix_duck,
        )
        ok, msg = self._mixer.start()
        if ok:
            log.info("混音已开启：麦克风 + TTS -> 输出设备")
        return ok, msg

    def stop_mixer(self) -> None:
        if self._mixer is not None:
            self._mixer.stop()

    def mixer_snapshot(self) -> dict:
        if self._mixer is None:
            return {"running": False, "enabled": self.cfg.mix_enabled}
        snap = self._mixer.snapshot()
        snap["enabled"] = self.cfg.mix_enabled
        return snap

    def _decode_wav(self, mp3: Path):
        """mp3 -> (float32 numpy, samplerate)。混音要的是裸样本。"""
        import numpy as np
        import wave

        wav = mp3.with_suffix(".wav")
        if self._ffmpeg:
            subprocess.run(
                [self._ffmpeg, "-y", "-loglevel", "error", "-i", str(mp3), str(wav)],
                check=True,
                capture_output=True,
            )
        try:
            with wave.open(str(wav), "rb") as w:
                sr = w.getframerate()
                ch = w.getnchannels()
                width = w.getsampwidth()
                frames = w.readframes(w.getnframes())
            if width != 2:
                raise RuntimeError("只支持 16bit wav")
            data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            if ch > 1:
                data = data.reshape(-1, ch).mean(axis=1)
            return data, sr
        finally:
            wav.unlink(missing_ok=True)

    async def close(self) -> None:
        self.stop_mixer()
        if self._player and not self._player.done():
            self._player.cancel()
            try:
                await self._player
            except (asyncio.CancelledError, Exception):
                pass
