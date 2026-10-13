"""构造 Pipecat 管线与 Nova Sonic（无工具、带续接）（D 线；tech §6.5、inv-1 §1；AC-64、AC-65；K6、K8）。

公开接口（§7）：build_sonic_service(compiled, settings)、build_pipeline(compiled, ws)。
管线顺序（§6.5）：transport.input → MediaGate.in → TurnDetector → Sonic → OutputGuard → MediaGate.out → transport.output
（TranscriptTap 改为订阅 provider 文字事件，见 TappedNovaSonicService 与 call/live.py；S0-r2 V12、歧义表 A-12。
 tech §6.5 的管线图仍画着 TranscriptTap 这一格，待设计下一版改一笔）。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

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
        raise NotImplementedError

    async def _handle_content_end_event(self, event_json: Any) -> None:
        # todo(D): 先取 self._content_being_received（role、type、text_stage、text_content）与 stopReason，
        #          对 TEXT 内容块发 ProviderTextEvent，再 await super()._handle_content_end_event(event_json)。
        #          依据 pipecat-ai==1.12.0 llm.py:1501-1569；版本由 injector 构造时检查。
        raise NotImplementedError


def build_sonic_service(compiled: CompiledCall, settings: Settings) -> TappedNovaSonicService:
    """不传 tools、不调 register_function（AC-64）；session_continuation=SessionContinuationParams(enabled=True)（AC-65）。"""
    # todo(D): model=settings.sonic_model_id、voice=settings.sonic_voice、region=settings.aws_region、
    #          凭证取 settings 的 SecretStr；system_instruction=compiled.sonic_prompt。
    # todo(D): 主线按 ≤300 s 设计，续接阈值用 SDK 默认（360 s），只作兜底（R22、K8）。
    raise NotImplementedError


def build_transport(websocket: Any, serializer: Any, settings: Settings) -> Any:
    """FastAPIWebsocketTransport：audio_in/out 启用，serializer=LineSerializer（auto_hang_up=False），不加 WAV 头。

    缺口 D-G-9（定稿 out_16k；K6）：输出采样率固定取 settings.engine.audio.pipeline_in_sample_rate（16 kHz）——
    转接时用户音频照 §6.4 以 16 kHz 直接进 transport，Sonic 的 24 kHz 由 transport 统一降到 16 kHz，serializer 的
    输入率恒为 16 kHz。Pipecat 的流式重采样器（SOXRStreamAudioResampler）不能中途换输入采样率，所以进 transport
    的音频只能有这两种固定来源。D-G-6：LiveCall 经本模块属性调用本函数与 build_sonic_service，测试会替换它们。"""
    raise NotImplementedError


@dataclass
class LivePipelineParts:
    """一通 live 电话的处理器（由 LiveCall 构造并持有引用）。"""

    media_gate_in: Any
    turn_detector: Any
    sonic: TappedNovaSonicService
    output_guard: Any
    media_gate_out: Any


def build_pipeline(compiled: CompiledCall, ws: Any, *, settings: Settings, parts: LivePipelineParts) -> tuple[Any, Any]:
    """返回 (Pipeline, PipelineTask)。PipelineParams(audio_in_sample_rate=engine.audio.pipeline_in_sample_rate)（S0 V5）。
    输入 16 kHz、Sonic 输出 24 kHz、线路 8 kHz μ-law（K6）。"""
    raise NotImplementedError
