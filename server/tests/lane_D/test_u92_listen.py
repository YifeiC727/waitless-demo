"""U92: PCM mixing, bounded read-only streaming and isolated one-use tickets."""

import asyncio
from array import array
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, WebSocket
from pydantic import ValidationError

from call_engine._support import AsgiWs, FakeRegistry, run_app_ws, wait_until
from call_engine.conftest import engine
from waitless.call.handoff import HandoffTokens, handoff_endpoint
from waitless.call.listen import (
    LISTEN_MAX_LAG_MS, LISTEN_QUEUE_FRAMES, ListenHub, ListenMixer,
    ListenTicketOut, listen_endpoint,
)


def pcm(value, samples=320):
    return array('h', [value] * samples).tobytes()


def mixer():
    return ListenMixer(sample_rate=16000, frame_bytes=640, max_lag_ms=LISTEN_MAX_LAG_MS)


def test_u92_mix_sums_both_sides():
    mix = mixer()
    mix.add_line_out(pcm(2000))
    frames = mix.add_line_in(pcm(1000))
    assert len(frames) == 1 and len(frames[0]) == 640
    assert list(array('h', frames[0])) == [3000] * 320


def test_u92_mix_pads_missing_out_with_zero():
    assert mixer().add_line_in(pcm(1000)) == [pcm(1000)]


@pytest.mark.parametrize(('value', 'want'), [(30000, 32767), (-30000, -32768)])
def test_u92_mix_clips(value, want):
    mix = mixer()
    mix.add_line_out(pcm(value))
    assert mix.add_line_in(pcm(value)) == [pcm(want)]


def test_u92_mix_reframes_uneven_chunks():
    mix = mixer()
    assert mix.add_line_in(pcm(1000, 300)) == []
    frames = mix.add_line_in(pcm(1000, 340))
    assert frames == [pcm(1000), pcm(1000)]
    assert [len(frame) for frame in frames] == [640, 640]


def test_u92_out_backlog_is_capped():
    mix = mixer()
    mix.add_line_out(pcm(2000, 16000 * 3))
    mix.add_line_in(pcm(1000))
    assert mix.out_buffered_ms <= LISTEN_MAX_LAG_MS
    assert mix.dropped_out_bytes > 0


def test_u92_clear_drops_pending_out():
    mix = mixer()
    mix.add_line_out(pcm(2000))
    mix.clear_out()
    assert mix.add_line_in(pcm(1000)) == [pcm(1000)]


async def attached(hub):
    ws = AsgiWs(path='/ws/listen/call-u92')
    await ws.websocket.accept()
    task = asyncio.create_task(hub.attach(ws.websocket))
    await wait_until(lambda: ws.sent_json(), what='listen ready')
    return ws, task


async def stop(ws, task):
    ws.json({'type': 'stop'})
    assert await asyncio.wait_for(task, 1) == 'stopped'


async def test_u92_hub_never_blocks():
    hub = ListenHub(media_gate=None, sample_rate=16000)
    ws, task = await attached(hub)
    entered = asyncio.Event()

    async def blocked_send(data):
        entered.set()
        await asyncio.Future()

    ws.websocket.send_bytes = blocked_send
    hub.on_line_in(pcm(1000), 16000)
    await asyncio.wait_for(entered.wait(), 1)

    async def feed():
        for _ in range(500):
            assert hub.on_line_in(pcm(1000), 16000) is None

    try:
        await asyncio.wait_for(feed(), 0.5)
        assert hub._queue.qsize() <= LISTEN_QUEUE_FRAMES
    finally:
        await hub.close('call_ended')
        await asyncio.wait_for(task, 1)


async def test_u92_hub_ends_on_handoff_and_drops_mic():
    gate = SimpleNamespace(in_handoff=False)
    hub = ListenHub(media_gate=gate, sample_rate=16000)
    ws, task = await attached(hub)
    gate.in_handoff = True
    hub.on_line_in(pcm(1000), 16000)
    assert await asyncio.wait_for(task, 1) == 'handoff'
    assert ws.sent_json()[-1] == {'type': 'ended', 'reason': 'handoff'}
    hub.on_line_out(pcm(2000), 16000)
    hub.on_line_in(pcm(1000), 16000)
    await asyncio.sleep(0)
    assert ws.sent_bytes() == []


async def test_u92_ready_then_binary_only():
    hub = ListenHub(media_gate=None, sample_rate=16000)
    ws, task = await attached(hub)
    assert ws.sent_json() == [{'type': 'ready', 'format': 'pcm_s16le', 'sample_rate': 16000, 'channels': 1}]
    hub.on_line_out(pcm(2000) * 6, 16000)
    hub.on_line_in(pcm(1000), 16000)
    await wait_until(lambda: ws.sent_bytes(), what='mixed PCM')
    sends = [msg for msg in ws.sent if msg['type'] == 'websocket.send']
    assert sends[0].get('text') is not None
    assert all(len(msg['bytes']) == 640 for msg in sends[1:])
    assert ws.sent_bytes() == [pcm(3000)]
    await stop(ws, task)
    assert ws.sent_json()[-1] == {'type': 'ended', 'reason': 'stopped'}


async def test_u92_browser_binary_closes_1002():
    hub = ListenHub(media_gate=None, sample_rate=16000)
    ws, task = await attached(hub)
    ws.binary(pcm(900))
    assert await asyncio.wait_for(task, 1) == 'disconnected'
    assert ws.close_code == 1002
    assert ws.sent_bytes() == []


