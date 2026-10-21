"""Protocol errors must return to the endpoint's automatic-resume path."""
import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect, WebSocketState

from e_support import (
    FakeHandoffSocket, ScriptedCompany, build_settings, line, make_rig,
    run_until, spec_handoff,
)


async def active_handoff(mode_env, engine_config, fake_clock):
    rig = make_rig(
        settings=build_settings(mode_env("text-sim"), engine_config),
        clock=fake_clock, company=ScriptedCompany([line("Holder required.")]),
        allowed_fields={}, script=[("Holder required", spec_handoff())],
    )
    session = await rig.start()
    await run_until(fake_clock, lambda: session.status().phase == "handoff_ready")
    joined = await session.join()
    return session, joined.handoff_id


@pytest.mark.parametrize("hello", ["binary", "invalid_json", "invalid_model"])
@pytest.mark.parametrize("close_failure", [None, "disconnect", "already_closed"])
async def test_bad_hello_closes_and_allows_return(
    hello, close_failure, mode_env, engine_config, fake_clock,
):
    class Socket(FakeHandoffSocket):
        async def close(self, code=1000, reason=None):
            await super().close(code=code, reason=reason)
            if close_failure == "disconnect":
                raise WebSocketDisconnect()
            if close_failure == "already_closed":
                self.application_state = WebSocketState.DISCONNECTED
                raise RuntimeError("Cannot call send once close was sent")

    session, handoff_id = await active_handoff(mode_env, engine_config, fake_clock)
    ws = Socket()
    if hello == "binary":
        ws.feed_bytes(b"invalid hello")
    elif hello == "invalid_json":
        ws.feed_text("{broken")
    else:
        ws.feed_json({"type": "hello", "format": "pcm_s16le", "sample_rate": 8000, "channels": 1})
    try:
        await asyncio.wait_for(session.attach_handoff(ws, handoff_id), timeout=2)
        assert ws.closed_code == 1002
        assert ws.sent_json() == []
        assert session.status().phase == "in_handoff"
        assert (await session.return_to_agent(handoff_id)).status == "resuming"
    finally:
        await session.hangup("test cleanup")


@pytest.mark.parametrize("stage", ["receive", "close"])
async def test_hello_service_failure_propagates(stage, mode_env, engine_config, fake_clock):
    failure = RuntimeError("service failed")

    class Socket(FakeHandoffSocket):
        async def receive(self):
            if stage == "receive":
                raise failure
            return await super().receive()

        async def close(self, code=1000, reason=None):
            raise failure

    session, handoff_id = await active_handoff(mode_env, engine_config, fake_clock)
    ws = Socket()
    ws.feed_text("invalid JSON")
    try:
        with pytest.raises(RuntimeError) as err:
            await session.attach_handoff(ws, handoff_id)
        assert err.value is failure
        assert session.status().phase == "in_handoff"
    finally:
        await session.hangup("test cleanup")


async def test_disconnect_before_hello_returns(mode_env, engine_config, fake_clock):
    session, handoff_id = await active_handoff(mode_env, engine_config, fake_clock)
    ws = FakeHandoffSocket()
    ws.feed_disconnect()
    try:
        await session.attach_handoff(ws, handoff_id)
        assert ws.closed_code is None and ws.sent_json() == []
        assert session.status().phase == "in_handoff"
    finally:
        await session.hangup("test cleanup")
