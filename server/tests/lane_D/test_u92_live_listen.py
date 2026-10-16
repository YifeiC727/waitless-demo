"""U92: serializer taps and LiveCall listener lifecycle without side effects."""

import asyncio
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import InterruptionFrame, OutputAudioRawFrame
from pipecat.serializers.twilio import TwilioFrameSerializer

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
    # The parent's streaming resampler buffers the first few 20 ms chunks.
    frame = None
    for seq in range(1, 10):
        frame = await ser.deserialize(media(seq))
        if frame is not None:
            break
        assert tap.inbound == []
    assert frame is not None
    assert tap.inbound == [(frame.audio, 16000)]
    assert len(tap.inbound) == 1
    for _ in range(15):
        out = OutputAudioRawFrame(audio=bytes(640), sample_rate=16000, num_channels=1)
        if await ser.serialize(out):
            break
        assert tap.outbound == []
    else:
        pytest.fail('streaming resampler produced no outbound payload')
    assert tap.outbound == [(out.audio, 16000)]
    assert await ser.serialize(InterruptionFrame())
    assert tap.cleared == 1


async def test_u92_serializer_without_hub_unchanged(engine, monkeypatch):
    # Independent soxr streams can dither PCM differently. Compare each return
    # with its own actual parent result, including buffering None returns.
    parent_returns = {}
    deserialize = TwilioFrameSerializer.deserialize
    serialize = TwilioFrameSerializer.serialize

    async def parent_in(self, data):
        parent_returns['in'] = await deserialize(self, data)
        return parent_returns['in']

    async def parent_out(self, frame):
        parent_returns['out'] = await serialize(self, frame)
        return parent_returns['out']

    monkeypatch.setattr(TwilioFrameSerializer, 'deserialize', parent_in)
    monkeypatch.setattr(TwilioFrameSerializer, 'serialize', parent_out)
    plain, tapped = await make_serializer(engine), await make_serializer(engine)
    assert plain.listen_hub is None
    tapped.listen_hub = Tap()
    for ser in (plain, tapped):
        incoming, outgoing = [], []
        for seq in range(1, 10):
            frame = await ser.deserialize(media(seq))
            assert frame is parent_returns['in']
            if frame is not None:
                assert frame.sample_rate == 16000
                incoming.append(frame.audio)
        for _ in range(15):
            args = dict(audio=bytes(640), sample_rate=16000, num_channels=1)
            payload = await ser.serialize(OutputAudioRawFrame(**args))
            assert payload is parent_returns['out']
            if payload:
                outgoing.append(payload)
        assert incoming and outgoing
        assert await ser.serialize(InterruptionFrame()) is parent_returns['out']


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
