"""U102: private user keys on the existing handoff line (E154–E156)."""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import yaml
from loguru import logger
from pipecat.frames.frames import OutputAudioRawFrame, OutputDTMFUrgentFrame

from lane_D.agent_support import agent_call, compiled, engine, line_number, settings, spec, store, trace
from lane_D.agent_support import reach_handoff_ready
from lane_D.test_u109_tool_error_hints import hint_for
from call_engine._support import AsgiWs, Chain, HANDOFF_HELLO, audio_ms, settle, wait_until
from waitless import models as m
from waitless.call.handoff import HandoffBridge
from waitless.call.media_gate import MediaGate
from waitless.config import ConfigError, load_engine_config
from waitless.interfaces import ToolFailure


@pytest.fixture
def capture_pipecat_debug(caplog):
    caplog.set_level(logging.DEBUG)
    sink = logger.add(caplog.handler, level='DEBUG')
    try:
        yield
    finally:
        logger.remove(sink)


async def joined(h):
    await reach_handoff_ready(h, h.settings.engine)
    join = await h.call.join()
    return join, await h.open_handoff(join)


async def keys_reply(ws, keys, *, extra=None):
    before = len(ws.sent_json())
    ws.json({'type': 'dtmf', 'keys': keys, **(extra or {})})
    await wait_until(lambda: len(ws.sent_json()) > before or ws.closed, what='key acknowledgement')
    return ws.sent_json()[-1]


@asynccontextmanager
async def bridge_session(engine, fake_clock, *, hello=True):
    gate = MediaGate(dtmf=engine.dtmf, clock=fake_clock)
    ws = AsgiWs(path='/ws/handoff/test')
    await ws.websocket.accept()
    bridge = HandoffBridge(websocket=ws.websocket, media_gate=gate, company_name='Test company')
    async with Chain(gate.outbound) as chain:
        await gate.enter_handoff()
        task = asyncio.create_task(bridge.run())
        try:
            if hello:
                ws.json(HANDOFF_HELLO)
                await wait_until(lambda: ws.sent_json(), what='ready')
            yield gate, ws, chain, task
        finally:
            await bridge.close('returned')
            await task


async def test_u102_keys_reach_the_line_once_with_tail(agent_call):
    h = agent_call
    await joined(h)
    ws, out, gate = h.handoff_ws, h.transport.output(), h.call._media_gate
    before = len(out.frames)
    dtmf_before = gate.counters.dtmf_frames_out
    assert await keys_reply(ws, '5') == {'type': 'dtmf_ack', 'count': 1}
    await settle(200)
    frames = out.frames[before:]
    assert [f.to_string() for f in frames if isinstance(f, OutputDTMFUrgentFrame)] == ['5']
    key_index = next(i for i, f in enumerate(frames) if isinstance(f, OutputDTMFUrgentFrame))
    tail = [f for f in frames[key_index + 1:] if isinstance(f, OutputAudioRawFrame)]
    assert audio_ms(tail) >= h.settings.engine.dtmf.tail_silence_ms
    assert all(not any(f.audio) and f.sample_rate == m.HANDOFF_SAMPLE_RATE for f in tail)
    assert gate.counters.user_dtmf_keys_out == 1
    assert gate.counters.dtmf_frames_out == dtmf_before


async def test_u102_sonic_and_jev_keys_still_blocked_in_handoff(agent_call):
    h = agent_call
    await joined(h)
    out = h.transport.output()
    before = len(out.frames)
    await h.sonic.push_frame(OutputDTMFUrgentFrame.from_string('2'))
    await h.sonic_says('Assistant audio must be blocked.')
    await h.call._task.queue_frame(OutputDTMFUrgentFrame.from_string('3'))
    await settle(200)
    assert not [f for f in out.frames[before:] if isinstance(f, (OutputAudioRawFrame, OutputDTMFUrgentFrame))]
    assert await keys_reply(h.handoff_ws, '1') == {'type': 'dtmf_ack', 'count': 1}
    await settle(200)
    assert [f.to_string() for f in out.dtmf()] == ['1']


