"""转接期间的媒体隔离（D 线；tech §6.5 MediaGate、S0-r2 V6；AC-74、AC-75；K4）。

公开接口（§7）：MediaGateIn、MediaGateOut、MediaGate.enter_handoff()/exit_handoff()/counters。
S0-r2 V6：转接期间连续 120 s 只送全零帧，会话不报错，接回注入后 965 ms 出新语音 —— 采用零值帧方案，
不用舒适噪声，也不重建会话。
"""

from typing import Any
import asyncio
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, OutputDTMFFrame, OutputDTMFUrgentFrame
from waitless.models import HANDOFF_SAMPLE_RATE, EngineDtmf
from waitless.interfaces import Clock
from waitless.config import SystemClock


from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pydantic import BaseModel

__all__ = ["MediaGate", "MediaGateCounters", "MediaGateIn", "MediaGateOut"]


def response_identity(identity):
    """One encoding for provider bindings, audio summaries and utterances."""
    if identity is None:
        return None
    if isinstance(identity, tuple) and len(identity) == 2:
        return f'{identity[0]}:{identity[1]}'
    return str(identity)


class MediaGateCounters(BaseModel):
    """AC-74 的帧计数（只统计转接期间）。"""

    frames_to_sonic: int = 0
    nonzero_frames_to_sonic: int = 0
    ai_audio_frames_out: int = 0
    dtmf_frames_out: int = 0
    user_audio_frames_out: int = 0
    frames_to_bridge: int = 0
    user_dtmf_keys_out: int = 0
    user_audio_frames_muted: int = 0


