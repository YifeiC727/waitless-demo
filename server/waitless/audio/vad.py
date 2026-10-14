"""Energy VAD using the configured threshold and shared PCM codec."""
from waitless.audio.codec import pcm16_rms_dbfs

__all__ = ["EnergyVad"]


class EnergyVad:
    def __init__(self, *, threshold_dbfs: float, frame_ms: int, sample_rate: int) -> None:
        if frame_ms <= 0 or sample_rate <= 0:
            raise ValueError("frame duration and sample rate must be positive")
        self.threshold_dbfs = threshold_dbfs
        self.frame_ms = frame_ms
        self.sample_rate = sample_rate
        self._last_voice_ms = None

    def level_dbfs(self, pcm16: bytes) -> float:
        return pcm16_rms_dbfs(pcm16)

    def is_speech(self, pcm16: bytes) -> bool:
        return self.level_dbfs(pcm16) > self.threshold_dbfs

    def feed(self, pcm16: bytes, t_ms: int) -> bool:
        if len(pcm16) % 2:
            raise ValueError("PCM16 must contain whole samples")
        size = self.sample_rate * self.frame_ms // 1000 * 2
        voiced = False
        for offset in range(0, len(pcm16), size):
            chunk = pcm16[offset:offset + size]
            if self.is_speech(chunk):
                voiced = True
                self._last_voice_ms = t_ms + offset * 1000 // (2 * self.sample_rate)
        return voiced

    @property
    def last_voice_ms(self) -> int | None:
        return self._last_voice_ms
