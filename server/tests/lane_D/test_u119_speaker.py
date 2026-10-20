"""Dual question snapshots, fail-safe handoff and live ownership, offline."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakes import FakeJev, jev_choice
from pipecat.frames.frames import OutputAudioRawFrame
from call_engine._support import AsgiWs, HANDOFF_HELLO, settle, wait_until
from lane_D.agent_support import agent_call, settings, spec, engine, compiled, line_number, store, trace
from waitless import models as m
from waitless.interfaces import ToolFailure

SPEAKER = {'instructions': 'Classify the current speaker in context.', 'min_confidence': .7,
    'options': {'menu_or_recording': 'menu', 'hold': 'hold', 'virtual_agent': 'bot', 'live_human': 'person'}}


def reply(speaker='virtual_agent', keypad='none', confidence=.95):
    return {'speaker': jev_choice(speaker, confidence), 'keypad': jev_choice(keypad)}


def response_model(values):
    return m.JevResponse(answers={key: m.JevAnswer.model_validate({**value,
        'probabilities': value.get('probabilities') or {value['choice']: 1.0}})
        for key, value in values.items()})


@pytest.fixture
async def live119(agent_call, monkeypatch):
    import waitless.call.live as module
    monkeypatch.setattr(module, 'load_speaker_config', lambda path: SPEAKER, raising=False)
    h = agent_call
    h.component_tools = False
    h.deps.jev = FakeJev(default=reply())
    await h.boot()
    yield h


async def turn(h, answer=None, text='Company turn'):
    if answer is not None:
        h.deps.jev = FakeJev([answer])
    h.call._keypad.deps.jev = h.deps.jev
    await h.sonic.emit_user_text(text)
    await settle(300)


async def test_live_context_service_and_handlers_are_empty(live119):
    h = live119
    assert not h.sonic.registered_functions
    assert h.sonic.init_kwargs['tools'] == []
    assert not getattr(h.call._context.tools, 'standard_tools', [])


async def test_same_request_independent_reasons_and_phase_changes(live119):
    h = live119
    await turn(h, reply(keypad='1'))
    assert len(h.deps.jev.calls) == 1
    assert set(h.deps.jev.calls[0].questions) == {'keypad', 'speaker'}
    assert len(h.transport.output().dtmf()) == 1
    assert h.call.status().phase == 'ai_agent'
    await turn(h, reply())
    assert len(h.events('status_updated')) == 1
    for d in h.events('decision'):
        assert d.policy_decision['policy_rule'] in ('phone-agent/keypad', 'phone-agent/speaker')
        assert d.jev_request['questions'].keys() == {'keypad', 'speaker'}


@pytest.mark.parametrize('failure', ['low', 'missing', 'offline', 'timeout', 'empty'])
async def test_unknown_once_immediately_requests_user_without_another_company_turn(live119, failure):
    h = live119
    if failure == 'low':
        answer = reply(confidence=.1)
    elif failure == 'missing':
        answer = {'keypad': jev_choice('1')}
    elif failure == 'empty':
        answer = reply()
    elif failure == 'timeout':
        async def ask(*args):
            await asyncio.Event().wait()
        h.deps.jev = SimpleNamespace(ask=ask)
        answer = None
    else:
        answer = RuntimeError('offline')
    await turn(h, answer, text='' if failure == 'empty' else 'Hello')
    if failure == 'timeout':
        await h.clock.advance(h.settings.engine.speaker.verdict_timeout_ms)
        await settle(300)
    assert h.call.status().phase == 'handoff_ready'
    assert h.call._media_gate.in_handoff
    assert not h.transport.output().dtmf()
    assert len(h.events('handoff_ready')) == 1


async def test_live_human_overrides_keys_and_event_sequence(live119):
    h = live119
    await turn(h, reply('live_human', '1'))
    assert not h.transport.output().dtmf()
    ev = h.events()
    speaker = next(e for e in ev if e.event_type == 'decision' and e.policy_decision['intent'] == 'speaker')
    follow = [e for e in ev if e.event_type in ('handoff_announced', 'handoff_ready', 'handoff_started')]
    assert [e.event_type for e in follow] == ['handoff_announced', 'handoff_ready']
    assert all(e.decision_id == speaker.decision_id and e.seq > speaker.seq for e in follow)
    assert follow[0].action['text'] == ''
    status = h.call.status()
    assert status.handoff.reason == h.settings.engine.handoff.notification_text
    assert status.handoff.reason_kind == 'live_human' and status.handoff.expires_at
    assert status.interrupts_used == 1
    join = await h.call.join()
    ws = await h.open_handoff(join)
    assert h.call.status().phase == 'in_handoff'
    assert len(h.events('handoff_started')) == 1
    ws.json({'type': 'return_to_agent'})
    await settle(200)
    assert h.call.ended
    assert not h.events('handoff_ended') and not h.event_payloads()


async def test_final_revokes_before_background_decider_runs(live119):
    h = live119
    await turn(h, reply())
    g = h.call._media_gate
    assert g.output_state == 'allowed'
    seen = []
    async def decide(seq):
        seen.append(g.output_state)
    h.call._keypad.on_company_turn = decide
    await h.sonic.emit_user_text('A human has answered')
    assert g.output_state == 'pending' and not seen
    await settle()
    assert seen == ['pending']


async def test_old_reply_cannot_rewrite_phase_or_press_keys(live119):
    h = live119
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def ask(state, questions):
        calls.append(state)
        if len(calls) == 1:
            entered.set()
            await release.wait()
            return response_model(reply('hold', '1'))
        return response_model(reply('live_human'))
    h.deps.jev = SimpleNamespace(ask=ask)
    h.call._keypad.deps.jev = h.deps.jev
    await h.sonic.emit_user_text('First')
    await entered.wait()
    await h.sonic.emit_user_text('Newest')
    await settle()
    release.set()
    await settle(500)
    assert calls[0].transcript.lines[-1].text == 'First'
    assert calls[-1].transcript.lines[-1].text == 'Newest'
    assert h.call.status().phase == 'handoff_ready'
    assert not h.transport.output().dtmf()
    assert [e.action['turn_seq'] for e in h.events('decision')] == [2, 2]


@pytest.mark.parametrize('endpoint', ['join_timeout', 'bad_hello', 'stalled_hello', 'company_hangup'])
async def test_first_deadline_from_ready_and_bad_hello_never_extends_it(live119, endpoint):
    h = live119
    await turn(h, reply('live_human'))
    expires = h.call.status().handoff.expires_at
    join = await h.call.join()
    if endpoint == 'bad_hello':
        await h.open_handoff(join, hello={'type': 'bad'})
    elif endpoint == 'stalled_hello':
        ws = AsgiWs(path=join.ws_path)
        await ws.websocket.accept()
        task = asyncio.create_task(h.call.attach_handoff(ws.websocket, join.handoff_id))
        await settle()
    elif endpoint == 'company_hangup':
        await h.call._on_line_disconnected(None, None)
        await settle(300)
        assert h.events('call_ended')[-1].action['reason'] == 'company_hangup'
        return
    assert h.call.status().handoff.expires_at == expires
    await h.clock.advance(h.settings.engine.handoff.join_timeout_s * 1000)
    await settle(500)
    assert h.call.ended
    assert h.events('call_ended')[-1].action['reason'] == 'join_timeout'
    assert not h.events('handoff_started') and not h.events('handoff_ended')


async def test_disconnect_rejoin_generations_mutual_exclusion_and_one_notification(live119):
    h = live119
    await turn(h, reply('live_human'))
    first = await h.call.join()
    stale = await h.call.join()
    assert h.handoff_tokens.consume(first.handoff_id, first.token) is None
    ws = await h.open_handoff(stale)
    active_generation = h.call.status().handoff.generation
    other = AsgiWs(path=stale.ws_path)
    await other.websocket.accept()
    with pytest.raises(ToolFailure):
        await h.call.attach_handoff(other.websocket, stale.handoff_id)
    ws.disconnect()
    await settle(300)
    assert h.call.status().phase == 'handoff_ready'
    assert h.call.status().handoff.generation > active_generation
    assert len(h.events('handoff_ready')) == 1
    second = await h.call.join()
    await h.open_handoff(second)
    await h.clock.advance(h.settings.engine.handoff.rejoin_grace_s * 1000)
    assert not h.call.ended
    assert len(h.events('handoff_started')) == 2 and h.call.status().interrupts_used == 1


async def test_expired_rejoin_rejected_even_before_timer_callback(live119):
    h = live119
    await turn(h, reply('live_human'))
    ws = await h.open_handoff(await h.call.join())
    ws.disconnect()
    await settle(300)
    join = await h.call.join()
    await h.clock.advance(h.settings.engine.handoff.rejoin_grace_s * 1000)
    assert h.call.ended
    assert h.events('call_ended')[-1].action['reason'] == 'rejoin_timeout'
    with pytest.raises(ToolFailure):
        await h.call.attach_handoff(ws.websocket, join.handoff_id)


async def test_live_return_entry_and_toolbox_callback_never_resume(live119):
    h = live119
    await turn(h, reply('live_human'))
    await h.call._toolbox.on_handoff_returned()
    assert h.call._media_gate.in_handoff and not h.events('handoff_ended')
    with pytest.raises(ToolFailure):
        await h.call.return_to_agent((await h.call.join()).handoff_id)
    assert h.call._media_gate.in_handoff and not h.event_payloads()


async def test_hangup_idempotent_and_remote_failure_can_retry_same_line(live119):
    h = live119
    h.call.carrier.hangup = AsyncMock(side_effect=[RuntimeError('offline'), None])
    with pytest.raises(RuntimeError):
        await h.call.hangup('user_hangup')
    assert h.call.ended and len(h.events('call_ended')) == 1
    await h.call.hangup('user_hangup')
    await h.call.hangup('user_hangup')
    assert h.call.carrier.hangup.call_count == 2
    assert all(c.args == (h.call_sid,) for c in h.call.carrier.hangup.call_args_list)


async def test_owned_carrier_remains_open_until_remote_retry_succeeds(live119):
    h = live119
    h.call._owns_carrier = True
    h.call.carrier.aclose = AsyncMock()
    h.call.carrier.hangup = AsyncMock(side_effect=[RuntimeError('offline'), None])
    with pytest.raises(RuntimeError):
        await h.call.hangup('user_hangup')
    assert h.call.carrier.aclose.await_count == 0
    await h.call.hangup('user_hangup')
    assert h.call.carrier.aclose.await_count == 1


async def test_rejoin_exact_deadline_rejects_hello_before_timer_is_dispatched(live119):
    h = live119
    await turn(h, reply('live_human'))
    ws = await h.open_handoff(await h.call.join())
    ws.disconnect()
    await settle(300)
    join = await h.call.join()
    # Reach the deadline without dispatching the timer, modelling the same-tick race.
    h.clock._now_ms += h.settings.engine.handoff.rejoin_grace_s * 1000
    other = AsgiWs(path=join.ws_path)
    await other.websocket.accept()
    with pytest.raises(ToolFailure):
        await h.call.attach_handoff(other.websocket, join.handoff_id)
    assert len(h.events('handoff_started')) == 1
    await h.clock.advance(0)
    await settle(300)
    assert h.call.ended and h.events('call_ended')[-1].action['reason'] == 'rejoin_timeout'


async def test_consumed_old_token_cannot_attach_after_new_join_generation(live119):
    h = live119
    await turn(h, reply('live_human'))
    first = await h.call.join()
    assert h.handoff_tokens.consume(first.handoff_id, first.token) == h.spec.call_id
    second = await h.call.join()
    old_ws = AsgiWs(path=first.ws_path)
    await old_ws.websocket.accept()
    with pytest.raises(ToolFailure):
        await h.call.attach_handoff(old_ws.websocket, first.handoff_id)
    await h.open_handoff(second)
    assert h.call.status().phase == 'in_handoff'


@pytest.mark.parametrize('bad', ['missing_keypad', 'missing_speaker', 'invalid_speaker', 'invalid_keypad', 'wrong_type'])
async def test_malformed_package_never_presses_or_opens_gate(live119, bad):
    h = live119
    answers = response_model(reply(keypad='1')).answers
    if bad.startswith('missing_'):
        answers.pop(bad.removeprefix('missing_'))
    elif bad == 'invalid_speaker':
        answers['speaker'] = m.JevAnswer(type='choice', choice='other', confidence=.99, probabilities={'other': 1.0})
    elif bad == 'invalid_keypad':
        answers['keypad'] = m.JevAnswer(type='choice', choice='other', confidence=.99, probabilities={'other': 1.0})
    else:
        answers['speaker'] = m.JevAnswer(type='noul', noul=.9)
    async def ask(*args):
        return m.JevResponse(answers=answers)
    h.deps.jev = SimpleNamespace(ask=ask)
    await turn(h)
    assert h.call.status().phase == 'handoff_ready'
    assert not h.events('dtmf_sent')
    assert h.call._media_gate.output_state == 'handoff'


@pytest.mark.parametrize('sample,speaker', [
    ('A conversational automated agent asks for the authorized address.', 'virtual_agent'),
    ('A recording imitates a representative and then lists menu choices.', 'menu_or_recording'),
    ('A person reads a standard introduction from a script.', 'live_human'),
    ('A representative says hello then waits a long time.', 'live_human'),
    ('Music and an announcement ask the caller to keep holding.', 'hold'),
    ('Corrupted or unusable transcription.', 'unknown'),
])
async def test_k34_offline_routing_samples_use_only_jev_verdict(live119, sample, speaker):
    h = live119
    answer = reply(speaker) if speaker != 'unknown' else RuntimeError('offline classifier unavailable')
    await turn(h, answer, text=sample)
    expected = {'virtual_agent': 'ai_agent', 'menu_or_recording': 'ivr', 'hold': 'hold',
        'live_human': 'handoff_ready', 'unknown': 'handoff_ready'}[speaker]
    assert h.call.status().phase == expected


async def test_stream_swap_invalidates_inflight_speaker_verdict(live119):
    h = live119
    entered, release = asyncio.Event(), asyncio.Event()
    async def ask(*args):
        entered.set()
        await release.wait()
        return response_model(reply('live_human', '1'))
    h.call._keypad.deps.jev = SimpleNamespace(ask=ask)
    await h.sonic.emit_user_text('A company line on the old stream')
    await entered.wait()
    await h.call._media_gate.invalidate_stream()
    release.set()
    await settle(300)
    assert not h.events('decision') and not h.events('handoff_ready')
    assert h.call._media_gate.output_state == 'closed'
