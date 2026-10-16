"""U92: serializer taps and LiveCall listener lifecycle without side effects."""

import asyncio
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import InterruptionFrame, OutputAudioRawFrame

from call_engine._support import (
    AsgiWs, LiveCallHarness, handoff_sonic, reach_handoff_ready, wait_until,
)
from call_engine.conftest import compiled, engine, gate, line_number, settings, spec, store, trace
from call_engine.test_line_serializer import SID, make_serializer
from waitless.call import line_protocol as lp
from waitless.call.listen import ListenTicketOut
from waitless.interfaces import ToolFailure


class Tap:
    def __init__(self):
        self.inbound, self.outbound, self.cleared = [], [], 0

    def on_line_in(self, pcm, rate):
        self.inbound.append((pcm, rate))

    def on_line_out(self, pcm, rate):
        self.outbound.append((pcm, rate))

    def on_clear(self):
        self.cleared += 1


def media(seq=1):
    return lp.encode_line_message(lp.make_media_in(
        sequence=seq, stream_sid=SID, chunk=seq, timestamp_ms=(seq - 1) * 20,
        ulaw=bytes([255]) * 160,
    ))


async def test_u92_serializer_taps_both_directions(engine):
    ser = await make_serializer(engine)
    tap = Tap()
    ser.listen_hub = tap
    frame = await ser.deserialize(media())
    assert tap.inbound == [(frame.audio, 16000)]
    assert len(tap.inbound) == 1
    out = OutputAudioRawFrame(audio=bytes(3200), sample_rate=16000, num_channels=1)
    assert await ser.serialize(out)
    assert tap.outbound == [(out.audio, 16000)]
    assert await ser.serialize(InterruptionFrame())
    assert tap.cleared == 1


async def test_u92_serializer_without_hub_unchanged(engine):
    plain, tapped = await make_serializer(engine), await make_serializer(engine)
    assert plain.listen_hub is None
    tapped.listen_hub = Tap()
    for seq in range(1, 10):
        left, right = await plain.deserialize(media(seq)), await tapped.deserialize(media(seq))
        assert type(left) is type(right)
        assert left.audio == right.audio
        assert left.sample_rate == right.sample_rate == 16000
    for _ in range(10):
        args = dict(audio=bytes(640), sample_rate=16000, num_channels=1)
        assert await plain.serialize(OutputAudioRawFrame(**args)) == await tapped.serialize(OutputAudioRawFrame(**args))
    assert await plain.serialize(InterruptionFrame()) == await tapped.serialize(InterruptionFrame())


@pytest.fixture
async def h(monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store):
    harness = LiveCallHarness(monkeypatch=monkeypatch, settings=settings, spec=spec,
        clock=fake_clock, trace=trace, gate=gate, sonic=handoff_sonic(make_fake_sonic),
        carrier=fake_carrier, store=store)
    try:
        yield harness
    finally:
        await harness.close()


async def open_listener(h):
    ticket = h.call.listen()
    assert isinstance(ticket, ListenTicketOut)
    assert ticket.task_id == h.spec.task_id and ticket.call_id == h.spec.call_id
    assert ticket.ws_path == f'/ws/listen/{h.spec.call_id}'
    assert h.call.consume_listen_token(ticket.token)
    assert not h.call.consume_listen_token(ticket.token)
    ws = AsgiWs(path=ticket.ws_path)
    await ws.websocket.accept()
    task = asyncio.create_task(h.call.attach_listen(ws.websocket))
    await wait_until(lambda: ws.sent_json(), what='live listener ready')
    return ws, task


async def test_u92_livecall_listen_refused_in_handoff(h, engine):
    await reach_handoff_ready(h, engine)
    assert h.call.status().phase == 'handoff_ready'
    with pytest.raises(ToolFailure) as exc:
        h.call.listen()
    assert exc.value.code == 'listen_unavailable'
    join = await h.call.join()
    ws = await h.open_handoff(join)
    assert h.call.status().phase == 'in_handoff'
    with pytest.raises(ToolFailure) as exc:
        h.call.listen()
    assert exc.value.code == 'listen_unavailable'


async def test_u92_listen_writes_no_trace_and_no_interrupt(h):
    await h.start()
    await h.connect_line()
    before = len(h.trace.events(call_id=h.spec.call_id))
    interrupts = h.call.status().interrupts_used
    ws, task = await open_listener(h)
    serializer = h.transport_builds[0]
    assert serializer.listen_hub is h.call._listen_hub
    serializer.listen_hub.on_line_in(bytes(640), 16000)
    await wait_until(lambda: ws.sent_bytes(), what='listen PCM')
    ws.json({'type': 'stop'})
    await asyncio.wait_for(task, 1)
    assert len(h.trace.events(call_id=h.spec.call_id)) == before
    assert h.call.status().interrupts_used == interrupts


async def test_u92_call_end_closes_listener(h):
    await h.start()
    await h.connect_line()
    ws, task = await open_listener(h)
    await h.call.hangup('test_end')
    await asyncio.wait_for(task, 1)
    assert ws.sent_json()[-1] == {'type': 'ended', 'reason': 'call_ended'}
    assert ws.closed
    with pytest.raises(ToolFailure) as exc:
        h.call.listen()
    assert exc.value.code == 'listen_unavailable'


async def test_u92_attach_refused_without_line_or_after_end(h):
    ws = AsgiWs(path=f'/ws/listen/{h.spec.call_id}')
    await ws.websocket.accept()
    await h.call.attach_listen(ws.websocket)
    assert ws.close_code == 4401
    await h.start()
    await h.connect_line()
    await h.call.hangup('test_end')
    after = AsgiWs(path=f'/ws/listen/{h.spec.call_id}')
    await after.websocket.accept()
    await h.call.attach_listen(after.websocket)
    assert after.close_code == 4401
