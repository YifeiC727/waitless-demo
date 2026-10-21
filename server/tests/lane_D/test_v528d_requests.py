"""F1: real LiveCall, TranscriptTap, policy and MediaGate; offline Jev wire only."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from call_engine._support import settle
from lane_D.test_u119_speaker import live119, reply, response_model
from lane_D.agent_support import agent_call, settings, spec, engine, compiled, line_number, store, trace

async def boot_unique(h):
    from uuid import uuid4
    previous = h.spec.call_id
    h.spec = h.spec.model_copy(update={'call_id': uuid4().hex})
    h.call.spec = h.spec
    h.services.calls.sessions[h.spec.call_id] = h.services.calls.sessions.pop(previous)
    h.component_tools = False
    await h.boot()
    return h


MAIN = {'keypad', 'speaker'}

class SplitWire:
    def __init__(self):
        self.entered = {k: asyncio.Event() for k in ('main', 'transfer')}
        self.release = {k: asyncio.Event() for k in self.entered}
        self.cancelled = set()
        self.calls = {}
        self.bad = None
        self.bad_side = 'main'
        self.answer = reply()

    async def ask(self, state, questions):
        side = 'transfer' if set(questions) == {'transfer'} else 'main'
        self.calls[side] = (state.model_copy(deep=True), dict(questions))
        self.entered[side].set()
        try:
            await self.release[side].wait()
        except asyncio.CancelledError:
            self.cancelled.add(side)
            raise
        values = response_model(self.answer).answers
        values = {k: v for k, v in values.items() if k in questions}
        if self.bad_side == side:
            if self.bad == 'error':
                raise RuntimeError('offline wire')
            if self.bad == 'missing':
                values.pop(next(iter(values)))
            if self.bad == 'extra':
                values['extra'] = response_model(reply()).answers['keypad']
            if self.bad == 'type':
                from waitless.models import JevAnswer
                values[next(iter(values))] = JevAnswer(type='noul', noul=.8)
        from waitless.models import JevResponse
        return JevResponse(answers=values)

async def start(h, wire):
    h.call._keypad.deps.jev = wire
    await h.sonic.emit_user_text('A company sentence')
    await settle(100)

@pytest.mark.parametrize('first', ['main', 'transfer'])
async def test_f1_both_completion_orders_one_classify_and_same_snapshot(live119, first):
    h, wire = live119, SplitWire()
    with patch('waitless.call.keypad.classify', wraps=__import__('waitless.call.speaker_policy', fromlist=['classify']).classify) as classify:
        await start(h, wire)
        assert set(wire.calls) == {'main', 'transfer'}
        assert set(wire.calls['main'][1]) == MAIN
        assert set(wire.calls['transfer'][1]) == {'transfer'}
        assert wire.calls['main'][0] == wire.calls['transfer'][0]
        await h.clock.advance(17)
        wire.release[first].set()
        await settle(80)
        assert not h.events('decision') and classify.call_count == 0
        await h.clock.advance(23)
        wire.release['transfer' if first == 'main' else 'main'].set()
        await settle(250)
        assert classify.call_count == 1
    d = next(e for e in h.events('decision') if e.policy_decision['intent'] == 'speaker')
    assert d.jev_request['transfer_state'] == d.jev_request['state']
    assert set(d.jev_request['questions']) == {'keypad', 'speaker', 'transfer'}
    assert 'model' in d.jev_request
    assert set(d.jev_response['answers']) == {'keypad', 'speaker', 'transfer'}
    assert {p['status'] for p in d.jev_response['parts'].values()} == {'ok'}
    assert d.latency_ms == 40
    assert sorted(p['latency_ms'] for p in d.jev_response['parts'].values()) == [17, 40]
    assert h.call._media_gate.output_state == 'allowed'

@pytest.mark.parametrize('side', ['main', 'transfer'])
@pytest.mark.parametrize('bad', ['missing', 'extra', 'type', 'error', 'timeout'])
async def test_f1_single_side_failure_cancels_and_counts_once(live119, side, bad):
    h, wire = live119, SplitWire()
    wire.answer['transfer']['choice'] = 'connected'
    wire.bad_side, wire.bad = side, bad
    await start(h, wire)
    assert set(wire.calls) == {'main', 'transfer'}
    if bad == 'timeout':
        wire.release['transfer' if side == 'main' else 'main'].set()
        await settle(80)
        await h.clock.advance(h.settings.engine.speaker.verdict_timeout_ms)
    else:
        wire.release[side].set()
    await settle(300)
    assert not h.call._keypad.policy_state.guard
    assert h.call._keypad.policy_state.unavailable_run == 1
    assert not h.events('handoff_ready') and not h.events('dtmf_sent')
    assert not h.call._tap._keypad_tasks
    d = h.events('decision')[0]
    assert d.jev_response['answers'] == {}
    status = d.jev_response['parts'][side]['status']
    assert status == ('error' if bad == 'error' else 'timeout' if bad == 'timeout' else 'invalid')
    if bad != 'timeout':
        other = 'transfer' if side == 'main' else 'main'
        assert other in wire.cancelled
        assert d.jev_response['parts'][other]['status'] == 'cancelled'

async def test_f1_connected_half_then_error_has_no_guard_or_handoff(live119):
    h, wire = live119, SplitWire()
    wire.answer['transfer']['choice'] = 'connected'
    wire.bad, wire.bad_side = 'error', 'main'
    await start(h, wire)
    wire.release['transfer'].set()
    await settle(80)
    assert not h.call._keypad.policy_state.guard and not h.events('decision')
    wire.release['main'].set()
    await settle(250)
    assert not h.call._keypad.policy_state.guard
    assert h.call._keypad.policy_state.unavailable_run == 1
    assert not h.events('handoff_ready')

@pytest.mark.parametrize('change', ['final', 'epoch'])
async def test_f1_stale_valid_pair_only_tightens_guard(live119, change):
    h, wire = live119, SplitWire()
    wire.answer['transfer']['choice'] = 'pending'
    await start(h, wire)
    wire.release['main'].set()
    await settle(80)
    if change == 'final':
        await h.sonic.emit_user_text('')
    else:
        await h.call._media_gate.invalidate_stream()
    wire.release['transfer'].set()
    await settle(250)
    assert h.call._keypad.policy_state.guard
    assert h.call._keypad.policy_state.unavailable_run == 0
    assert h.call._media_gate.output_state != 'allowed'
    assert not h.events('handoff_ready') and not h.events('dtmf_sent')

async def test_f1_trace_store_roundtrip_and_fallback_exact_copy(live119, tmp_path):
    from waitless.store import Store
    from waitless.trace import Trace
    h, wire = live119, SplitWire()
    wire.answer['speaker']['confidence'] = .1
    wire.answer['speaker']['probabilities'] = dict.fromkeys(
        ('menu_or_recording', 'hold', 'virtual_agent', 'live_human'), .25)
    wire.release['main'].set(); wire.release['transfer'].set()
    await start(h, wire)
    await settle(250)
    d = next(e for e in h.events('decision') if e.policy_decision['intent'] == 'speaker')
    trigger = h.call._fallback_trigger(1, d.decision_id, 'speaker_quiet_after_uncertain')
    assert trigger['jev_request'] == d.jev_request and trigger['jev_response'] == d.jev_response
    store = Store(tmp_path / 'trace.sqlite')
    try:
        trace = Trace(store, h.clock)
        trace.event(d)
        got = trace.events(call_id=h.spec.call_id)[0]
        assert got.jev_request == d.jev_request and got.jev_response == d.jev_response
    finally:
        store.close()

async def test_f1_policy_latency_covers_both_parts(live119):
    h, wire = live119, SplitWire()
    await start(h, wire)
    wire.release['main'].set()
    await settle(40)
    await h.clock.advance(29)
    wire.release['transfer'].set()
    await settle(250)
    d = h.events('decision')[0]
    assert d.policy_decision['latency_ms'] == d.latency_ms == 29

async def test_f1_uncooperative_cancel_is_bounded_and_owned(agent_call):
    from waitless.call.live import UNRECLAIMED, TEARDOWN_STATE
    h, wire = await boot_unique(agent_call), SplitWire()
    late = asyncio.Event()
    original = wire.ask
    async def ask(state, questions):
        if set(questions) == {'transfer'}:
            try:
                return await original(state, questions)
            except asyncio.CancelledError:
                await late.wait()
                return response_model({'transfer': reply()['transfer']})
        return await original(state, questions)
    wire.ask = ask
    wire.bad = 'error'
    h.settings.engine.speaker.teardown_cancel_grace_ms = 15
    await start(h, wire)
    wire.release['main'].set()
    try:
        await asyncio.sleep(.05)
        await settle(150)
        assert h.call._keypad.policy_state.unavailable_run == 1
        assert not h.call._tap._keypad_tasks
        assert any(call_id == h.spec.call_id for call_id, _ in UNRECLAIMED.values())
        assert any(r['error_type'] == 'Unreclaimed' for r in TEARDOWN_STATE.get(h.spec.call_id, []))
    finally:
        late.set()
        await settle(150)
    assert not any(call_id == h.spec.call_id for call_id, _ in UNRECLAIMED.values())
    assert not h.events('handoff_ready')

@pytest.mark.parametrize('side', ['main', 'transfer'])
async def test_f1_http_bad_package_is_invalid(live119, mode_env, side):
    import httpx
    from pydantic import SecretStr
    from waitless.decision.jev import HttpJevClient
    h = live119
    env = mode_env('text-sim')
    async def respond(req):
        import json
        body = json.loads(req.content)
        qs = set(body['questions'])
        values = response_model(reply()).model_dump(mode='json')['answers']
        values = {k: v for k, v in values.items() if k in qs}
        if (side == 'main') == ('speaker' in qs):
            values.pop(next(iter(values)))
        return httpx.Response(200, json={'answers': values})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        h.call._keypad.deps.jev = HttpJevClient(url=env['WAITLESS_JEV_URL'],
            model=env['WAITLESS_JEV_MODEL'], api_key=SecretStr('offline-fixture'),
            timeout_s=1, http=http)
        await h.sonic.emit_user_text('Company sentence')
        await settle(250)
    d = h.events('decision')[0]
    assert d.jev_response['answers'] == {}
    assert d.jev_response['parts'][side]['status'] == 'invalid'
    assert isinstance(d.jev_response['parts'][side]['response'], dict)
    assert h.call._keypad.policy_state.unavailable_run == 1
