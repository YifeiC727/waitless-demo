"""AC-64、AC-65：Nova Sonic 服务的构造；SonicTap 发出的 provider 文字事件（AC-66、AC-72 的取数来源）；
以及线路 transport 的输出采样率（缺口 D-G-9，AC-76 的同一条线路要同时承载 Sonic 与用户音频）。

AC-64 / D67：live 构造服务时传空 list；不注册工具。
AC-65：构造时显式传入 session_continuation=SessionContinuationParams(enabled=True)。
测试用替身截获父类构造参数（tech §7.1 AC-65「测试用替身类截获构造参数」），不连 AWS。
"""

from types import SimpleNamespace
from typing import Any

import pytest
from pipecat.adapters.services.aws_nova_sonic_adapter import Role
from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService, ContentType, CurrentContent, TextStage
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.aws.nova_sonic.session_continuation import SessionContinuationParams

from call_engine._support import AsgiWs, line_ws_path
from waitless import models as m


@pytest.fixture
def sonic_ctor(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """截获 AWSNovaSonicLLMService.__init__ 的关键字参数与 register_function 调用，然后照常构造。"""
    seen = SimpleNamespace(kwargs=[], registered=[])
    original_init = AWSNovaSonicLLMService.__init__
    original_register = AWSNovaSonicLLMService.register_function

    def init_spy(self: Any, *args: Any, **kwargs: Any) -> None:
        seen.kwargs.append(dict(kwargs))
        original_init(self, *args, **kwargs)

    def register_spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        seen.registered.append(args[0] if args else kwargs.get("function_name"))
        return original_register(self, *args, **kwargs)

    monkeypatch.setattr(AWSNovaSonicLLMService, "__init__", init_spy)
    monkeypatch.setattr(AWSNovaSonicLLMService, "register_function", register_spy)
    return seen


def build(compiled: Any, settings: Any) -> Any:
    from waitless.call.pipeline import build_sonic_service

    from lane_D.agent_support import tool_schema
    return build_sonic_service(compiled, settings, tools=tool_schema(compiled))


def test_ac64_sonic_service_has_no_live_tools(sonic_ctor: SimpleNamespace, compiled: Any, settings: Any) -> None:
    from waitless.call.pipeline import TappedNovaSonicService

    svc = build(compiled, settings)
    assert isinstance(svc, TappedNovaSonicService) and isinstance(svc, AWSNovaSonicLLMService)
    assert len(sonic_ctor.kwargs) == 1
    assert sonic_ctor.kwargs[0]['tools'] == []  # Even an old caller cannot enable fallback tools.
    assert sonic_ctor.registered == []


def test_ac65_sonic_service_has_session_continuation_enabled(
    sonic_ctor: SimpleNamespace, compiled: Any, settings: Any
) -> None:
    build(compiled, settings)
    sc = sonic_ctor.kwargs[0].get("session_continuation")
    assert isinstance(sc, SessionContinuationParams), "session_continuation must be passed explicitly"
    assert sc.enabled is True


# ---------------------------------------------------------------------------
# SonicTap：provider contentEnd 时发 ProviderTextEvent（S0-r2 V8、V12；A-12）
# ---------------------------------------------------------------------------


async def end_content(svc: Any, *, role: Role, kind: ContentType, stage: TextStage | None, text: str | None, stop: str) -> None:
    """模拟 Sonic 的一个内容块结束：骨架约定 TappedNovaSonicService 覆盖 _handle_content_end_event，
    在调用父类之前读 _content_being_received（pipecat-ai==1.12.0 llm.py:1501-1569）。"""
    svc._content_being_received = CurrentContent(type=kind, role=role, text_stage=stage, text_content=text)
    await svc._handle_content_end_event({"contentEnd": {"stopReason": stop}})


async def test_ac66_tap_emits_user_final_even_with_partial_turn(compiled: Any, settings: Any) -> None:
    svc = build(compiled, settings)
    events: list[m.ProviderTextEvent] = []

    async def listener(ev: m.ProviderTextEvent) -> None:
        events.append(ev)

    svc.add_text_listener(listener)
    await end_content(svc, role=Role.USER, kind=ContentType.TEXT, stage=TextStage.FINAL, text="For billing, press 1.", stop="PARTIAL_TURN")
    assert len(events) == 1
    ev = events[0]
    assert (ev.role, ev.stage, ev.text, ev.stop_reason) == ("USER", "FINAL", "For billing, press 1.", "PARTIAL_TURN")
    assert svc._content_being_received is None  # 父类的处理照常执行（它会清掉当前内容块）


async def test_ac72_tap_emits_assistant_final_and_speculative(compiled: Any, settings: Any) -> None:
    svc = build(compiled, settings)
    events: list[m.ProviderTextEvent] = []

    async def listener(ev: m.ProviderTextEvent) -> None:
        events.append(ev)

    svc.add_text_listener(listener)
    await end_content(svc, role=Role.ASSISTANT, kind=ContentType.TEXT, stage=TextStage.SPECULATIVE, text="One moment please.", stop="PARTIAL_TURN")
    await end_content(svc, role=Role.ASSISTANT, kind=ContentType.AUDIO, stage=None, text=None, stop="END_TURN")
    await end_content(svc, role=Role.ASSISTANT, kind=ContentType.TEXT, stage=TextStage.FINAL, text="One moment please.", stop="END_TURN")
    assert [(e.role, e.stage, e.text) for e in events] == [
        ("ASSISTANT", "SPECULATIVE", "One moment please."),
        ("ASSISTANT", "FINAL", "One moment please."),
    ]


def test_ac76_line_transport_outputs_at_the_pipeline_rate(settings: Any, engine: Any) -> None:
    """缺口 D-G-9（定稿 out_16k）：transport 输出采样率固定取 engine.audio.pipeline_in_sample_rate——转接时用户音频
    以 16 kHz 直接进 transport，Sonic 的 24 kHz 由 transport 降到 16 kHz；serializer 是传入的 LineSerializer，不加 WAV 头。
    （断言读 FastAPIWebsocketTransport 的构造参数 _params：pipecat 没有公开的读取入口。）"""
    from waitless.call.line_ws import LineSerializer
    from waitless.call.pipeline import build_transport

    rate = engine.audio.pipeline_in_sample_rate
    serializer = LineSerializer(stream_sid="MZ" + "5" * 32,
                                params=TwilioFrameSerializer.InputParams(sample_rate=rate, auto_hang_up=False))
    ws = AsgiWs(path=line_ws_path(settings))
    transport = build_transport(ws.websocket, serializer, settings)
    params = transport._params
    assert params.audio_out_sample_rate == rate
    assert params.audio_in_enabled is True and params.audio_out_enabled is True
    assert params.serializer is serializer
    assert not params.add_wav_header
