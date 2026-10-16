"""I2B-2: real line transport disconnects and failed carrier hangups finish calls."""
import asyncio

import httpx
import pytest
from pipecat.frames.frames import CancelFrame, EndFrame, StartFrame

from call_engine.conftest import compiled, engine, gate, line_number, settings, spec, store, trace
from call_engine._support import (
    AsgiWs, FakeCarrierApi, FakePipelineTask, line_ws_path, wait_until,
)
from waitless.call import pipeline
from waitless.call.line_protocol import StartMsg
from waitless.interfaces import ToolFailure


from lane_D.agent_support import AgentCallHarness as LiveCallHarness

async def connect_real_line(h, monkeypatch):
    """Use the actual Pipecat WebSocket transport, with the public fake Sonic."""
    # LiveCallHarness replaced the constructors; get the real transport class directly.
    def build_transport(ws, serializer, settings):
        h.transport = pipeline.FastAPIWebsocketTransport(ws, pipeline.FastAPIWebsocketParams(
            audio_in_enabled=True, audio_out_enabled=True,
            audio_in_sample_rate=settings.engine.audio.pipeline_in_sample_rate,
            audio_out_sample_rate=settings.engine.audio.pipeline_in_sample_rate,
            serializer=serializer, add_wav_header=False))
        return h.transport

    monkeypatch.setattr(pipeline, "build_transport", build_transport)
    frames = []
    process = h.sonic.process_frame

    async def capture(frame, direction):
        frames.append(frame)
        await process(frame, direction)

    monkeypatch.setattr(h.sonic, "process_frame", capture)
    await h.start()
    messages = h.carrier.line_messages(h.dial, h.call_sid, include_stop=False)
    h.start_msg = StartMsg.model_validate(messages[1])
    h.line_ws = AsgiWs(path=line_ws_path(h.settings), services=h.services,
        query={"token": h.dial.stream_token})
    await h.line_ws.websocket.accept()
    h.line_task = asyncio.create_task(h.call.attach_line(h.line_ws.websocket, h.start_msg))
    await wait_until(lambda: any(isinstance(f, StartFrame) for f in frames) or h.line_task.done(),
        what="real transport pipeline start")
    if h.line_task.done():
        h.line_task.result()
    return frames


@pytest.fixture
def harness(monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store):
    return LiveCallHarness(monkeypatch=monkeypatch, settings=settings, spec=spec,
        clock=fake_clock, trace=trace, gate=gate, sonic=make_fake_sonic(),
        carrier=fake_carrier, store=store)


@pytest.mark.parametrize("code", [1000, 1012])
async def test_i2b2_disconnect_without_stop_cancels_pipeline_once(harness, monkeypatch, code):
    h = harness
    try:
        frames = await connect_real_line(h, monkeypatch)
        h.line_ws.disconnect(code)
        await wait_until(lambda: h.call.ended, what="call ended after line disconnect")
        await asyncio.wait_for(asyncio.shield(h.line_task), 2)
        assert any(isinstance(f, CancelFrame) for f in frames)
        assert h.on_ended.calls == [h.spec.call_id]
        await h.call.hangup("already_ended")
        assert h.on_ended.calls == [h.spec.call_id]
    finally:
        await h.close()


@pytest.mark.parametrize("error", [httpx.ConnectError("carrier unavailable"), ToolFailure("unknown_call")])
async def test_i2b2_failed_hangup_still_cancels_and_reraises(harness, monkeypatch, error):
    h = harness
    try:
        frames = await connect_real_line(h, monkeypatch)
        original_hangup = h.call.carrier.hangup

        async def fail(call_sid):
            raise error

        monkeypatch.setattr(h.call.carrier, "hangup", fail)
        try:
            with pytest.raises(type(error)) as caught:
                await h.call.hangup("shutdown")
            assert caught.value is error
            assert h.call.ended
            await asyncio.wait_for(asyncio.shield(h.line_task), 2)
            assert any(isinstance(f, CancelFrame) for f in frames)
            assert h.on_ended.calls == [h.spec.call_id]
            await h.call.hangup("again")
            assert h.on_ended.calls == [h.spec.call_id]
        finally:
            monkeypatch.setattr(h.call.carrier, "hangup", original_hangup)
    finally:
        await h.close()


async def test_i2b2_stop_then_disconnect_notifies_once(harness, monkeypatch):
    h = harness
    try:
        frames = await connect_real_line(h, monkeypatch)
        stop = h.carrier.line_messages(h.dial, h.call_sid, include_stop=True)[-1]
        h.line_ws.json(stop)
        await wait_until(lambda: any(isinstance(f, EndFrame) for f in frames), what="stop EndFrame at Sonic")
        h.line_ws.disconnect()
        await asyncio.wait_for(asyncio.shield(h.line_task), 2)
        assert h.call.ended
        assert any(isinstance(f, EndFrame) for f in frames)
        assert h.on_ended.calls == [h.spec.call_id]
        await h.call.hangup("again")
        assert h.on_ended.calls == [h.spec.call_id]
    finally:
        await h.close()


async def test_i2b2_disconnect_callback_returns_before_finish_waits(harness, monkeypatch):
    h = harness
    released = asyncio.Event()
    entered = asyncio.Event()
    try:
        await connect_real_line(h, monkeypatch)
        finish = h.call._finish

        async def slow_finish(call_id):
            entered.set()
            await released.wait()
            await finish(call_id)

        monkeypatch.setattr(h.call, "_finish", slow_finish)
        # Invoke the transport's callback directly; it must not await pipeline cancellation.
        handlers = h.transport._event_handlers["on_client_disconnected"].handlers
        assert handlers, "no disconnect lifecycle handler registered"
        await asyncio.wait_for(handlers[-1](h.transport, h.line_ws.websocket), 1)
        await asyncio.wait_for(entered.wait(), 1)
        assert not h.call.ended
        released.set()
        await asyncio.wait_for(asyncio.shield(h.line_task), 2)
        assert h.on_ended.calls == [h.spec.call_id]
    finally:
        released.set()
        await h.close()


async def test_i2b2_failed_hangup_without_a_line_finishes_once(harness, monkeypatch):
    h = harness
    await h.start()
    error = httpx.ConnectError("carrier unavailable before line attachment")
    carrier = FakeCarrierApi()
    monkeypatch.setattr(h.call, "carrier", carrier)

    async def fail(call_sid):
        raise error

    monkeypatch.setattr(carrier, "hangup", fail)

    class CancelTask(FakePipelineTask):
        async def cancel(self):
            await self.queue_frame(CancelFrame())

    task = CancelTask()
    h.call._task = task
    try:
        with pytest.raises(httpx.ConnectError) as caught:
            await h.call.hangup("shutdown")
        assert caught.value is error and h.call.ended
        assert len(task.frames) == 1 and isinstance(task.frames[0], CancelFrame)
        assert h.on_ended.calls == [h.spec.call_id]
        await h.call.hangup("again")
        assert len(task.frames) == 1 and h.on_ended.calls == [h.spec.call_id]
    finally:
        monkeypatch.setattr(h.call, "carrier", h.carrier_client)
        await h.close()
