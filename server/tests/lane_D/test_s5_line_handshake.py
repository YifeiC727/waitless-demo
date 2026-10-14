"""D-s5: reject binary protocol handshakes before session lookup."""

import pytest

from call_engine.conftest import engine, settings, store, trace, line_number
from call_engine.test_line_auth import make_services, start_and_connected
from call_engine._support import AsgiWs, FakeLineSession, line_ws_path, run_endpoint
from waitless.call.line_ws import line_endpoint
from starlette.status import WS_1002_PROTOCOL_ERROR


@pytest.mark.parametrize("position", [0, 1])
async def test_binary_handshake_closes_without_reading_next_message(
    position, settings, store, trace, fake_clock, fake_carrier, line_number,
):
    session = FakeLineSession("call-a")
    services, tokens = make_services(settings, store, trace, fake_clock, {"call-a": session})
    ws = AsgiWs(path=line_ws_path(settings), query={"token": tokens.issue("call-a")}, services=services)
    if position:
        ws.json(start_and_connected(fake_carrier, call_id="call-a", task_id="task-a",
                                   to_number=line_number, stream_url=settings.line_ws_url)[0])
    ws.binary(b"invalid handshake")
    # No following start/disconnect: the endpoint must reject this frame immediately.
    await run_endpoint(line_endpoint(ws.websocket), ws)
    assert ws.accepted and ws.close_code == WS_1002_PROTOCOL_ERROR
    assert not services.calls.lookups
    assert not session.attached
