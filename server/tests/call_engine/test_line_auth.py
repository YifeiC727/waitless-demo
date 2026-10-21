"""AC-62：没有有效的每通令牌时，/ws/line 拒绝连接（关闭码 4401）。

机制（tech §7.1 AC-62；缺口 E-G-6 定稿 endpoint_accepts）：拨号时签发每通令牌（LiveCall.start → LineTokens.issue）；
line_endpoint 先 accept，再用 LineTokens.verify 核对令牌，无效就以 models.WS_CLOSE_UNAUTHORIZED 关闭、不建管线；
有效时读完 connected、start，把已 accept 的连接交给 session.attach_line（session 不再 accept）。
端点经 websocket.app.state.services（interfaces.AppServices）找到令牌签发器与通话。
"""

from typing import Any

import pytest

from call_engine._support import (
    AsgiWs,
    FakeLineSession,
    FakeRegistry,
    SimpleHandoffTokens,
    line_ws_path,
    make_deps,
    run_app_ws,
    run_endpoint,
    ws_test_app,
)
from waitless import models as m
from waitless.call import line_protocol as lp
from waitless.interfaces import AppServices


def make_services(settings: Any, store: Any, trace: Any, clock: Any, sessions: dict[str, Any]) -> tuple[AppServices, Any]:
    from waitless.call.line_ws import LineTokens

    tokens = LineTokens(clock)
    services = AppServices(
        settings=settings,
        store=store,
        trace=trace,
        clock=clock,
        calls=FakeRegistry(sessions),
        line_tokens=tokens,
        handoff_tokens=SimpleHandoffTokens(clock, settings.engine.handoff.token_ttl_s),
    )
    return services, tokens


def start_and_connected(fake_carrier: Any, *, call_id: str, task_id: str, to_number: str, stream_url: str) -> list[dict[str, Any]]:
    dial = m.CarrierDialIn(call_id=call_id, task_id=task_id, to_number=to_number, stream_url=stream_url, stream_token="x")
    return fake_carrier.line_messages(dial, fake_carrier.new_call_sid(), include_stop=False)


def test_ac62_line_tokens_issue_verify_revoke(fake_clock: Any) -> None:
    from waitless.call.line_ws import LineTokens

    tokens = LineTokens(fake_clock)
    a = tokens.issue("call-a")
    b = tokens.issue("call-b")
    assert a != b
    assert tokens.verify(a) == "call-a" and tokens.verify(b) == "call-b"
    assert tokens.verify("not-a-token") is None
    assert tokens.verify("") is None
    tokens.revoke("call-a")
    assert tokens.verify(a) is None
    assert tokens.verify(b) == "call-b"


async def test_ac62_line_ws_without_token_is_accepted_then_closed_4401(
    settings: Any, store: Any, trace: Any, fake_clock: Any, fake_carrier: Any, line_number: str
) -> None:
    """经 FastAPI 路由连接（整个 ASGI 应用在测试的事件循环里跑）：没带令牌——先 accept，再以 4401 关闭（E-G-6），
    不交给通话。"""
    session = FakeLineSession("call-a")
    services, _ = make_services(settings, store, trace, fake_clock, {"call-a": session})
    ws = AsgiWs(path=line_ws_path(settings), services=services)
    for msg in start_and_connected(fake_carrier, call_id="call-a", task_id="task-a", to_number=line_number,
                                   stream_url=settings.line_ws_url):
        ws.json(msg)
    ws.disconnect()
    await run_app_ws(ws_test_app(services), ws)
    kinds = [msg["type"] for msg in ws.sent]
    assert "websocket.accept" in kinds and "websocket.close" in kinds
    assert kinds.index("websocket.accept") < kinds.index("websocket.close")
    assert ws.close_code == m.WS_CLOSE_UNAUTHORIZED
    assert session.attached == []


@pytest.mark.parametrize("token_kind", ["missing", "empty", "forged", "revoked"])
async def test_ac62_line_ws_without_valid_token_closes_4401(
    token_kind: str, settings: Any, store: Any, trace: Any, fake_clock: Any, fake_carrier: Any, line_number: str
) -> None:
    from waitless.call.line_ws import line_endpoint

    session = FakeLineSession("call-a")
    services, tokens = make_services(settings, store, trace, fake_clock, {"call-a": session})
    query = {"missing": {}, "empty": {"token": ""}, "forged": {"token": "forged-token"}}.get(token_kind)
    if token_kind == "revoked":
        token = tokens.issue("call-a")
        tokens.revoke("call-a")
        query = {"token": token}
    ws = AsgiWs(path=line_ws_path(settings), query=query, services=services)
    for msg in start_and_connected(fake_carrier, call_id="call-a", task_id="task-a", to_number=line_number, stream_url=settings.line_ws_url):
        ws.json(msg)
    ws.disconnect()
    await run_endpoint(line_endpoint(ws.websocket), ws)
    assert ws.close_code == m.WS_CLOSE_UNAUTHORIZED
    assert session.attached == []


async def test_ac62_line_ws_with_valid_token_hands_start_to_the_call(
    settings: Any, store: Any, trace: Any, fake_clock: Any, fake_carrier: Any, line_number: str
) -> None:
    from waitless.call.line_ws import line_endpoint

    session = FakeLineSession("call-a")
    other = FakeLineSession("call-b")
    services, tokens = make_services(settings, store, trace, fake_clock, {"call-a": session, "call-b": other})
    token = tokens.issue("call-a")
    ws = AsgiWs(path=line_ws_path(settings), query={"token": token}, services=services)
    for msg in start_and_connected(fake_carrier, call_id="call-a", task_id="task-a", to_number=line_number, stream_url=settings.line_ws_url):
        ws.json(msg)
    ws.disconnect()
    await run_endpoint(line_endpoint(ws.websocket), ws)
    assert ws.accepted and ws.close_code != m.WS_CLOSE_UNAUTHORIZED
    acks = [lp.parse_line_message(t, direction="to_carrier") for t in ws.sent_texts()]
    assert [a.mark.name for a in acks if isinstance(a, lp.MarkMsg)] == ["attached"]
    assert len(session.attached) == 1 and other.attached == []
    start = session.attached[0]
    assert isinstance(start, lp.StartMsg)
    assert start.start.customParameters.call_id == "call-a"
    assert services.calls.lookups == ["call-a"]


async def test_ac62_live_call_start_issues_token_and_dials_with_it(
    spec: Any, settings: Any, store: Any, trace: Any, fake_clock: Any, gate: Any, fake_carrier: Any
) -> None:
    from waitless.call.line_ws import LineTokens
    from waitless.call.live import CarrierClient, LiveCall

    tokens = LineTokens(fake_clock)
    deps = make_deps(settings=settings, trace=trace, clock=fake_clock, gate=gate, store=store, line_tokens=tokens)
    call = LiveCall(spec, deps, carrier=CarrierClient(base_url=settings.carrier_url, http=fake_carrier.transport))
    await call.start()
    assert len(fake_carrier.dials) == 1
    dial = fake_carrier.dials[0]
    assert (dial.call_id, dial.task_id, dial.to_number) == (spec.call_id, spec.task_id, spec.to_number)
    assert dial.stream_url == settings.line_ws_url
    assert tokens.verify(dial.stream_token) == spec.call_id
    assert call.status().phase == "dialing"
    assert trace.of_type("call_started", call_id=spec.call_id)
    assert dial.stream_token not in trace.text_dump()  # 令牌不进 trace
