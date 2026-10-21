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
            self.budget.note_failed()
            log.warning("发弹幕异常：%s", e)
            return False, f"exception:{e}"

        code = resp.get("code") if isinstance(resp, dict) else None
        if code == 0:
            self.budget.note_sent(reply_mid or 0)
            self.store.incr_usage("danmaku_sent")
            log.info("已发送弹幕：%s", text)
            return True, "ok"

        msg = resp.get("message") if isinstance(resp, dict) else str(resp)
        # 常见：-101 未登录、10030 弹幕过长、-412 被风控
        self.budget.note_failed()
        log.warning("发弹幕被拒 code=%s msg=%s", code, msg)
        return False, f"code:{code}"


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
                text, self.cfg.voice, rate=self.cfg.rate, volume=self.cfg.volume
            )
            await communicate.save(str(path))
        except Exception as e:
            log.warning("TTS 合成失败：%s", e)
            return None

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
        import winsound  # 仅 Windows

        wav = mp3.with_suffix(".wav")
        if self._ffmpeg:
            subprocess.run(
                [self._ffmpeg, "-y", "-loglevel", "error", "-i", str(mp3), str(wav)],
                check=True,
                capture_output=True,
            )
        else:
            raise RuntimeError("缺少 ffmpeg，无法转 wav 播放")
        winsound.PlaySound(str(wav), winsound.SND_FILENAME)
        wav.unlink(missing_ok=True)

    async def close(self) -> None:
        if self._player and not self._player.done():
            self._player.cancel()
            try:
                await self._player
            except (asyncio.CancelledError, Exception):
                pass
