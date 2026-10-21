"""AC-63：/ws/handoff 令牌只能用一次；过期或重复使用时拒绝（4401）。

机制（tech §7.1 AC-63；缺口 E-G-6 定稿 endpoint_accepts）：join_call 签发一次性令牌（HandoffTokens.issue），
TTL = engine.handoff.token_ttl_s，用过就作废（consume）；handoff_endpoint 先 accept 再核对令牌，失败以
models.WS_CLOSE_UNAUTHORIZED 关闭；成功时把已 accept 的连接交给 session.attach_handoff。
"""

from datetime import datetime, timedelta
from typing import Any

import pytest

from call_engine._support import (
    HANDOFF_HELLO,
    AsgiWs,
    FakeHandoffSession,
    FakeRegistry,
    handoff_path,
    SimpleLineTokens,
    run_app_ws,
    run_endpoint,
    ws_test_app,
)
from waitless import models as m
from waitless.interfaces import AppServices


def handoff_tokens(clock: Any, engine: Any) -> Any:
    from waitless.call.handoff import HandoffTokens

    return HandoffTokens(clock, ttl_s=engine.handoff.token_ttl_s)


def services_with(settings: Any, store: Any, trace: Any, clock: Any, tokens: Any, sessions: dict[str, Any]) -> AppServices:
    return AppServices(
        settings=settings,
        store=store,
        trace=trace,
        clock=clock,
        calls=FakeRegistry(sessions),
        line_tokens=SimpleLineTokens(),
        handoff_tokens=tokens,
    )


def test_ac63_token_is_single_use(fake_clock: Any, engine: Any) -> None:
    tokens = handoff_tokens(fake_clock, engine)
    token, _ = tokens.issue("call-a", "handoff-1")
    assert tokens.consume("handoff-1", token) == "call-a"
    assert tokens.consume("handoff-1", token) is None


def test_ac63_token_expires_after_ttl(fake_clock: Any, engine: Any) -> None:
    ttl_ms = engine.handoff.token_ttl_s * 1000
    tokens = handoff_tokens(fake_clock, engine)
    issued_at = fake_clock.now()
    early, expires_at = tokens.issue("call-a", "handoff-1")
    late, _ = tokens.issue("call-a", "handoff-2")
    expires = datetime.fromisoformat(expires_at)
    assert expires.tzinfo is not None
    assert abs(expires - (issued_at + timedelta(milliseconds=ttl_ms))) <= timedelta(seconds=1)
    fake_clock.advance_sync(ttl_ms - 1000)
    assert tokens.consume("handoff-1", early) == "call-a"
    fake_clock.advance_sync(2000)
    assert tokens.consume("handoff-2", late) is None


def test_ac63_token_is_bound_to_its_handoff_id(fake_clock: Any, engine: Any) -> None:
    tokens = handoff_tokens(fake_clock, engine)
    token, _ = tokens.issue("call-a", "handoff-1")
    assert tokens.consume("handoff-2", token) is None
    assert tokens.consume("handoff-1", "forged") is None


async def test_ac63_handoff_ws_refuses_reused_token_with_4401(
    settings: Any, store: Any, trace: Any, fake_clock: Any, engine: Any
) -> None:
    """经 FastAPI 路由连接：第一次成功（会话收到已 accept 的连接），同一令牌第二次以 4401 关闭。"""
    tokens = handoff_tokens(fake_clock, engine)
    session = FakeHandoffSession()
    services = services_with(settings, store, trace, fake_clock, tokens, {"call-a": session})
    app = ws_test_app(services)
    token, _ = tokens.issue("call-a", "handoff-1")
    first = AsgiWs(path=handoff_path("handoff-1"), query={"token": token}, services=services)
    first.json(HANDOFF_HELLO)
    first.disconnect()
    await run_app_ws(app, first)
    assert first.accepted and first.close_code != m.WS_CLOSE_UNAUTHORIZED
    assert [j["type"] for j in first.sent_json() if isinstance(j, dict)] == ["ready"]
    assert session.attached == ["handoff-1"]
    again = AsgiWs(path=handoff_path("handoff-1"), query={"token": token}, services=services)
    again.json(HANDOFF_HELLO)
    again.disconnect()
    await run_app_ws(app, again)
    assert again.close_code == m.WS_CLOSE_UNAUTHORIZED
    assert session.attached == ["handoff-1"]


@pytest.mark.parametrize("case", ["expired", "forged", "missing"])
async def test_ac63_handoff_ws_refuses_expired_or_invalid_token_with_4401(
    case: str, settings: Any, store: Any, trace: Any, fake_clock: Any, engine: Any
) -> None:
    from waitless.call.handoff import handoff_endpoint

    tokens = handoff_tokens(fake_clock, engine)
    session = FakeHandoffSession()
    services = services_with(settings, store, trace, fake_clock, tokens, {"call-a": session})
    token, _ = tokens.issue("call-a", "handoff-1")
    if case == "expired":
        fake_clock.advance_sync(engine.handoff.token_ttl_s * 1000 + 1000)
        query = {"token": token}
    elif case == "forged":
        query = {"token": token + "x"}
    else:
        query = {}
    ws = AsgiWs(path=handoff_path("handoff-1"), query=query, services=services)
    ws.json(HANDOFF_HELLO)
    ws.disconnect()
    await run_endpoint(handoff_endpoint(ws.websocket, "handoff-1"), ws)
    assert ws.close_code == m.WS_CLOSE_UNAUTHORIZED
    assert session.attached == []
