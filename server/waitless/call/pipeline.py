"""Build the live phone-agent pipeline and Sonic service with session continuation."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
import time
from pipecat.services.aws.nova_sonic.llm import ContentType
from pipecat.services.aws.nova_sonic.session_continuation import SessionContinuationParams
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

    def add_text_listener(self, listener: Callable[[ProviderTextEvent], Awaitable[None]]) -> None:
        if not hasattr(self, "_text_listeners"):
            self._text_listeners = []
        self._text_listeners.append(listener)

    async def _report_assistant_response_started(self) -> None:
        self._waitless_response = getattr(self, "_waitless_response", 0) + 1
        await super()._report_assistant_response_started()

    async def _handle_content_end_event(self, event_json: Any) -> None:
        # todo(D): 先取 self._content_being_received（role、type、text_stage、text_content）与 stopReason，
        #          对 TEXT 内容块发 ProviderTextEvent，再 await super()._handle_content_end_event(event_json)。
        #          依据 pipecat-ai==1.12.0 llm.py:1501-1569；版本由 injector 构造时检查。
        content = self._content_being_received
        if content and content.type == ContentType.TEXT and content.role.value in ("USER", "ASSISTANT"):
            clock = getattr(self, "_waitless_clock", None)
            ev = ProviderTextEvent(role=content.role.value, stage=content.text_stage.value if content.text_stage else None,
                text=content.text_content or "", stop_reason=event_json["contentEnd"].get("stopReason"),
                t_ms=clock.monotonic_ms() if clock else int(time.monotonic() * 1000),
                response=getattr(self, "_waitless_response", 0) or None)
            for listener in tuple(getattr(self, "_text_listeners", ())):
                await listener(ev)
        await super()._handle_content_end_event(event_json)


def build_sonic_service(compiled: CompiledCall, settings: Settings, *, tools) -> TappedNovaSonicService:
    """Pass the skill prompt and tools unchanged; keep session continuation enabled."""
    for name in ("sonic_model_id", "sonic_voice", "aws_region", "aws_access_key_id", "aws_secret_access_key"):
        if getattr(settings, name, None) is None:
            raise ValueError(f"missing {name}")
    return TappedNovaSonicService(model=settings.sonic_model_id, voice_id=settings.sonic_voice, region=settings.aws_region,
        access_key_id=settings.aws_access_key_id.get_secret_value(), secret_access_key=settings.aws_secret_access_key.get_secret_value(),
        system_instruction=compiled.sonic_prompt, tools=tools, session_continuation=SessionContinuationParams(enabled=True))


def build_transport(websocket: Any, serializer: Any, settings: Settings) -> Any:
    """FastAPIWebsocketTransport：audio_in/out 启用，serializer=LineSerializer（auto_hang_up=False），不加 WAV 头。

    缺口 D-G-9（定稿 out_16k；K6）：输出采样率固定取 settings.engine.audio.pipeline_in_sample_rate（16 kHz）——
    转接时用户音频照 §6.4 以 16 kHz 直接进 transport，Sonic 的 24 kHz 由 transport 统一降到 16 kHz，serializer 的
    输入率恒为 16 kHz。Pipecat 的流式重采样器（SOXRStreamAudioResampler）不能中途换输入采样率，所以进 transport
    的音频只能有这两种固定来源。D-G-6：LiveCall 经本模块属性调用本函数与 build_sonic_service，测试会替换它们。"""
    return FastAPIWebsocketTransport(websocket, FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True,
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
    pipe = Pipeline([transport.input(), parts.media_gate_in, parts.context.user(), parts.sonic,
        parts.monitor, parts.media_gate_out, transport.output(), parts.context.assistant()])
    task = PipelineTask(pipe, params=PipelineParams(audio_in_sample_rate=settings.engine.audio.pipeline_in_sample_rate,
        audio_out_sample_rate=settings.engine.audio.pipeline_in_sample_rate))
    return pipe, task
