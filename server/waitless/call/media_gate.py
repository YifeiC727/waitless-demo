"""转接期间的媒体隔离（D 线；tech §6.5 MediaGate、S0-r2 V6；AC-74、AC-75；K4）。

公开接口（§7）：MediaGateIn、MediaGateOut、MediaGate.enter_handoff()/exit_handoff()/counters。
S0-r2 V6：转接期间连续 120 s 只送全零帧，会话不报错，接回注入后 965 ms 出新语音 —— 采用零值帧方案，
不用舒适噪声，也不重建会话。
"""

from typing import Any
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, OutputDTMFFrame, OutputDTMFUrgentFrame
from waitless.models import HANDOFF_SAMPLE_RATE


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
        self._bridge = bridge
        self._in_handoff = False
        self._counters = MediaGateCounters()
        self._inbound = MediaGateIn(self)
        self._outbound = MediaGateOut(self)

    @property
    def inbound(self) -> "MediaGateIn":
        return self._inbound

    @property
    def outbound(self) -> "MediaGateOut":
        return self._outbound

    @property
    def in_handoff(self) -> bool:
        return self._in_handoff

    @property
    def counters(self) -> MediaGateCounters:
        return self._counters

    async def enter_handoff(self) -> None:
        """切到转接：两侧开始隔离，并 broadcast_interruption 清掉已排队的 AI 音频（inv-1 §1.8）。"""
        if not self._in_handoff:
            self._in_handoff = True
            await self._outbound.broadcast_interruption()

    async def exit_handoff(self) -> None:
        self._in_handoff = False

    def attach_bridge(self, bridge: Any) -> None:
        self._bridge = bridge


class MediaGateIn(FrameProcessor):
    """转接期间：InputAudioRawFrame 复制给 HandoffBridge（送浏览器），往 Sonic 只送等长的零值帧。"""

    def __init__(self, gate: MediaGate) -> None:
        super().__init__(enable_direct_mode=True)
        self.gate = gate

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and self.gate.in_handoff and isinstance(frame, InputAudioRawFrame):
            c = self.gate.counters
            if self.gate._bridge is not None:
                await self.gate._bridge.to_browser(frame.audio)
                c.frames_to_bridge += 1
            frame = InputAudioRawFrame(audio=bytes(len(frame.audio)), sample_rate=frame.sample_rate, num_channels=frame.num_channels)
            c.frames_to_sonic += 1
        await self.push_frame(frame, direction)


class MediaGateOut(FrameProcessor):
    """转接期间：丢弃 Sonic 的音频帧（含 TTSAudioRawFrame）和所有 OutputDTMF* 帧，只放行 HandoffBridge 注入的用户音频。
    生命周期帧与 InterruptionFrame 照常传递。"""

    def __init__(self, gate: MediaGate) -> None:
        super().__init__(enable_direct_mode=True)
        self.gate = gate

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and self.gate.in_handoff and isinstance(frame, (OutputAudioRawFrame, OutputDTMFFrame, OutputDTMFUrgentFrame)):
            return
        await self.push_frame(frame, direction)

    async def push_user_audio(self, pcm16_16k: bytes) -> None:
        """HandoffBridge 调用：包成 OutputAudioRawFrame(sample_rate=16000) 推给 transport.output（§6.4）。"""
        if not self.gate.in_handoff:
            return
        self.gate.counters.user_audio_frames_out += 1
        await self.push_frame(OutputAudioRawFrame(audio=pcm16_16k, sample_rate=HANDOFF_SAMPLE_RATE, num_channels=1))
