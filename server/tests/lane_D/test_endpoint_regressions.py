"""D-r3 endpoint regressions using the visible ASGI and registry fakes."""

import pytest

from call_engine.conftest import engine, settings, store, trace, line_number
from call_engine.test_line_auth import make_services, start_and_connected
from call_engine._support import AsgiWs, FakeLineSession, HANDOFF_ROUTE, line_ws_path, run_endpoint
from waitless import models as m
from waitless.call.line_ws import line_endpoint
from waitless.call.handoff import HandoffTokens, handoff_endpoint


@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("invalid", [
    "not-json", "[]", "{}", '{"event":"unknown"}',
    '{"event":"clear"}', '{"event":"connected"}',
    '{"event":"connected","protocol":"wrong","version":"1.0.0"}',
    "wrong-order",
])
async def test_invalid_handshake_closes_without_attaching(
    position, invalid, settings, store, trace, fake_clock, fake_carrier, line_number,
):
    session = FakeLineSession("call-a")
    services, tokens = make_services(settings, store, trace, fake_clock, {"call-a": session})
    ws = AsgiWs(path=line_ws_path(settings), query={"token": tokens.issue("call-a")}, services=services)
    messages = start_and_connected(fake_carrier, call_id="call-a", task_id="task-a",
                                  to_number=line_number, stream_url=settings.line_ws_url)
    for index, message in enumerate(messages[:2]):
        if index == position:
            if invalid == "wrong-order":
                ws.json(messages[1 - position])
            else:
                ws.text(invalid)
        else:
            ws.json(message)
    ws.disconnect()
    await run_endpoint(line_endpoint(ws.websocket), ws)
    assert ws.accepted and ws.closed
    assert ws.close_code != m.WS_CLOSE_UNAUTHORIZED
    assert session.attached == []
    assert services.calls.lookups == []


@pytest.mark.parametrize("endpoint", ["line", "handoff"])
async def test_valid_token_for_missing_session_closes_unauthorized(
    endpoint, settings, store, trace, fake_clock, fake_carrier, line_number,
):
    services, tokens = make_services(settings, store, trace, fake_clock, {})
    if endpoint == "line":
        ws = AsgiWs(path=line_ws_path(settings), query={"token": tokens.issue("call-a")}, services=services)
        for message in start_and_connected(fake_carrier, call_id="call-a", task_id="task-a",
                                           to_number=line_number, stream_url=settings.line_ws_url)[:2]:
            ws.json(message)
        coroutine = line_endpoint(ws.websocket)
    else:
        services.handoff_tokens = HandoffTokens(fake_clock, ttl_s=settings.engine.handoff.token_ttl_s)
        token, _ = services.handoff_tokens.issue("call-a", "handoff-a")
        ws = AsgiWs(path=HANDOFF_ROUTE, query={"token": token}, services=services)
        coroutine = handoff_endpoint(ws.websocket, "handoff-a")
    ws.disconnect()
    await run_endpoint(coroutine, ws)
    assert ws.accepted and ws.close_code == m.WS_CLOSE_UNAUTHORIZED
    assert services.calls.lookups == ["call-a"]
    assert not ws.sent_texts()
    assert not fake_clock.sleep_calls
