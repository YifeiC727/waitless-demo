"""能量 VAD（D 线；tech §6.5；AC-66）。参数取 engine.yaml 的 vad 段，不另设默认值。

公开接口（§7）：EnergyVad。
"""

__all__ = ["EnergyVad"]


class EnergyVad:
    def __init__(self, *, threshold_dbfs: float, frame_ms: int, sample_rate: int) -> None:
        raise NotImplementedError

    def level_dbfs(self, pcm16: bytes) -> float:
        raise NotImplementedError

    def is_speech(self, pcm16: bytes) -> bool:
        """一帧电平高于 threshold_dbfs 即为有声。"""
        raise NotImplementedError

    def feed(self, pcm16: bytes, t_ms: int) -> bool:
        """按 frame_ms 切帧逐帧判断；返回这段是否含有声帧，并更新 last_voice_ms。"""
        raise NotImplementedError

    @property
    def last_voice_ms(self) -> int | None:
        raise NotImplementedError
