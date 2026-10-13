"""FakeSonicService：AWSNovaSonicLLMService 的替身（tech §12；对齐 S0-r2 结论）。

它做四件事：
1. 记录构造参数（init_kwargs）和 register_function 调用 —— AC-64、AC-65 检查不带工具、带续接。
2. 记录送进来的输入音频帧：总数、全零帧数、字节数 —— AC-74 检查转接期间只收到零值帧。
3. 模拟私有注入 `_send_text_event(text, role, interactive)`，与 pipecat 1.12.0 llm.py:943-955 一致：
   `_stream` 或 `_prompt_name` 为空、或 text 为空时静默丢弃（status=dropped）；
   续接换流期间（`_sc.handoff_in_progress=True`）发出的文字记为 status=lost（发到了旧流）。
   换流顺序照 llm.py:1262-1305：先 clear_handoff_flag()（旗标先复位），再 swap_stream()（`_stream`
   换成新对象、`_audio_input_started=True`）。D 线的注入队列要等 `_stream` 变化后再发（S0-r2 V7）。
4. 按脚本推出一段回应：帧序列照 llm.py:1399-1649 ——
   LLMFullResponseStartFrame、TTSStartedFrame、（SPECULATIVE 文字）LLMTextFrame、TTSAudioRawFrame×n、
   LLMFullResponseEndFrame、TTSStoppedFrame；同时经 SonicTap 发 provider 文字事件：
   ASSISTANT SPECULATIVE（文字帧之前）与 ASSISTANT FINAL（回应结束之后）。
   USER FINAL 只经 SonicTap 发出（S0-r2 V12：TurnDetector 从 provider 事件取数）；
   push_transcription_frames=True 时另向上游推 TranscriptionFrame。

帧怎么映射可以替换：frame_factory(SonicFrameEvent) -> list[帧]。默认映射用上面的 Pipecat 帧类；
没装 Pipecat 时返回 SonicFrameEvent 本身，便于纯逻辑测试。D 线若需别的帧，在 lane_D 测试里
传自己的 frame_factory，不要改本文件。

输出去向：设了 sink（async 可调用）就交给 sink（便于直接驱动下一个处理器的 process_frame）；
否则用 FrameProcessor.push_frame（需要在已启动的管线里）。
"""

import math
import struct
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from waitless.models import ProviderTextEvent

