"""Build the live phone-agent pipeline and Sonic service with session continuation."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
import time
import asyncio
from contextvars import ContextVar
from pipecat.frames.frames import OutputAudioRawFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame
from waitless.call.output_transport import ClearingWebsocketTransport
from pipecat.services.aws.nova_sonic.llm import ContentType
from pipecat.services.aws.nova_sonic.session_continuation import SessionContinuationParams, SessionContinuationHelper
from pipecat.utils.async_tool_cancellation import ASYNC_TOOL_CANCELLATION_INSTRUCTIONS
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport, FastAPIWebsocketParams


from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService

from waitless.config import Settings
from waitless.models import CompiledCall, ProviderTextEvent

__all__ = ["LivePipelineParts", "TappedNovaSonicService", "build_pipeline", "build_sonic_service", "build_transport"]


class TappedNovaSonicService(AWSNovaSonicLLMService):
    """AWSNovaSonicLLMService 加 provider 文字事件订阅（实现 interfaces.SonicTap）。

    S0-r2：USER FINAL 与 ASSISTANT FINAL 不作为独立管线帧推出（llm.py:1501-1569）；TranscriptionFrame
    要等 ASSISTANT 开始回应才向上游推，p95 1288 ms。所以在 provider 的 contentEnd 处理里发 ProviderTextEvent：
    role、stage（FINAL/SPECULATIVE）、text、stop_reason（PARTIAL_TURN 也要发，不要求 END_TURN）、response 编号。
    tests/fakes 的 FakeSonicService 实现同一接口。
    缺口 D-G-8：在 contentEnd 处理里同步 await 每个回调；回调方保证不阻塞（见 interfaces.SonicTap）。
    """

    def __init__(self, *args, continuation_hold_margin_ms: int, **kwargs):
        self._continuation_hold_margin_ms = continuation_hold_margin_ms
        super().__init__(*args, **kwargs)
        self._source_stream = ContextVar('sonic_receive_source', default=None)
        self._stream_generation = 0
        self._generation_stream = None
        self._media_gate = None
        self._promotion_lock = asyncio.Lock()
        self._sc_create_task = self._sc._create_task
        self._sc._create_task = self._create_continuation_task

    def _create_continuation_task(self, coro):
        # Do not replace execute_handoff: service calls it during promotion.
        if getattr(coro, 'cr_code', None) is SessionContinuationHelper.execute_handoff.__code__:
            coro.close()
            coro = self._run_sc_handoff()
        return self._sc_create_task(coro)

    async def _run_sc_handoff(self):
        # 1.12.0 promotion with an application cleanup barrier. Both normal
        # and forced paths serialize here, and keep the original helper method.
        async with self._promotion_lock:
            if self._disconnecting:
                return
            old = (self._client, self._stream, self._receive_task,
                self._prompt_name, self._input_audio_content_name)
            next_session = await self._sc.execute_handoff()
            if not next_session:
                return
            if self._disconnecting:
                self._sc._next_session = next_session
                await self._sc.cleanup_next_session()
                return
            self._client, self._stream = next_session.client, next_session.stream
            self._prompt_name = next_session.prompt_name
            self._input_audio_content_name = next_session.input_audio_content_name
            self._connected_time = time.time()
            self._audio_input_started = True
            self._stream_generation += 1
            self._generation_stream = self._stream
            self._waitless_response_identity = None
            self._assistant_is_responding = False
            self._content_being_received = None
            try:
                if self._media_gate is not None:
                    await self._media_gate.invalidate_stream()
                self._receive_task = self.create_task(self._receive_task_handler(stream=self._stream))
                self._sc.set_connected(self._connected_time)
            finally:
                self.create_task(self._sc.close_old_session(*old), name='sc_close_old_session')

    def bind_media_gate(self, gate):
        self._media_gate = gate

    def _source_current(self):
        source = getattr(self, '_source_stream', None)
        return source is None or source.get() is None or source.get() is self._stream

    async def _receive_task_handler(self, stream=None):
        stream = self._stream if stream is None else stream
        if stream is not self._stream:
            return
        if stream is not self._generation_stream:
            self._stream_generation += 1
            self._generation_stream = stream
            self._waitless_response_identity = None
            self._assistant_is_responding = False
            self._content_being_received = None
            if self._media_gate is not None:
                await self._media_gate.invalidate_stream()
        token = self._source_stream.set(stream)
        try:
            # Upstream 1.12.0 checks stream identity after receive and before
            # dispatch, so old sources cannot touch shared content/SC state.
            await super()._receive_task_handler(stream=stream)
        finally:
            self._source_stream.reset(token)

    async def push_frame(self, frame, direction=None):
        if not self._source_current():
            return
        identity = getattr(self, '_waitless_response_identity', None)
        if isinstance(frame, (OutputAudioRawFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame)):
            frame.response_identity = identity
            binding = getattr(self, '_waitless_response_binding', None)
            frame.company_seq = binding[0] if binding else None
            frame.response_number = getattr(self, '_waitless_response', 0)
        if direction is None:
            await super().push_frame(frame)
        else:
            await super().push_frame(frame, direction)

    def connected_at(self) -> float | None:
        """Read the authoritative pipecat 1.12.0 clock, including teardown state.

        The upstream clock changes only on connection/successful stream swap.
        A failed, empty or concurrent handoff must keep the old session age.
        """
        if self._disconnecting or self._stream is None:
            return None
        return getattr(self, '_connected_time', None)

    def repeat_hold_allowed(self, hold_ms: int) -> bool:
        connected = self.connected_at()
        if connected is None:
            return False
        # The helper holds the exact SessionContinuationParams passed at build.
        threshold = self._sc._params.transition_threshold_seconds
        return time.time() - connected < (
            threshold - hold_ms / 1000 - self._continuation_hold_margin_ms / 1000)

    def _compose_system_instruction(self):
        """1.12.0 composition with phone-agent owning the async result policy.

        Keep all other addons and caches. Do not edit the skill or globally
        replace Pipecat's instructions; ordinary upstream services are unchanged.
        """
        parts = [self._base_system_instruction] if self._base_system_instruction else []
        parts.extend(self._appended_system_instructions)
        if self._filter_incomplete_user_turns:
            parts.append(self._user_turn_completion_config.completion_instructions)
        if self._cancel_tool_names:
            parts.append(ASYNC_TOOL_CANCELLATION_INSTRUCTIONS)
        composed = "\n\n".join(p for p in parts if p) or None
        self._settings.system_instruction = composed
        self._composed_system_instruction = composed

    def add_text_listener(self, listener: Callable[[ProviderTextEvent], Awaitable[None]]) -> None:
        if not hasattr(self, "_text_listeners"):
            self._text_listeners = []
        self._text_listeners.append(listener)

    async def _report_assistant_response_started(self) -> None:
        if not self._source_current():
            return
        self._waitless_response = getattr(self, "_waitless_response", 0) + 1
        self._waitless_response_identity = (getattr(self, '_stream_generation', 0), self._waitless_response)
        gate = getattr(self, '_media_gate', None)
        self._waitless_response_binding = gate.bind_response(self._waitless_response_identity) if gate else None
        await super()._report_assistant_response_started()

    async def _handle_content_end_event(self, event_json: Any) -> None:
        # todo(D): 先取 self._content_being_received（role、type、text_stage、text_content）与 stopReason，
        #          对 TEXT 内容块发 ProviderTextEvent，再 await super()._handle_content_end_event(event_json)。
        #          依据 pipecat-ai==1.12.0 llm.py:1501-1569；版本由 injector 构造时检查。
        if not self._source_current():
            return
        content = self._content_being_received
        if content and content.type == ContentType.TEXT and content.role.value in ("USER", "ASSISTANT"):
            clock = getattr(self, "_waitless_clock", None)
            ev = ProviderTextEvent(role=content.role.value, stage=content.text_stage.value if content.text_stage else None,
                text=content.text_content or "", stop_reason=event_json["contentEnd"].get("stopReason"),
                t_ms=clock.monotonic_ms() if clock else int(time.monotonic() * 1000),
                response=getattr(self, "_waitless_response", 0) or None)
            object.__setattr__(ev, 'response_identity', getattr(self, '_waitless_response_identity', None))
            for listener in tuple(getattr(self, "_text_listeners", ())):
                await listener(ev)
        if self._source_current():
            await super()._handle_content_end_event(event_json)


def build_sonic_service(compiled: CompiledCall, settings: Settings, *, tools) -> TappedNovaSonicService:
    """Build the live service with no fallback tools and continuation enabled."""
    for name in ("sonic_model_id", "sonic_voice", "aws_region", "aws_access_key_id", "aws_secret_access_key"):
        if getattr(settings, name, None) is None:
            raise ValueError(f"missing {name}")
    return TappedNovaSonicService(model=settings.sonic_model_id, voice_id=settings.sonic_voice, region=settings.aws_region,
        access_key_id=settings.aws_access_key_id.get_secret_value(), secret_access_key=settings.aws_secret_access_key.get_secret_value(),
        system_instruction=compiled.sonic_prompt, tools=[], session_continuation=SessionContinuationParams(enabled=True),
        continuation_hold_margin_ms=settings.engine.continuation_hold_margin_ms)


def build_transport(websocket: Any, serializer: Any, settings: Settings) -> Any:
    """FastAPIWebsocketTransport：audio_in/out 启用，serializer=LineSerializer（auto_hang_up=False），不加 WAV 头。

    缺口 D-G-9（定稿 out_16k；K6）：输出采样率固定取 settings.engine.audio.pipeline_in_sample_rate（16 kHz）——
    转接时用户音频照 §6.4 以 16 kHz 直接进 transport，Sonic 的 24 kHz 由 transport 统一降到 16 kHz，serializer 的
    输入率恒为 16 kHz。Pipecat 的流式重采样器（SOXRStreamAudioResampler）不能中途换输入采样率，所以进 transport
    的音频只能有这两种固定来源。D-G-6：LiveCall 经本模块属性调用本函数与 build_sonic_service，测试会替换它们。"""
    return ClearingWebsocketTransport(websocket, FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True,
        audio_in_sample_rate=settings.engine.audio.pipeline_in_sample_rate,
        audio_out_sample_rate=settings.engine.audio.pipeline_in_sample_rate, serializer=serializer, add_wav_header=False))


@dataclass
class LivePipelineParts:
    """一通 live 电话的处理器（由 LiveCall 构造并持有引用）。"""

    media_gate_in: Any
    context: Any
    sonic: TappedNovaSonicService
    monitor: Any
    media_gate_out: Any


def build_pipeline(compiled: CompiledCall, ws: Any, *, settings: Settings, parts: LivePipelineParts) -> tuple[Any, Any]:
    """返回 (Pipeline, PipelineTask)。PipelineParams(audio_in_sample_rate=engine.audio.pipeline_in_sample_rate)（S0 V5）。
    输入 16 kHz、Sonic 输出 24 kHz、线路 8 kHz μ-law（K6）。"""
    transport = ws
    bind = getattr(transport.output(), 'bind_gate', None)
    if bind is not None:
        bind(parts.media_gate_out.gate)
    pipe = Pipeline([transport.input(), parts.media_gate_in, parts.context.user(), parts.sonic,
        parts.monitor, parts.media_gate_out, transport.output(), parts.context.assistant()])
    task = PipelineTask(pipe, params=PipelineParams(audio_in_sample_rate=settings.engine.audio.pipeline_in_sample_rate,
        audio_out_sample_rate=settings.engine.audio.pipeline_in_sample_rate))
    return pipe, task
