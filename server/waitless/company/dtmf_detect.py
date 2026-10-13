"""Goertzel 按键识别（B 线；tech §6.10、S0 V4；AC-79、AC-80；K15）。

公开接口（§7）：GoertzelDetector.feed(pcm) -> [digit]。
S0 V4：Pipecat 内置音调每个约 300 ms、RMS 约 −24 dBFS；20 ms 窗口、连续 40 ms 判定一个按键。
"""

__all__ = ["DTMF_COLS_HZ", "DTMF_KEYS", "DTMF_ROWS_HZ", "GoertzelDetector"]

# 标准 DTMF 频率（ITU-T Q.23）
DTMF_ROWS_HZ: tuple[float, ...] = (697.0, 770.0, 852.0, 941.0)
DTMF_COLS_HZ: tuple[float, ...] = (1209.0, 1336.0, 1477.0)
DTMF_KEYS: tuple[tuple[str, ...], ...] = (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9"), ("*", "0", "#"))


class GoertzelDetector:
    def __init__(self, *, sample_rate: int, window_ms: int = 20, stable_ms: int = 40) -> None:
        raise NotImplementedError

    def feed(self, pcm16: bytes) -> list[str]:
        """流式喂入 PCM16 LE；返回这次新识别出的按键（按顺序）。同一按键持续期间只报一次。"""
        # todo(B): 判定条件：行、列各有一个主峰超过阈值，且频率纯度（主峰能量占比）足够，用来排除人声（AC-80）。
        # todo(B): 阈值适配 −24 dBFS 的音调（K15）；测试用 Pipecat 自带的 DTMF 音调经 μ-law 往返（AC-79）：
        #          audio.codec 的 ulaw_encode/ulaw_decode 已由第 3 步写好并冻结（A-21），B 线环境另装
        #          pipecat-ai[aws-nova-sonic,websocket]==1.12.0，按 AC-79 原文跑，不等 D 线。
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError
