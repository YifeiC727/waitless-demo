import asyncio
import json
import pytest
from fakes import jev_choice
from pipecat.frames.frames import OutputDTMFUrgentFrame, OutputAudioRawFrame
from call_engine._support import dtmf_digits, settle, wait_until
from lane_D.agent_support import *
from lane_D.test_u109_tool_error_hints import hint_for

ASK = {'question': 'Which date?', 'options': [{'label': 'Monday'}, {'label': 'Tuesday', 'detail': 'Afternoon'}]}

async def boot(h):
    await h.boot()

async def test_update_status_sets_phase_and_status_line(agent_call):
    h = agent_call
    await boot(h)
    assert await h.tool('update_status', {'phase': 'hold', 'status_line': 'Waiting for service'}) == {'ok': True}
    status = h.call.status(debug=True)
    assert (status.phase, status.status_line) == ('hold', 'Waiting for service')
    assert status.debug.guard.model_dump() == {'blocked': 0, 'released': 0}
    assert h.events('status_updated')[-1].action == {'phase': 'hold', 'status_line': 'Waiting for service'}
    h.assert_trace_contract()

async def test_update_status_rejects_bad_phase(agent_call):
    h = agent_call
    await boot(h)
    assert (await h.tool('update_status', {'phase': 'private', 'status_line': 'x'}))['error'] == 'invalid_phase'
    assert not h.events('status_updated')
    h.assert_trace_contract()

async def test_action_phase_wins_over_reported_phase(agent_call):
    h = agent_call
    await boot(h)
    await h.tool('ask_user', ASK)
    await h.tool('update_status', {'phase': 'hold', 'status_line': 'Waiting'})
    assert h.call.status().phase == 'awaiting_user'
    choice = h.call.status().pending_choice
    await h.call.answer(choice.choice_id, choice.options[0].id)
    assert h.call.status().phase == 'hold'
    await h.tool('request_handoff', {'reason': 'Verification'})
    await h.clock.advance(h.settings.engine.handoff.announce_idle_ms + 10)
    await settle(200)
    await h.tool('update_status', {'phase': 'live_rep', 'status_line': 'Representative'})
    assert h.call.status().phase == 'handoff_ready'
    h.assert_trace_contract()

async def test_ask_user_creates_card_and_returns_asked(agent_call):
    h = agent_call
    await boot(h)
    result = await h.tool('ask_user', ASK)
    choice = h.call.status().pending_choice
    assert result == {'status': 'asked', 'choice_id': choice.choice_id}
    assert choice.question_text == ASK['question']
    assert [o.label for o in choice.options] == ['Monday', 'Tuesday']
    assert (choice.expires_at - choice.created_at).total_seconds() == 20
    assert h.call.status().phase == 'awaiting_user'
    assert h.call.status().interrupts_used == 1
    h.assert_trace_contract()

async def test_ask_user_answer_becomes_event(agent_call):
    h = agent_call
    await boot(h)
    await h.tool('ask_user', ASK)
    choice = h.call.status().pending_choice
    option = choice.options[1]
    assert (await h.call.answer(choice.choice_id, option.id)).status == 'choice_recorded'
    assert h.event_payloads() == [{'event': 'user_choice', 'choice_id': choice.choice_id,
        'status': 'answered', 'option_id': option.id, 'label': 'Tuesday', 'detail': 'Afternoon'}]
    assert h.events('user_answered')
    h.assert_trace_contract()

async def test_ask_user_expiry_becomes_event(agent_call):
    h = agent_call
    await boot(h)
    await h.tool('ask_user', ASK)
    choice = h.call.status().pending_choice
    await h.clock.advance(20_001)
    await settle()
    assert h.event_payloads() == [{'event': 'user_choice', 'choice_id': choice.choice_id, 'status': 'expired'}]
    assert h.deps.pending.get(choice.choice_id).status == 'expired'
    assert h.events('pending_expired')
    h.assert_trace_contract()

async def test_ask_user_budget_exhausted_is_an_error(agent_call):
    h = agent_call
    h.spec.compiled.policy['global']['interrupt_budget'] = 0
    await boot(h)
    result = await h.tool('ask_user', ASK)
    assert result == {'ok': False, 'error': 'interrupt_budget_exhausted', 'used': 0, 'budget': 0,
        'hint': hint_for(h.settings.skills_dir, 'interrupt_budget_exhausted')}
    assert h.call.status().pending_choice is None
    assert not h.events('pending_choice_created')
    h.assert_trace_contract()