class MediaGate:
    """持有 in/out 两个处理器与 HandoffBridge 的引用。

    MediaGate 管许可、隔离与清理确认；同步 fact_sink 由 LiveCall 注入并负责写 trace。
    LiveCall 在有效 hello/ready 后写 handoff_started；live 不写 handoff_ended。
    pending/hold 仅抑制输出，in_handoff 才把真实入站替换为零帧并停止转写。
    """

    def __init__(self, *, bridge: Any | None = None, dtmf: EngineDtmf | None = None,
                 clock: Clock | None = None, output_buffer_ms: int = 1200, fact_sink=None) -> None:
        self._fact_sink = fact_sink
        self._audio_facts = {}
        self._settled_responses = set()
        self._clear_serial = 0
        self._cleanup_ids = {}
        self.output_observers = []
        self.requires_output_ack = False
        self.output_state = 'closed'
        self.company_seq = 0
        self.output_epoch = 0
        self._pending_epoch = None
        self._response = None
        self._bindings = {}
        self._revoked = set()
        self._audible = set()
        self._buffer = []
        self._buffer_ms = 0
        self._buffer_limit_ms = output_buffer_ms
        self._cleaner = None
        self._clean_lock = asyncio.Lock()
        self._cleaned = asyncio.Event()
        self._superseded_cleanups = set()
        self._cleaned.set()
        self._bridge = bridge
        self._dtmf = dtmf
        self._clock = clock if clock is not None else SystemClock()
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

    def set_output_cleaner(self, cleaner):
        """The awaitable returns only after sender/filter/line clear completes."""
        self._cleaner = cleaner

    def _fact(self, kind, *, t_ms=None, **fields):
        if self._fact_sink is not None:
            self._fact_sink(kind, t_ms=self._clock.monotonic_ms() if t_ms is None else t_ms, **fields)

    def settle_responses(self):
        for identity in self._audio_facts:
            self._settle_response(identity)

    def _settle_response(self, identity):
        if identity in self._settled_responses:
            return
        first, last, frames = self._audio_facts[identity]
        self._fact('response_audio', identity=response_identity(identity),
                   first_ms=first, last_ms=last, frames=frames)
        self._settled_responses.add(identity)

    def _commit_audio(self, identity):
        first, _, frames = self._audio_facts[identity]
        now = self._clock.monotonic_ms()
        self._audio_facts[identity] = (now if first is None else first, now, frames + 1)

    def _revoke(self, state, *, trigger_seq=None, revoked_permission):
        self.closed_at_ms = self._clock.monotonic_ms()
        self.output_epoch += 1
        self.output_state = state
        self._pending_epoch = None
        self._revoked.update(self._bindings)
        self._bindings.clear()
        self._response = None
        self._buffer.clear()
        self._buffer_ms = 0
        if not self._cleaned.is_set():
            self._superseded_cleanups.add(self._cleaned)
        self._cleaned = asyncio.Event()
        self._clear_serial += 1
        self._cleanup_ids[self._cleaned] = self._clear_serial
        self._fact('output_revoked', t_ms=self.closed_at_ms, trigger_seq=trigger_seq,
                   revoked_seq=revoked_permission[0], revoked_epoch=revoked_permission[1],
                   new_epoch=self.output_epoch, state=state, clear_id=self._clear_serial)
        self.settle_responses()

    async def wait_output_clean(self):
        while True:
            completed = self._cleaned
            await completed.wait()
            if completed is self._cleaned:
                return

    def begin_pending(self, seq):
        """Synchronous: must run in the USER FINAL callback before any await/task."""
        if self.output_state in ('handoff', 'ended') or seq <= self.company_seq:
            return
        self._revoke('pending', trigger_seq=seq, revoked_permission=(self.company_seq, self.output_epoch))
        self.company_seq = seq
        self._pending_epoch = self.output_epoch

    async def clear_output(self):
        completed = self._cleaned
        clear_id = self._cleanup_ids.get(completed, 0)
        async with self._clean_lock:
            if completed.is_set():
                return
            try:
                if self._cleaner is not None:
                    # Preserve upstream interruption, downstream completion and
                    # the original lock/await ordering. Observation never awaits.
                    if self.outbound.previous is not None:
                        from pipecat.frames.frames import InterruptionFrame
                        await self.outbound.push_frame(InterruptionFrame(), FrameDirection.UPSTREAM)
                    await self._cleaner()
                elif self.outbound.next is not None:
                    await self.outbound.broadcast_interruption()
            except BaseException:
                self._fact('output_cleared', clear_id=clear_id, covers=[], ok=False)
                raise
            self.clear_completed_at_ms = self._clock.monotonic_ms()
            covers = sorted(self._cleanup_ids[waiter] for waiter in self._superseded_cleanups
                            if waiter is not completed)
            self._fact('output_cleared', t_ms=self.clear_completed_at_ms, clear_id=clear_id, covers=covers, ok=True)
            completed.set()
            for waiter in self._superseded_cleanups:
                waiter.set()
            self._superseded_cleanups.clear()

    def bind_response(self, identity):
        """Bind once at response start; a late chunk cannot acquire a new seq."""
        if identity not in self._bindings and identity not in self._revoked:
            if self._response is not None:
                self._revoked.add(self._response)
                self._settle_response(self._response)
                self._buffer.clear()
                self._buffer_ms = 0
            self._response = identity
            self._bindings[identity] = (self.company_seq, self.output_epoch)
            self._audio_facts[identity] = (None, None, 0)
            self._fact('response_bound', identity=response_identity(identity), seq=self.company_seq, epoch=self.output_epoch)
        return self._bindings.get(identity)

    def response_audible(self, identity):
        return identity in self._audible and identity not in self._revoked and identity in self._bindings

    async def mark_audible(self, identity):
        if (self.output_state != 'allowed' or identity in self._revoked
                or self._bindings.get(identity) != (self.company_seq, self.output_epoch)):
            return
        self._audible.add(identity)
        for observer in self.output_observers:
            await observer(identity)

    async def resolve_speaker(self, seq, speaker):
        epoch = self._pending_epoch
        if seq != self.company_seq or self.output_state != 'pending' or epoch is None:
            return False
        await self.wait_output_clean()
        if seq != self.company_seq or self.output_epoch != epoch or self.output_state != 'pending':
            return False
        if speaker not in ('menu_or_recording', 'virtual_agent'):
            self._revoke('hold' if speaker == 'hold' else 'closed', revoked_permission=(self.company_seq, self.output_epoch))
            await self.clear_output()
            return True
        self.output_state = 'allowed'
        self._fact('output_allowed', seq=self.company_seq, epoch=self.output_epoch, speaker=speaker)
        buffered, self._buffer = self._buffer, []
        self._buffer_ms = 0
        for frame in buffered:
            await self.outbound._ai_audio(frame)
        return True

    async def invalidate_stream(self):
        if self.output_state not in ('handoff', 'ended'):
            self._revoke('closed', revoked_permission=(self.company_seq, self.output_epoch))
            await self.clear_output()

    async def end_output(self):
        self._revoke('ended', revoked_permission=(self.company_seq, self.output_epoch))
        await self.clear_output()
        for delivered in self.outbound._user_audio_pending:
            delivered.set()

    async def enter_handoff(self) -> None:
        if self.output_state == 'ended':
            return
        if not self._in_handoff:
            self._in_handoff = True
            self._revoke('handoff', revoked_permission=(self.company_seq, self.output_epoch))
        await self.clear_output()

    async def exit_handoff(self) -> None:
        self._in_handoff = False
        self.output_state = 'closed'
        self._outbound._user_mute_until_ms = 0
        for delivered in self._outbound._user_audio_pending:
            delivered.set()

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
        self._user_mute_until_ms = 0
        self._user_audio_pending: set[asyncio.Event] = set()

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, (OutputAudioRawFrame, OutputDTMFFrame, OutputDTMFUrgentFrame)):
                epoch = getattr(frame, '_waitless_output_epoch', self.gate.output_epoch)
                if epoch != self.gate.output_epoch:
                    return
            if isinstance(frame, OutputAudioRawFrame):
                if getattr(frame, '_waitless_dtmf_tail', False):
                    if self.gate.output_state not in ('handoff', 'ended', 'closed'):
                        frame._waitless_output_epoch = self.gate.output_epoch
                        await self.push_frame(frame, direction)
                else:
                    await self._ai_audio(frame)
                return
            if isinstance(frame, (OutputDTMFFrame, OutputDTMFUrgentFrame)):
                if self.gate.output_state in ('handoff', 'ended', 'closed'):
                    return
                frame._waitless_output_epoch = self.gate.output_epoch
        await self.push_frame(frame, direction)

    async def _ai_audio(self, frame):
        g = self.gate
        identity = getattr(frame, 'response_identity', None)
        binding = g._bindings.get(identity)
        if (binding != (g.company_seq, g.output_epoch) or identity in g._revoked
                or getattr(frame, 'company_seq', None) != g.company_seq
                or identity != g._response):
            return
        if g.output_state == 'pending':
            g._buffer_ms += len(frame.audio) * 1000 / (frame.sample_rate * frame.num_channels * 2)
            if g._buffer_ms > g._buffer_limit_ms:
                g._revoked.add(identity)
                g._settle_response(identity)
                g._buffer.clear()
                g._buffer_ms = 0
            else:
                g._buffer.append(frame)
        elif g.output_state == 'allowed' and g._cleaned.is_set():
            frame._waitless_output_epoch = g.output_epoch
            g._commit_audio(identity)
            await self.push_frame(frame)
            if not g.requires_output_ack:
                await g.mark_audible(identity)

    async def push_user_audio(self, pcm16_16k: bytes) -> None:
        """HandoffBridge 调用：包成 OutputAudioRawFrame(sample_rate=16000) 推给 transport.output（§6.4）。"""
        await self.gate.wait_output_clean()
        if not self.gate.in_handoff or self.gate.output_state == 'ended':
            return
        if self.gate._clock.monotonic_ms() < self._user_mute_until_ms:
            self.gate.counters.user_audio_frames_muted += 1
            return
        self.gate.counters.user_audio_frames_out += 1
        frame = OutputAudioRawFrame(audio=pcm16_16k, sample_rate=HANDOFF_SAMPLE_RATE, num_channels=1)
        frame._waitless_output_epoch = self.gate.output_epoch
        frame._waitless_user_audio = True
        downstream = self.next
        if downstream is None:
            await self.push_frame(frame)
            return
        # Urgent DTMF can overtake queued DataFrames. Finish delivering this PCM
        # to the output processor before receiving the next browser message.
        delivered = asyncio.Event()
        async def on_processed(processor, processed):
            if processed is frame:
                delivered.set()
        self._user_audio_pending.add(delivered)
        downstream.add_event_handler("on_after_process_frame", on_processed)
        try:
            await self.push_frame(frame)
            await delivered.wait()
        finally:
            downstream.remove_event_handler("on_after_process_frame", on_processed)
            self._user_audio_pending.discard(delivered)

    async def push_user_dtmf(self, keys: str) -> bool:
        """用户按键只从本处理器直接送往线路；不经 AI，不记录键的内容。"""
        await self.gate.wait_output_clean()
        if not self.gate.in_handoff or self.gate.output_state == 'ended':
            return False
        config = self.gate._dtmf
        if config is None:
            raise ValueError("user DTMF settings are required")
        self._user_mute_until_ms = (self.gate._clock.monotonic_ms()
            + config.user_mute_ms_per_key * len(keys) + config.tail_silence_ms)
        frame = OutputDTMFUrgentFrame.from_string(keys)
        frame._waitless_output_epoch = self.gate.output_epoch
        await self.push_frame(frame)
        if config.tail_silence_ms:
            tail = OutputAudioRawFrame(
                audio=bytes(HANDOFF_SAMPLE_RATE * config.tail_silence_ms // 1000 * 2),
                sample_rate=HANDOFF_SAMPLE_RATE, num_channels=1)
            tail._waitless_dtmf_tail = True
            tail._waitless_output_epoch = self.gate.output_epoch
            await self.push_frame(tail)
        self.gate.counters.user_dtmf_keys_out += len(keys)
        return True