async def test_u102_audio_key_audio_keeps_order(agent_call):
    h = agent_call
    await joined(h)
    ws, gate, out = h.handoff_ws, h.call._media_gate, h.transport.output()
    a, b, c = [bytes([i]) * m.HANDOFF_FRAME_BYTES for i in (1, 2, 3)]
    ws.binary(a)
    assert await keys_reply(ws, '9') == {'type': 'dtmf_ack', 'count': 1}
    ws.binary(b)
    ws.binary(b)
    await settle(200)
    assert gate.counters.user_audio_frames_muted == 2
    duration = h.settings.engine.dtmf.user_mute_ms_per_key + h.settings.engine.dtmf.tail_silence_ms
    await h.clock.advance(duration - 1)
    ws.binary(b)
    await settle(200)
    assert gate.counters.user_audio_frames_muted == 3
    await h.clock.advance(1)
    ws.binary(c)
    await settle(200)
    frames = out.frames
    ai = next(i for i, f in enumerate(frames) if isinstance(f, OutputAudioRawFrame) and f.audio == a)
    ki = next(i for i, f in enumerate(frames) if isinstance(f, OutputDTMFUrgentFrame))
    ci = next(i for i, f in enumerate(frames) if isinstance(f, OutputAudioRawFrame) and f.audio == c)
    assert ai < ki < ci
    assert not any(isinstance(f, OutputAudioRawFrame) and f.audio == b for f in frames)
    assert audio_ms([f for f in frames[ki + 1:ci] if isinstance(f, OutputAudioRawFrame)]) >= h.settings.engine.dtmf.tail_silence_ms


@pytest.mark.parametrize('payload', [
    {'keys': '5a'}, {'keys': ''}, {'keys': '1' * 17}, {'keys': 5}, {},
    {'keys': '5\n'}, {'keys': '５'}, {'keys': '5', 'extra': True},
])
async def test_u102_bad_frames_rejected_without_closing(engine, fake_clock, payload):
    async with bridge_session(engine, fake_clock) as (gate, ws, chain, task):
        ws.json({'type': 'dtmf', **payload})
        await wait_until(lambda: len(ws.sent_json()) == 2, what='invalid keys')
        assert ws.sent_json()[-1] == {'type': 'dtmf_error', 'error': 'invalid_keys'}
        assert not ws.closed and not task.done() and not chain.tail.dtmf()


async def test_u102_bad_frames_rejected_without_closing_rate_and_state(engine, fake_clock):
    async with bridge_session(engine, fake_clock) as (gate, ws, chain, task):
        assert await keys_reply(ws, '1' * 16) == {'type': 'dtmf_ack', 'count': 16}
        await fake_clock.advance(9999)
        assert await keys_reply(ws, '2' * 16) == {'type': 'dtmf_ack', 'count': 16}
        before = len(chain.tail.dtmf())
        assert await keys_reply(ws, '3') == {'type': 'dtmf_error', 'error': 'rate_limited'}
        assert len(chain.tail.dtmf()) == before and not ws.closed
        await fake_clock.advance(1)
        assert await keys_reply(ws, '3') == {'type': 'dtmf_ack', 'count': 1}
        await gate.exit_handoff()
        before = len(chain.tail.dtmf())
        assert await keys_reply(ws, '4') == {'type': 'dtmf_error', 'error': 'not_in_handoff'}
        assert not await gate.outbound.push_user_dtmf('4')
        assert len(chain.tail.dtmf()) == before and not ws.closed


async def test_u102_bad_frames_rejected_without_closing_before_ready(engine, fake_clock):
    async with bridge_session(engine, fake_clock, hello=False) as (gate, ws, chain, task):
        ws.json({'type': 'dtmf', 'keys': '5'})
        await task
        assert ws.close_code == 1002 and not chain.tail.dtmf()


