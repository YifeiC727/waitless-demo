"""D51b: tool results precede deferred handoff; application events are not speech."""
import asyncio

from lane_D.agent_support import *


async def immediate_tool(h, name, args):
    """Catch an idle wait in the handler without advancing the fake clock."""
    started = h.clock.monotonic_ms()
    task = asyncio.create_task(invoke_tool(h.sonic, name, args))
    try:
        await settle(100)
        assert task.done(), 'tool must return without advancing the fake clock'
        assert h.clock.monotonic_ms() == started
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_d51b_request_handoff_returns_before_the_idle_wait(agent_call):
    h = agent_call
    await h.boot()
    await h.sonic_says('I will bring the account holder onto the call.')
    h.call._monitor.last_audio_ms = h.clock.monotonic_ms()
    assert await immediate_tool(h, 'request_handoff', {'reason': 'Verification'}) == {'status': 'handoff_ready'}
    assert not h.events('handoff_ready')
    assert not h.call._media_gate.in_handoff
    await h.clock.advance(h.settings.engine.handoff.announce_idle_ms + 10)
    await settle(200)
    kinds = [e.event_type for e in h.events()]
    assert kinds.index('agent_tool_result') < kinds.index('handoff_announced') < kinds.index('handoff_ready') < kinds.index('handoff_started')
    assert h.call._media_gate.in_handoff
    joined = await h.call.join()
    await h.sonic.emit_user_text('Private verification details.')
    await h.sonic_says('Private assistant output.')
    await h.call.return_to_agent(joined.handoff_id)
    events = h.events()
    started = next(i for i, e in enumerate(events) if e.event_type == 'handoff_started')
    ended = next(i for i, e in enumerate(events) if e.event_type == 'handoff_ended')
    assert events[started + 1:ended] == []
    h.assert_trace_contract()


async def test_d51b_second_request_while_pending_is_refused(agent_call):
    h = agent_call
    await h.boot()
    h.call._monitor.last_audio_ms = h.clock.monotonic_ms()
    assert await immediate_tool(h, 'request_handoff', {'reason': 'Verification'}) == {'status': 'handoff_ready'}
    assert not h.events('handoff_ready')
    assert await immediate_tool(h, 'request_handoff', {'reason': 'Duplicate'}) == {'ok': False, 'error': 'handoff_already_active'}
    await h.clock.advance(h.settings.engine.handoff.announce_idle_ms + 10)
    await settle(200)
    assert h.call.status().interrupts_used == 1
    joined = await h.call.join()
    await h.call.return_to_agent(joined.handoff_id)
    assert await immediate_tool(h, 'request_handoff', {'reason': 'Second verification'}) == {'status': 'handoff_ready'}
    await h.clock.advance(h.settings.engine.handoff.announce_idle_ms + 10)
    await settle(200)
    assert len(h.events('handoff_started')) == 2
    assert h.call.status().interrupts_used == 2
    h.assert_trace_contract()


async def test_d51b_pending_handoff_counts_against_the_budget(agent_call):
    h = agent_call
    h.spec.compiled.policy['global']['interrupt_budget'] = 1
    await h.boot()
    h.call._monitor.last_audio_ms = h.clock.monotonic_ms()
    assert await immediate_tool(h, 'request_handoff', {'reason': 'Verification'}) == {'status': 'handoff_ready'}
    assert not h.events('handoff_ready')
    assert h.call.status().interrupts_used == 1
    result = await immediate_tool(h, 'ask_user', {'question': 'Which date?',
        'options': [{'label': 'Monday'}, {'label': 'Tuesday'}]})
    assert result == {'ok': False, 'error': 'interrupt_budget_exhausted', 'used': 1, 'budget': 1}
    assert h.call.status().pending_choice is None
    assert not h.events('pending_choice_created')
    h.assert_trace_contract()


async def test_d51b_event_final_without_space_is_not_company_turn(agent_call):
    h = agent_call
    await h.boot()
    for words in ('[Waitless event]{"event":"handoff_ended"}',
                  '  [Waitless event] {"event":"x"}', 'Sure, how can I help?'):
        await h.sonic.emit_user_text(words)
    assert [e.transcript for e in h.events('company_turn')] == ['Sure, how can I help?']
    assert h.events('company_turn')[0].action['seq'] == 1
