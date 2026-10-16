"""U106: expiry notifications must follow the registry transition."""
from datetime import datetime

from lane_D.agent_support import *


ASK = {'question': 'Which date?', 'options': [{'label': 'Monday'}, {'label': 'Tuesday'}]}


async def expire_choice(h, monkeypatch, *, early):
    h.spec.compiled.policy['global']['interrupt_budget'] = 1
    await h.boot()
    timeout = h.spec.compiled.policy['global']['ask_user']['timeout_sec']
    sleep = h.clock.sleep
    early_wakes = []

    async def wake_early_once(seconds):
        if early and not early_wakes and seconds == timeout:
            await sleep(seconds - .001)
            early_wakes.append(datetime.fromisoformat(h.clock.now_iso()))
        else:
            await sleep(seconds)

    monkeypatch.setattr(h.clock, 'sleep', wake_early_once)
    emitted_states = []
    send_event = h.call._toolbox.injector.send_event

    async def observe_event(text):
        payload = json.loads(text.removeprefix('[Waitless event] '))
        if payload.get('status') == 'expired':
            emitted_states.append((h.deps.pending.current(h.spec.call_id),
                h.deps.pending.get(payload['choice_id']).status))
        await send_event(text)

    monkeypatch.setattr(h.call._toolbox.injector, 'send_event', observe_event)
    result = await h.tool('ask_user', ASK)
    choice = h.deps.pending.get(result['choice_id'])
    await h.clock.advance(round(timeout * 1000))
    await settle()
    if early:
        assert len(early_wakes) == 1
        assert (choice.expires_at - early_wakes[0]).total_seconds() == .001
    return choice, emitted_states


def assert_expired(h, choice, emitted_states):
    assert len(h.events('pending_expired')) == 1
    assert h.event_payloads() == [{'event': 'user_choice',
        'choice_id': choice.choice_id, 'status': 'expired'}]
    assert h.deps.pending.current(h.spec.call_id) is None
    assert h.deps.pending.get(choice.choice_id).status == 'expired'
    assert emitted_states == [(None, 'expired')]
    h.assert_trace_contract()


async def test_u106_expired_choice_is_closed_before_the_event(agent_call, monkeypatch):
    h = agent_call
    choice, emitted_states = await expire_choice(h, monkeypatch, early=True)
    assert_expired(h, choice, emitted_states)


async def test_u106_ask_user_after_expiry_is_not_already_open(agent_call, monkeypatch):
    h = agent_call
    await expire_choice(h, monkeypatch, early=True)
    result = await h.tool('ask_user', ASK)
    assert result == {'ok': False, 'error': 'interrupt_budget_exhausted', 'used': 1, 'budget': 1}
    assert len(h.events('pending_choice_created')) == 1


async def test_u106_on_time_expiry_unchanged(agent_call, monkeypatch):
    h = agent_call
    choice, emitted_states = await expire_choice(h, monkeypatch, early=False)
    assert_expired(h, choice, emitted_states)


async def test_u106b_registry_stuck_open_still_notifies_once_and_ask_user_moves_on(agent_call):
    from dataclasses import replace

    h = agent_call
    h.spec.compiled.policy['global']['interrupt_budget'] = 1
    await h.boot()
    registry = h.deps.pending
    pending = SimpleNamespace(create=registry.create, current=registry.current,
        get=registry.get, decision_of=registry.decision_of,
        expire_due=lambda: [registry.current(h.spec.call_id)])
    toolbox = h.call._toolbox
    toolbox.deps = replace(toolbox.deps, pending=pending)
    result = await h.tool('ask_user', ASK)
    choice = pending.get(result['choice_id'])
    timeout = h.spec.compiled.policy['global']['ask_user']['timeout_sec']
    await h.clock.advance(round(timeout * 1000))
    await settle()

    assert pending.current(h.spec.call_id).status == 'open'
    assert len(h.events('pending_expired')) == 1
    assert h.event_payloads() == [{'event': 'user_choice',
        'choice_id': choice.choice_id, 'status': 'expired'}]

    await toolbox._expire(choice)
    assert len(h.events('pending_expired')) == 1
    assert len(h.event_payloads()) == 1
    result = await h.tool('ask_user', ASK)
    assert result == {'ok': False, 'error': 'interrupt_budget_exhausted', 'used': 1, 'budget': 1}
    assert len(h.events('pending_choice_created')) == 1
    h.assert_trace_contract()
