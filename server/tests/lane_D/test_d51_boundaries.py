"""Schema boundaries and lifecycle races that must not cause media/card side effects."""
import asyncio
import pytest
from lane_D.agent_support import *
from lane_D.test_u109_tool_error_hints import hint_for
from lane_D.test_d51_tools import ASK

@pytest.mark.parametrize('name,args', [
    ('update_status', {'phase': 'ivr', 'status_line': 'x' * 121}),
    ('ask_user', {'question': 'x' * 201, 'options': [{'label': 'A'}, {'label': 'B'}]}),
    ('ask_user', {'question': 'Q', 'options': [{'label': 'A'}]}),
    ('ask_user', {'question': 'Q', 'options': [{'label': 'A'}] * 4}),
    ('ask_user', {'question': 'Q', 'options': [{'label': 'x' * 61}, {'label': 'B'}]}),
    ('ask_user', {'question': 'Q', 'options': [{'label': 'A', 'detail': 'x' * 121}, {'label': 'B'}]}),
    ('ask_jev', {'question': 'x' * 301, 'options': ['A', 'B']}),
    ('ask_jev', {'question': 'Q', 'options': ['A']}),
    ('ask_jev', {'question': 'Q', 'options': ['A'] * 6}),
    ('ask_jev', {'question': 'Q', 'options': ['x' * 81, 'B']}),
    ('request_handoff', {'reason': 'x' * 161}),
    ('end_call', {'outcome': 'confirmed', 'summary': 'x' * 301}),
    ('end_call', {'outcome': 'confirmed', 'summary': 'Done', 'confirmation_number': 2}),
])
async def test_invalid_arguments_have_no_media_or_card_effect(agent_call, name, args):
    h = agent_call
    await h.boot()
    result = await h.tool(name, args)
    assert result['ok'] is False
    assert not h.transport.output().dtmf()
    assert not h.call.status().pending_choice and not h.call._media_gate.in_handoff
    assert not h.carrier.hangups
    h.assert_trace_contract()

async def test_ask_user_and_handoff_share_one_budget(agent_call):
    h = agent_call
    h.spec.compiled.policy['global']['interrupt_budget'] = 1
    await h.boot()
    await h.tool('ask_user', ASK)
    choice = h.call.status().pending_choice
    await h.call.answer(choice.choice_id, choice.options[0].id)
    result = await h.tool('request_handoff', {'reason': 'Verify'})
    assert result == {'ok': False, 'error': 'interrupt_budget_exhausted', 'used': 1, 'budget': 1,
        'hint': hint_for(h.settings.skills_dir, 'interrupt_budget_exhausted')}
    assert not h.events('handoff_ready') and not h.call._media_gate.in_handoff
    h.assert_trace_contract()

async def test_existing_pending_choice_is_not_replaced(agent_call):
    h = agent_call
    await h.boot()
    first = await h.tool('ask_user', ASK)
    assert await h.tool('ask_user', ASK) == {'ok': False, 'error': 'choice_already_open',
        'hint': hint_for(h.settings.skills_dir, 'choice_already_open')}
    assert h.call.status().pending_choice.choice_id == first['choice_id']
    assert len(h.events('pending_choice_created')) == 1
    h.assert_trace_contract()

async def test_expiry_and_late_answer_send_one_event(agent_call):
    h = agent_call
    await h.boot()
    await h.tool('ask_user', ASK)
    choice = h.call.status().pending_choice
    # Force the registry's late-answer branch before the background expiry task runs.
    h.clock._now_ms += 20_000
    result = await h.call.answer(choice.choice_id, choice.options[0].id)
    assert result.status == 'expired'
    await h.clock.advance(1)
    assert h.event_payloads() == [{'event': 'user_choice', 'choice_id': choice.choice_id, 'status': 'expired'}]
    assert len(h.events('pending_expired')) == 1
    h.assert_trace_contract()

