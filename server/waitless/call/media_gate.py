"""转接期间的媒体隔离（D 线；tech §6.5 MediaGate、S0-r2 V6；AC-74、AC-75；K4）。

公开接口（§7）：MediaGateIn、MediaGateOut、MediaGate.enter_handoff()/exit_handoff()/counters。
S0-r2 V6：转接期间连续 120 s 只送全零帧，会话不报错，接回注入后 965 ms 出新语音 —— 采用零值帧方案，
不用舒适噪声，也不重建会话。
"""

from typing import Any

from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pydantic import BaseModel

__all__ = ["MediaGate", "MediaGateCounters", "MediaGateIn", "MediaGateOut"]


class MediaGateCounters(BaseModel):
    """AC-74 的帧计数（只统计转接期间）。"""

    frames_to_sonic: int = 0
    nonzero_frames_to_sonic: int = 0
    ai_audio_frames_out: int = 0
    dtmf_frames_out: int = 0
    user_audio_frames_out: int = 0
    frames_to_bridge: int = 0


class MediaGate:
    """持有 in/out 两个处理器与 HandoffBridge 的引用。

    缺口 D-G-3（定稿 livecall_writes）：MediaGate 只管媒体，不写 trace、不带 decision_id；handoff_started 由
    LiveActionBackend 在 enter_handoff() 之后写，handoff_ended 由 LiveCall 在 exit_handoff() 之后写。
    in_handoff 供 TranscriptTap 判断丢弃转写（AC-75）。tech §7.1 AC-75/77 的「由 MediaGate 写」待设计下一版改文字。
    """

    def __init__(self, *, bridge: Any | None = None) -> None:
        raise NotImplementedError

    @property
    def inbound(self) -> "MediaGateIn":
        raise NotImplementedError

    @property
    def outbound(self) -> "MediaGateOut":
        raise NotImplementedError

    @property
    def in_handoff(self) -> bool:
        raise NotImplementedError

    @property
    def counters(self) -> MediaGateCounters:
        raise NotImplementedError

    async def enter_handoff(self) -> None:
        """切到转接：两侧开始隔离，并 broadcast_interruption 清掉已排队的 AI 音频（inv-1 §1.8）。"""
        raise NotImplementedError

    async def exit_handoff(self) -> None:
        raise NotImplementedError

    def attach_bridge(self, bridge: Any) -> None:
        raise NotImplementedError


class MediaGateIn(FrameProcessor):
    """转接期间：InputAudioRawFrame 复制给 HandoffBridge（送浏览器），往 Sonic 只送等长的零值帧。"""

    def __init__(self, gate: MediaGate) -> None:
        raise NotImplementedError

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        raise NotImplementedError


class MediaGateOut(FrameProcessor):
    """转接期间：丢弃 Sonic 的音频帧（含 TTSAudioRawFrame）和所有 OutputDTMF* 帧，只放行 HandoffBridge 注入的用户音频。
    生命周期帧与 InterruptionFrame 照常传递。"""

    def __init__(self, gate: MediaGate) -> None:
        raise NotImplementedError

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        raise NotImplementedError

    async def push_user_audio(self, pcm16_16k: bytes) -> None:
        """HandoffBridge 调用：包成 OutputAudioRawFrame(sample_rate=16000) 推给 transport.output（§6.4）。"""
        raise NotImplementedError
