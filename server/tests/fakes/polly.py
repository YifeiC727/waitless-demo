"""FakePolly：形状同 boto3 polly 客户端的 synthesize_speech（tech §12）。

只支持 OutputFormat="pcm"（PCM16 LE 单声道），SampleRate 是字符串 "8000" 或 "16000"
（与 Polly API 一致）。同一段文字每次生成相同的字节：时长 = max(min_ms, ms_per_char × 字符数)，
内容是幅度 amplitude 的正弦音（默认约 −23 dBFS，能被能量 VAD 判为有声）。
"""

import math
import struct
from typing import Any

SUPPORTED_ENGINES = frozenset({"standard", "neural", "long-form", "generative"})
SUPPORTED_RATES = frozenset({"8000", "16000"})


class FakeAudioStream:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self.closed = False

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            chunk = self._data[self._pos :]
        else:
            chunk = self._data[self._pos : self._pos + amt]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


def tone_pcm16(duration_ms: int, sample_rate: int, *, freq_hz: float = 440.0, amplitude: float = 0.1) -> bytes:
    n = int(sample_rate * duration_ms / 1000)
    peak = int(32767 * amplitude)
    return b"".join(
        struct.pack("<h", int(peak * math.sin(2 * math.pi * freq_hz * i / sample_rate))) for i in range(n)
    )


def pcm16_duration_ms(pcm: bytes, sample_rate: int) -> float:
    return len(pcm) / 2 / sample_rate * 1000


class FakePolly:
    def __init__(
        self,
        *,
        ms_per_char: int = 40,
        min_ms: int = 300,
        amplitude: float = 0.1,
        freq_hz: float = 440.0,
        fail_with: BaseException | None = None,
    ) -> None:
        self.ms_per_char = ms_per_char
        self.min_ms = min_ms
        self.amplitude = amplitude
        self.freq_hz = freq_hz
        self.fail_with = fail_with
        self.calls: list[dict[str, Any]] = []

    def synthesize_speech(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        if self.fail_with is not None:
            raise self.fail_with
        for key in ("Text", "OutputFormat", "VoiceId"):
            if key not in kwargs:
                raise TypeError(f"synthesize_speech missing required parameter {key}")
        if kwargs["OutputFormat"] != "pcm":
            raise ValueError("FakePolly only supports OutputFormat='pcm'")
        rate = kwargs.get("SampleRate", "16000")
        if not isinstance(rate, str) or rate not in SUPPORTED_RATES:
            raise ValueError(f"SampleRate must be one of {sorted(SUPPORTED_RATES)} as a string")
        engine = kwargs.get("Engine", "standard")
        if engine not in SUPPORTED_ENGINES:
            raise ValueError(f"unsupported Engine {engine!r}")
        text = kwargs["Text"]
        duration = max(self.min_ms, self.ms_per_char * len(text))
        pcm = tone_pcm16(duration, int(rate), freq_hz=self.freq_hz, amplitude=self.amplitude)
        return {"AudioStream": FakeAudioStream(pcm), "ContentType": "audio/pcm", "RequestCharacters": len(text)}


__all__ = ["FakeAudioStream", "FakePolly", "pcm16_duration_ms", "tone_pcm16"]