async def test_jev_failure_returns_unavailable(agent_call):
    h = agent_call
    h.spec.compiled.jev_templates['ask_jev'] = {'instructions': 'Fixture skill instructions'}
    h.deps.jev.push(RuntimeError('unavailable'))
    await h.boot()
    assert await h.tool('ask_jev', {'question': 'Which?', 'options': ['One', 'Two']}) == {'ok': False, 'error': 'jev_unavailable',
        'hint': hint_for(h.settings.skills_dir, 'jev_unavailable')}
    h.assert_trace_contract()

async def test_monitor_idle_wait_has_a_cap(agent_call):
    from pipecat.frames.frames import TTSAudioRawFrame
    h = agent_call
    await h.boot()
    monitor = h.call._monitor
    task = asyncio.create_task(monitor.wait_idle(800, cap_ms=2000))
    await settle(100)
    for _ in range(10):
        await h.clock.advance(200)
        await h.sonic.push_frame(TTSAudioRawFrame(audio=bytes(960), sample_rate=24000, num_channels=1))
        await settle(100)
    assert task.done()
    await task
    assert monitor.audio_serial == 10
    assert len(h.transport.output().tts_audio()) == 0  # Raw test frames have no response identity.


@pytest.mark.parametrize('answer', [False, True])
async def test_pending_completion_does_not_clear_active_handoff(agent_call, answer):
    h = agent_call
    await h.boot()
    await h.tool('ask_user', ASK)
    choice = h.call.status().pending_choice
    await h.tool('request_handoff', {'reason': 'Verification'})
    await h.clock.advance(h.settings.engine.handoff.announce_idle_ms + 10)
    await settle(200)
    joined = await h.call.join()
    await h.open_handoff(joined)
    if answer:
        await h.call.answer(choice.choice_id, choice.options[0].id)
    else:
        await h.clock.advance(20_001)
    assert h.call.status().phase == 'in_handoff'
    from waitless.interfaces import ToolFailure
    with pytest.raises(ToolFailure) as exc:
        h.call.listen()
    assert exc.value.code == 'listen_unavailable'


async def test_new_choice_cannot_override_handoff_phase(agent_call):
    h = agent_call
    await h.boot()
    await h.tool('request_handoff', {'reason': 'Verification'})
    await h.clock.advance(h.settings.engine.handoff.announce_idle_ms + 10)
    await settle(200)
    await h.tool('ask_user', ASK)
    assert h.call.status().phase == 'handoff_ready'


async def test_dtmf_media_failure_does_not_claim_delivery(agent_call):
    from waitless.call.agent_tools import send_dtmf
    h = agent_call
    await h.boot()
    async def broken_queue(frame):
        raise RuntimeError('offline transport')
    with pytest.raises(RuntimeError, match='offline transport'):
        await send_dtmf(SimpleNamespace(queue_frame=broken_queue), h.settings, '1')
    assert not h.events('dtmf_sent')


async def test_cancelled_tool_still_records_a_tool_result(agent_call):
    h = agent_call
    await h.boot()
    # Keep the invocation pending at the budget check; idle waiting is now detached.
    await h.call._toolbox._budget_lock.acquire()
    task = asyncio.create_task(invoke_tool(h.sonic, 'request_handoff', {'reason': 'Verify'}))
    await settle(100)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    h.call._toolbox._budget_lock.release()
    assert h.events('agent_tool_result')[-1].action['result'] == {'ok': False, 'error': 'cancelled'}
    assert not h.events('handoff_ready')
    h.assert_trace_contract()


async def test_registry_expired_choice_still_notifies_its_agent(agent_call):
    h = agent_call
    await h.boot()
    await h.tool('ask_user', ASK)
    choice = h.call.status().pending_choice
    # Another call's timer may run the shared registry's expire_due first.
    h.clock._now_ms += 20_000
    assert [c.choice_id for c in h.deps.pending.expire_due()] == [choice.choice_id]
    await h.clock.advance(1)
    assert h.event_payloads() == [{'event': 'user_choice', 'choice_id': choice.choice_id, 'status': 'expired'}]
    h.assert_trace_contract()