async def test_u92_second_listener_replaces_first():
    hub = ListenHub(media_gate=None, sample_rate=16000)
    first, first_task = await attached(hub)
    second, second_task = await attached(hub)
    assert await asyncio.wait_for(first_task, 1) == 'replaced'
    assert first.sent_json()[-1] == {'type': 'ended', 'reason': 'replaced'}
    hub.on_line_in(pcm(1000), 16000)
    await wait_until(lambda: second.sent_bytes(), what='replacement audio')
    assert first.sent_bytes() == []
    assert second.sent_bytes() == [pcm(1000)]
    await stop(second, second_task)


class TicketSession:
    def __init__(self, clock, engine):
        self.tokens = HandoffTokens(clock, ttl_s=engine.handoff.token_ttl_s)
        self.hub = ListenHub(media_gate=None, sample_rate=16000)

    def consume_listen_token(self, token):
        return self.tokens.consume('call-u92', token) == 'call-u92'

    async def attach_listen(self, websocket):
        return await self.hub.attach(websocket)


def listen_app(session, clock, engine):
    app = FastAPI()
    app.state.listen_origins = ['test-origin']
    app.state.services = SimpleNamespace(
        calls=FakeRegistry({'call-u92': session}),
        handoff_tokens=HandoffTokens(clock, ttl_s=engine.handoff.token_ttl_s),
    )

    @app.websocket('/ws/listen/{call_id}')
    async def listen(websocket: WebSocket, call_id: str):
        await listen_endpoint(websocket, call_id)

    @app.websocket('/ws/handoff/{handoff_id}')
    async def handoff(websocket: WebSocket, handoff_id: str):
        await handoff_endpoint(websocket, handoff_id)

    return app


async def connect(app, token=None, *, origin='test-origin', path='/ws/listen/call-u92'):
    ws = AsgiWs(path=path, query={} if token is None else {'token': token})
    if origin is not None:
        ws.scope['headers'].append((b'origin', origin.encode()))
    ws.json({'type': 'stop'})
    await run_app_ws(app, ws)
    return ws


async def test_u92_listen_ws_token_single_use_and_expiry(fake_clock, engine):
    session = TicketSession(fake_clock, engine)
    app = listen_app(session, fake_clock, engine)
    token, _ = session.tokens.issue('call-u92', 'call-u92')
    first = await connect(app, token)
    assert first.accepted and first.close_code == 1000
    assert first.sent_json()[0]['type'] == 'ready'
    assert (await connect(app, token)).close_code == 4401
    expired, _ = session.tokens.issue('call-u92', 'call-u92')
    fake_clock.advance_sync(engine.handoff.token_ttl_s * 1000 + 1000)
    assert (await connect(app, expired)).close_code == 4401
    assert (await connect(app, 'forged')).close_code == 4401
    assert (await connect(app)).close_code == 4401


async def test_u92_listen_token_cannot_open_handoff(fake_clock, engine):
    session = TicketSession(fake_clock, engine)
    app = listen_app(session, fake_clock, engine)
    token, _ = session.tokens.issue('call-u92', 'call-u92')
    ws = await connect(app, token, path='/ws/handoff/call-u92')
    assert ws.close_code == 4401


@pytest.mark.parametrize('origin', [None, 'other-origin'])
async def test_u92_origin_rejected_without_consuming_token(fake_clock, engine, origin):
    session = TicketSession(fake_clock, engine)
    app = listen_app(session, fake_clock, engine)
    token, _ = session.tokens.issue('call-u92', 'call-u92')
    ws = await connect(app, token, origin=origin)
    assert ws.accepted and ws.close_code == 4401
    assert (await connect(app, token)).sent_json()[0]['type'] == 'ready'


@pytest.mark.parametrize('session', [None, SimpleNamespace()])
async def test_u92_missing_session_or_token_consumer_rejected(fake_clock, engine, session):
    app = listen_app(session, fake_clock, engine)
    assert (await connect(app, 'forged')).close_code == 4401


async def test_u92_wrong_sample_rate_is_dropped_and_counted():
    hub = ListenHub(media_gate=None, sample_rate=16000)
    ws, task = await attached(hub)
    hub.on_line_in(pcm(1000), 8000)
    hub.on_line_out(pcm(2000), 8000)
    hub.on_line_in(pcm(1000), 16000)
    await wait_until(lambda: ws.sent_bytes(), what='valid input')
    assert ws.sent_bytes() == [pcm(1000)]
    assert hub.dropped_in_bytes == 640 and hub.dropped_out_bytes == 640
    await stop(ws, task)


async def test_u92_disconnect_and_repeated_close():
    hub = ListenHub(media_gate=None, sample_rate=16000)
    ws, task = await attached(hub)
    ws.disconnect()
    assert await asyncio.wait_for(task, 1) == 'disconnected'
    await hub.close('call_ended')
    await hub.close('call_ended')


def test_u92_ticket_validates_path_and_forbids_extra():
    fields = dict(task_id='task-u92', call_id='call-u92', ws_path='/ws/listen/call-u92', token='test-token', expires_at='2030-01-01T00:00:00+00:00')
    assert ListenTicketOut(**fields).ws_path == '/ws/listen/call-u92'
    with pytest.raises(ValidationError):
        ListenTicketOut(**(fields | {'ws_path': '/ws/listen/other'}))
    with pytest.raises(ValidationError):
        ListenTicketOut(**fields, extra=True)