async def test_ask_jev_calls_jev_once_and_returns_choice(agent_call, tmp_path, monkeypatch):
    h = agent_call
    # Provide the G-owned skill through the existing loader, without changing G files.
    skill = tmp_path / 'phone-agent'
    (skill / 'references').mkdir(parents=True)
    (skill / 'references/jev.yaml').write_text('ask_jev_instructions: |\n  Skill-owned judgment instructions.\n')
    monkeypatch.setattr('waitless.skills.loader.load_skills', lambda root: {'phone-agent': SimpleNamespace(path=skill)})
    h.spec.compiled.allowed_fields['ssn'] = 'DECOY-NEVER-SEND'
    h.deps.jev.push({'q': jev_choice('2', .9)})
    await boot(h)
    result = await h.tool('ask_jev', {'question': 'Which menu?', 'options': ['Billing', 'Service']})
    assert result == {'choice': 'Service', 'confidence': .9, 'probabilities': {'Billing': pytest.approx(.1), 'Service': .9}}
    assert len(h.deps.jev.calls) == 1
    request = h.deps.jev.calls[0]
    assert request.questions['q'].instructions == 'Skill-owned judgment instructions.\n\n\nQuestion: Which menu?'
    assert request.questions['q'].criteria == {'1': 'Billing', '2': 'Service'}
    assert 'DECOY-NEVER-SEND' not in request.state.model_dump_json()
    assert h.events('decision')[-1].jev_response['answers']['q']['choice'] == '2'
    h.assert_trace_contract()

async def test_ask_jev_without_client_returns_unavailable(agent_call):
    h = agent_call
    h.deps.jev = None
    await boot(h)
    assert await h.tool('ask_jev', {'question': 'Which?', 'options': ['One', 'Two']}) == {'ok': False, 'error': 'jev_unavailable',
        'hint': hint_for(h.settings.skills_dir, 'jev_unavailable')}
    h.assert_trace_contract()

async def test_request_handoff_waits_idle_then_ready(agent_call):
    h = agent_call
    await boot(h)
    await h.sonic_says('I will bring the account holder onto the call.')
    last_audio = h.clock.monotonic_ms()
    assert await h.tool('request_handoff', {'reason': 'Verification'}) == {'status': 'handoff_ready'}
    await h.clock.advance(h.settings.engine.handoff.announce_idle_ms + 10)
    await settle(200)
    types = [e.event_type for e in h.events()]
    assert types.index('handoff_announced') < types.index('handoff_ready') < types.index('handoff_started')
    assert h.events('handoff_ready')[-1].t_ms - last_audio >= h.settings.engine.handoff.announce_idle_ms
    assert h.events('handoff_announced')[-1].action['text'] == 'I will bring the account holder onto the call.'
    assert h.call._media_gate.in_handoff
    h.assert_trace_contract()

async def test_live_return_refuses_to_reconnect_retained_toolbox(agent_call):
    h = agent_call
    await boot(h)
    await h.tool('request_handoff', {'reason': 'Verification'})
    await h.clock.advance(h.settings.engine.handoff.announce_idle_ms + 10)
    await settle(200)
    join = await h.call.join()
    from waitless.interfaces import ToolFailure
    with pytest.raises(ToolFailure):
        await h.call.return_to_agent(join.handoff_id)
    assert not h.event_payloads()
    assert h.call._media_gate.in_handoff
    assert not h.events('handoff_ended')
    h.assert_trace_contract()

async def test_end_call_records_and_hangs_up(agent_call):
    h = agent_call
    await boot(h)
    args = {'outcome': 'confirmed', 'confirmation_number': 'LE-12345', 'summary': 'Service started'}
    assert await h.tool('end_call', args) == {'ok': True}
    await h.clock.advance(9000)
    await wait_until(lambda: h.call.ended)
    assert h.events('hangup')[-1].action == args
    assert h.carrier.hangups == [h.call_sid]
    assert h.events('hangup')[-1].seq < h.events('call_ended')[-1].seq
    h.assert_trace_contract()

async def test_end_call_rejects_bad_outcome(agent_call):
    h = agent_call
    await boot(h)
    assert (await h.tool('end_call', {'outcome': 'maybe', 'summary': 'x'}))['ok'] is False
    assert not h.events('hangup') and not h.carrier.hangups
    h.assert_trace_contract()
