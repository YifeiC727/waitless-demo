"""D-s5: protocol failures close the browser socket and retain grace recovery."""

import asyncio

import pytest
from starlette.status import WS_1002_PROTOCOL_ERROR

from call_engine.conftest import compiled, deps, engine, gate, line_number, settings, spec, store, trace
from call_engine._support import AsgiWs, HANDOFF_HELLO, reach_handoff_ready, settle, wait_until
from call_engine.test_resume import harness, RESUME
from waitless.call.handoff import handoff_endpoint


@pytest.mark.parametrize("invalid", ["hello-json", "hello-binary", "pcm-size", "control-json"])
async def test_invalid_handoff_frame_closes_and_resumes_after_grace(
    invalid, monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store, engine,
):
    h = harness(monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store)
    try:
        await reach_handoff_ready(h, engine)
        join = await h.call.join()
        ws = AsgiWs(path=join.ws_path, query={"token": join.token}, services=h.services)
        h.handoff_ws = ws
        h.handoff_task = asyncio.create_task(handoff_endpoint(ws.websocket, join.handoff_id))
        if invalid == "hello-json":
            ws.json({"type": "hello"})
        elif invalid == "hello-binary":
            ws.binary(b"not a hello")
        else:
            ws.json(HANDOFF_HELLO)
            if invalid == "pcm-size":
                ws.binary(b"\x00\x00")
            else:
                ws.text("not-json")
        await wait_until(lambda: ws.closed or h.handoff_task.done(), what="protocol rejection")
        if h.handoff_task.done():
            h.handoff_task.result()
        await settle()
        assert ws.close_code == WS_1002_PROTOCOL_ERROR
        assert h.call.status().phase == "in_handoff"
        await h.silence(engine.handoff.resume_grace_ms - engine.vad.frame_ms)
        assert RESUME not in h.sonic.sent_texts
        await h.silence(2 * engine.vad.frame_ms)
        await wait_until(h.handoff_task.done, what="automatic return after protocol error")
        h.handoff_task.result()
        assert h.call.status().phase == "resuming"
        assert not h.call._media_gate.in_handoff
        assert h.sonic.sent_texts.count(RESUME) == 1
        assert len(trace.of_type("handoff_ended", call_id=spec.call_id)) == 1
    finally:
        await h.close()