try:  # Pipecat 只在 lane/env 的环境里有；没有时退化为纯记录对象
    from pipecat.frames.frames import (
        InputAudioRawFrame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
        TranscriptionFrame,
        TTSAudioRawFrame,
        TTSStartedFrame,
        TTSStoppedFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    HAVE_PIPECAT = True
except ImportError:  # pragma: no cover - 取决于环境
    FrameProcessor = object  # type: ignore[misc,assignment]
    FrameDirection = None  # type: ignore[assignment]
    HAVE_PIPECAT = False


@dataclass
class SonicResponse:
    """一段脚本回应。"""

    text: str
    final_text: str | None = None  # provider ASSISTANT FINAL 文字；默认同 text
    audio_ms: int = 600
    sample_rate: int = 24000  # Nova Sonic 默认输出 24 kHz（inv-1 §1.4）
    chunk_ms: int = 40
    stop_reason: str = "END_TURN"
    text_first: bool = True  # False：先出一块音频再出文字（OutputGuard 反例）
    with_text: bool = True  # False：只有音频没有文字（OutputGuard 必须一直扣住）


@dataclass
class SonicFrameEvent:
    kind: Literal["response_start", "text", "audio", "response_end", "user_transcription"]
    response: int | None = None
    text: str | None = None
    audio: bytes | None = None
    sample_rate: int | None = None


@dataclass
class Injection:
    text: str
    role: str | None
    interactive: bool
    generation: int
    status: Literal["sent", "dropped", "lost"]


@dataclass
class _FakeContinuationHelper:
    """对应 llm._sc（session continuation helper）上 D 线会读的旗标。"""

    handoff_in_progress: bool = False


def default_frame_factory(ev: SonicFrameEvent) -> list[Any]:
    if not HAVE_PIPECAT:
        return [ev]
    if ev.kind == "response_start":
        return [LLMFullResponseStartFrame(), TTSStartedFrame()]
    if ev.kind == "text":
        return [LLMTextFrame(text=ev.text or "")]
    if ev.kind == "audio":
        return [TTSAudioRawFrame(audio=ev.audio or b"", sample_rate=ev.sample_rate or 24000, num_channels=1)]
    if ev.kind == "response_end":
        return [LLMFullResponseEndFrame(), TTSStoppedFrame()]
    if ev.kind == "user_transcription":
        return [TranscriptionFrame(text=ev.text or "", user_id="fake-user", timestamp="")]
    raise ValueError(ev.kind)


def _tone(ms: int, rate: int, freq: float = 330.0, amplitude: float = 0.2) -> bytes:
    n = int(rate * ms / 1000)
    peak = int(32767 * amplitude)
    return b"".join(struct.pack("<h", int(peak * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))


class FakeSonicService(FrameProcessor):  # type: ignore[misc]
    def __init__(
        self,
        *args: Any,
        frame_factory: Callable[[SonicFrameEvent], list[Any]] | None = None,
        responses_on_inject: Iterable[SonicResponse | str] = (),
        auto_play: bool = False,
        sink: Callable[[Any], Awaitable[None]] | None = None,
        push_transcription_frames: bool = False,
        clock: Any = None,
        **kwargs: Any,
    ) -> None:
        if HAVE_PIPECAT:
            super().__init__()
        self.init_args = args
        self.init_kwargs = kwargs
        self.registered_functions: dict[str, Any] = {}
        # 与 llm.py 同名的内部状态（D 线的注入器读这些）
        self._sc = _FakeContinuationHelper()
        self._stream: object | None = object()
        self._prompt_name: str | None = "fake-prompt"
        self._audio_input_started = True
        self.stream_generation = 1
        # 注入与回应
        self.injections: list[Injection] = []
        self.responses_on_inject: deque[SonicResponse] = deque(
            r if isinstance(r, SonicResponse) else SonicResponse(text=r) for r in responses_on_inject
        )
        self.auto_play = auto_play
        self._pending: deque[SonicResponse] = deque()
        self.response_count = 0
        # 输出
        self.frame_factory = frame_factory or default_frame_factory
        self.sink = sink
        self.push_transcription_frames = push_transcription_frames
        self._listeners: list[Callable[[ProviderTextEvent], Awaitable[None]]] = []
        self.emitted_frames: list[Any] = []
        self.text_events: list[ProviderTextEvent] = []
        # 输入音频统计（AC-74）
        self.input_frames = 0
        self.input_zero_frames = 0
        self.input_bytes = 0
        self._clock = clock
        self._t_ms = 0

    # -- 构造参数视图 --------------------------------------------------------
    @property
    def tools(self) -> Any:
        return self.init_kwargs.get("tools")

    @property
    def session_continuation(self) -> Any:
        return self.init_kwargs.get("session_continuation")

    def register_function(self, name: str, handler: Any, **kwargs: Any) -> None:
        self.registered_functions[name] = handler

    # -- SonicTap -----------------------------------------------------------
    def add_text_listener(self, listener: Callable[[ProviderTextEvent], Awaitable[None]]) -> None:
        self._listeners.append(listener)

    # -- 私有注入（llm.py:943-955 的行为） ------------------------------------
    async def _send_text_event(self, text: str, role: Any = None, interactive: bool = False) -> None:
        role_value = getattr(role, "value", role)
        if not self._stream or not self._prompt_name or not text:
            status: Literal["sent", "dropped", "lost"] = "dropped"
        elif self._sc.handoff_in_progress:
            status = "lost"
        else:
            status = "sent"
        self.injections.append(
            Injection(text=text, role=role_value, interactive=interactive, generation=self.stream_generation, status=status)
        )
        if status == "sent" and self.responses_on_inject:
            self._pending.append(self.responses_on_inject.popleft())
            if self.auto_play:
                await self.play_pending()

    @property
    def sent_texts(self) -> list[str]:
        return [i.text for i in self.injections if i.status == "sent"]

    # -- 会话续接模拟（llm.py:1262-1305 的顺序） -----------------------------
    def begin_continuation(self) -> None:
        self._sc.handoff_in_progress = True

    def clear_handoff_flag(self) -> None:
        self._sc.handoff_in_progress = False

    def swap_stream(self) -> None:
        self._stream = object()
        self.stream_generation += 1
        self._audio_input_started = True

    def complete_continuation(self) -> None:
        self.clear_handoff_flag()
        self.swap_stream()

    # -- 推出转写与回应 ------------------------------------------------------
    async def emit_user_text(self, text: str, *, stage: str = "FINAL", stop_reason: str | None = "END_TURN") -> None:
        """对方的一块 USER 转写（stop_reason 可为 PARTIAL_TURN，S0-r2 V12）。"""
        await self._text_event("USER", stage, text, stop_reason, None)
        if self.push_transcription_frames and stage == "FINAL":
            await self._send(SonicFrameEvent(kind="user_transcription", text=text), upstream=True)

    async def emit_response(self, response: SonicResponse | str) -> int:
        """推出一段回应，返回回应编号（从 1 开始，与 LLMFullResponseStartFrame 计数一致）。"""
        r = response if isinstance(response, SonicResponse) else SonicResponse(text=response)
        self.response_count += 1
        n = self.response_count
        chunks = self._audio_chunks(r)
        await self._send(SonicFrameEvent(kind="response_start", response=n))
        if r.with_text and r.text_first:
            await self._speculative(r, n)
        for i, chunk in enumerate(chunks):
            await self._send(SonicFrameEvent(kind="audio", response=n, audio=chunk, sample_rate=r.sample_rate))
            if i == 0 and r.with_text and not r.text_first:
                await self._speculative(r, n)
        if r.with_text and not r.text_first and not chunks:
            await self._speculative(r, n)
        await self._send(SonicFrameEvent(kind="response_end", response=n))
        if r.with_text:
            await self._text_event("ASSISTANT", "FINAL", r.final_text if r.final_text is not None else r.text, r.stop_reason, n)
        return n

    async def play_pending(self) -> int:
        played = 0
        while self._pending:
            await self.emit_response(self._pending.popleft())
            played += 1
        return played

    # -- Pipecat 处理 -------------------------------------------------------
    async def process_frame(self, frame: Any, direction: Any) -> None:
        if not HAVE_PIPECAT:
            raise RuntimeError("process_frame needs pipecat installed")
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.record_input_audio(frame.audio)
            return  # 与真实服务一致：输入音频被消费，不往下游传
        await self.push_frame(frame, direction)

    def record_input_audio(self, audio: bytes) -> None:
        self.input_frames += 1
        self.input_bytes += len(audio)
        if not any(audio):
            self.input_zero_frames += 1

    # -- 内部 ---------------------------------------------------------------
    def _now(self) -> int:
        if self._clock is not None:
            return int(self._clock.monotonic_ms())
        self._t_ms += 1
        return self._t_ms

    async def _speculative(self, r: SonicResponse, n: int) -> None:
        await self._text_event("ASSISTANT", "SPECULATIVE", r.text, "PARTIAL_TURN", n)
        await self._send(SonicFrameEvent(kind="text", response=n, text=r.text))

    async def _text_event(self, role: str, stage: str, text: str, stop: str | None, response: int | None) -> None:
        ev = ProviderTextEvent(role=role, stage=stage, text=text, stop_reason=stop, t_ms=self._now(), response=response)
        self.text_events.append(ev)
        for listener in list(self._listeners):
            await listener(ev)

    def _audio_chunks(self, r: SonicResponse) -> list[bytes]:
        total = _tone(r.audio_ms, r.sample_rate)
        step = max(2, int(r.sample_rate * r.chunk_ms / 1000) * 2)
        return [total[i : i + step] for i in range(0, len(total), step)]

    async def _send(self, ev: SonicFrameEvent, *, upstream: bool = False) -> None:
        for frame in self.frame_factory(ev):
            self.emitted_frames.append(frame)
            if self.sink is not None:
                await self.sink(frame)
            elif HAVE_PIPECAT:
                direction = FrameDirection.UPSTREAM if upstream else FrameDirection.DOWNSTREAM
                await self.push_frame(frame, direction)


__all__ = [
    "HAVE_PIPECAT",
    "FakeSonicService",
    "Injection",
    "SonicFrameEvent",
    "SonicResponse",
    "default_frame_factory",
]
