"""D-r3 endpoint regressions using the visible ASGI and registry fakes."""

import pytest

from call_engine.conftest import engine, settings, store, trace, line_number
from call_engine.test_line_auth import make_services, start_and_connected
from call_engine._support import AsgiWs, FakeLineSession, line_ws_path, run_endpoint
from waitless import models as m
from waitless.call.line_ws import line_endpoint


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
