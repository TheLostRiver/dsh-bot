"""实时混音：把真人麦克风和 AI 语音混成一路，送去直播姬/OBS。

为什么需要它：直播姬只能选一个音频输入设备。要么收你的麦克风，要么收虚拟声卡。
想"真人说话 + AI 插话"同时成立，就得在软件层先把两路混好，只给它一路。

设计要点：
- 纯函数（resample / mix_frames）不依赖设备，可离线测试
- 闪避（ducking）：AI 说话时把麦克风压低，避免盖住人声
- 采样率不一致时用线性插值重采样（TTS 是 24kHz，声卡常见 48kHz）
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("biliauto.audio.mixer")

# 混音统一用 float32 [-1, 1]
_EPS = 1e-6


def to_float(data: np.ndarray) -> np.ndarray:
    """int16 -> float32 [-1,1]；已经是浮点就原样返回。"""
    if data.dtype == np.float32:
        return data
    if data.dtype == np.int16:
        return data.astype(np.float32) / 32768.0
    return data.astype(np.float32) / max(1.0, float(np.abs(data).max()) or 1.0)


def resample(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """线性插值重采样。语音场景够用，不需要 sinc。"""
    if src_sr == dst_sr or len(x) == 0:
        return x.astype(np.float32, copy=False)
    n_out = max(1, int(round(len(x) * dst_sr / float(src_sr))))
    src_idx = np.arange(len(x), dtype=np.float64)
    dst_idx = np.linspace(0, len(x) - 1, n_out)
    return np.interp(dst_idx, src_idx, x).astype(np.float32)


def mix_frames(
    mic: np.ndarray,
    tts: np.ndarray | None,
    mic_gain: float = 1.0,
    tts_gain: float = 1.0,
    duck: float = 0.0,
) -> np.ndarray:
    """把一帧麦克风和（可能的）同长度 TTS 混在一起。

    duck>0 时，只要这一帧有 TTS，就把麦克风按 (1-duck) 压下去。
    返回已限幅的 float32。
    """
    out = mic.astype(np.float32, copy=True) * float(mic_gain)

    if tts is not None and len(tts):
        if len(tts) != len(out):
            tts = _fit(tts, len(out))
        if duck > 0:
            out *= max(0.0, 1.0 - float(duck))
        out += tts.astype(np.float32) * float(tts_gain)

    np.clip(out, -1.0, 1.0, out=out)
    return out


def _fit(x: np.ndarray, n: int) -> np.ndarray:
    if len(x) >= n:
        return x[:n]
    return np.concatenate([x, np.zeros(n - len(x), dtype=np.float32)])


@dataclass
class MixerStats:
    frames: int = 0
    tts_frames: int = 0
    dropped: int = 0


class AudioMixer:
    """麦克风 + TTS 的实时混音器。

    用 sounddevice 的 duplex stream：每个回调里读麦克风、取一段 TTS、
    混好后写出去。TTS 用队列喂，队列见底就只出麦克风。
    """

    def __init__(
        self,
        input_device: int | None,
        output_device: int | None,
        mic_gain: float = 1.0,
        tts_gain: float = 1.0,
        duck: float = 0.85,
        samplerate: int = 0,
        blocksize: int = 1024,
    ) -> None:
        self.input_device = input_device
        self.output_device = output_device
        self.mic_gain = mic_gain
        self.tts_gain = tts_gain
        self.duck = duck
        self.samplerate = samplerate
        self.blocksize = blocksize

        self._stream = None
        self._tts: deque[np.ndarray] = deque()
        self._tts_remain: np.ndarray | None = None
        self.stats = MixerStats()

    # ---------- 生命周期 ----------
    @property
    def running(self) -> bool:
        return self._stream is not None

    def start(self) -> tuple[bool, str]:
        if self.running:
            return True, "已在运行"
        try:
            import sounddevice as sd
        except Exception as e:
            return False, f"未安装 sounddevice：{e}"

        try:
            rate = self.samplerate or int(
                sd.query_devices(self.output_device, "output")["default_samplerate"]
            )
            self._stream = sd.Stream(
                samplerate=rate,
                blocksize=self.blocksize,
                device=(self.input_device, self.output_device),
                channels=(1, 1),
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
            log.info(
                "混音已启动：%sHz，麦克风=%s 输出=%s",
                rate, self.input_device, self.output_device,
            )
            return True, f"混音已启动（{rate}Hz）"
        except Exception as e:
            self._stream = None
            return False, f"打开音频流失败：{str(e)[:160]}"

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self._tts.clear()
        self._tts_remain = None

    # ---------- 喂 TTS ----------
    def feed(self, data: np.ndarray, samplerate: int) -> None:
        """把一段 TTS 音频塞进混音队列（会自动重采样到设备采样率）。"""
        if not self.running:
            return
        try:
            rate = int(self._stream.samplerate)
            mono = data if data.ndim == 1 else data[:, 0]
            self._tts.append(resample(to_float(mono), samplerate, rate))
        except Exception as e:
            log.debug("喂混音失败：%s", e)

    @property
    def queued_seconds(self) -> float:
        if not self.running:
            return 0.0
        total = sum(len(x) for x in self._tts)
        if self._tts_remain is not None:
            total += len(self._tts_remain)
        return total / float(self._stream.samplerate)

    # ---------- 回调 ----------
    def _take(self, n: int) -> np.ndarray | None:
        """从 TTS 队列里取 n 个样本；取不满就返回 None（不播半截）。"""
        parts: list[np.ndarray] = []
        need = n
        while need > 0:
            if self._tts_remain is None or len(self._tts_remain) == 0:
                if not self._tts:
                    # 不够一帧就放回去，下一帧再来
                    for p in reversed(parts):
                        self._tts.appendleft(p)
                    return None
                self._tts_remain = self._tts.popleft()
                continue
            take = min(need, len(self._tts_remain))
            parts.append(self._tts_remain[:take])
            self._tts_remain = self._tts_remain[take:]
            need -= take

        out = np.concatenate(parts) if parts else None
        self._tts_remain = None
        return out

    def _callback(self, indata, outdata, frames, time_info, status) -> None:
        try:
            mic = indata[:, 0] if indata.ndim > 1 else indata
            tts = self._take(frames)
            out = mix_frames(mic, tts, self.mic_gain, self.tts_gain, self.duck)
            if outdata.ndim > 1:
                outdata[:, 0] = out
            else:
                outdata[:] = out

            self.stats.frames += 1
            if tts is not None:
                self.stats.tts_frames += 1
        except Exception:
            outdata.fill(0)

    # ---------- 诊断 ----------
    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "input": self.input_device,
            "output": self.output_device,
            "mic_gain": self.mic_gain,
            "tts_gain": self.tts_gain,
            "duck": self.duck,
            "queued_seconds": round(self.queued_seconds, 2),
            "frames": self.stats.frames,
            "tts_frames": self.stats.tts_frames,
        }