async def test_u102_bad_frames_rejected_without_closing_after_return(agent_call):
    h = agent_call
    join, ws = await joined(h)
    await h.call.hangup('user_hangup')
    await settle(200)
    assert ws.closed and h.handoff_task.done()
    ws.json({'type': 'dtmf', 'keys': '5'})
    await settle(200)
    assert not h.transport.output().dtmf()


@pytest.mark.parametrize('text', ['x' * 300, json.dumps({'type': 'ignored', 'text': '界' * 90}, ensure_ascii=False)])
async def test_u102_oversized_text_frame_is_protocol_error(agent_call, text):
    h = agent_call
    await joined(h)
    h.handoff_ws.text(text)
    await wait_until(lambda: h.handoff_ws.closed, what='oversized frame rejection')
    assert h.handoff_ws.close_code == 1002
    assert not h.transport.output().dtmf()


async def test_u102_keys_never_recorded(agent_call, caplog, capture_pipecat_debug):
    caplog.set_level(logging.DEBUG)
    h = agent_call
    join, ws = await joined(h)
    assert await keys_reply(ws, '86753091#') == {'type': 'dtmf_ack', 'count': 9}
    await h.sonic.emit_user_text('Private handoff speech.')
    assert '86753091' not in h.call.status(debug=True).model_dump_json()
    await h.call.hangup('user_hangup')
    await h.call.hangup('test_end')
    await settle(200)
    assert '86753091' not in h.trace.text_dump()
    assert '86753091' not in json.dumps(vars(h.deps.store), default=str)
    assert '86753091' not in caplog.text
    assert '86753091' not in h.call.status(debug=True).model_dump_json()
    assert all('86753091' not in t for t in h.sonic.sent_texts)
    events = h.events()
    start = next(i for i, e in enumerate(events) if e.event_type == 'handoff_started')
    end = next(i for i, e in enumerate(events) if e.event_type == 'call_ended')
    assert all(e.event_type == 'agent_tool_result' for e in events[start + 1:end])


async def test_u102_listen_still_refused_in_handoff(agent_call):
    h = agent_call
    await joined(h)
    with pytest.raises(ToolFailure) as exc:
        h.call.listen()
    assert exc.value.code == 'listen_unavailable'


async def test_u102_status_carries_handoff_reason(agent_call):
    h = agent_call
    await reach_handoff_ready(h, h.settings.engine)
    reason = h.settings.engine.handoff.notification_text
    assert h.call.status().handoff.reason == reason
    join = await h.call.join()
    await h.open_handoff(join)
    assert h.call.status().handoff.reason == reason
    await h.call.hangup('user_hangup')
    assert h.call.status().handoff.state == 'ended'


def test_u102_contract_fields(tmp_path):
    assert m.HandoffStatus.model_fields['reason'].default is None
    assert 'user_mute_ms_per_key' in m.EngineDtmf.model_fields
    path = Path(__file__).resolve().parents[2] / 'config' / 'engine.yaml'
    value = yaml.safe_load(path.read_text())
    assert value['dtmf']['user_mute_ms_per_key'] > 0
    del value['dtmf']['user_mute_ms_per_key']
    missing = tmp_path / 'engine.yaml'
    missing.write_text(yaml.safe_dump(value))
    with pytest.raises(ConfigError):
        load_engine_config(missing)


async def test_u102_repeated_system_handoff_keeps_notification_and_count(agent_call):
    h = agent_call
    await reach_handoff_ready(h, h.settings.engine)
    reason = h.settings.engine.handoff.notification_text
    decision = next(e for e in h.events('decision') if e.policy_decision['intent'] == 'speaker')
    await h.call.system_handoff({'seq': 1, 'speaker': 'live_human', 'decision_id': decision.decision_id})
    assert h.call.status().handoff.reason == reason
    assert len(h.events('handoff_ready')) == 1 and h.call.status().interrupts_used == 1
