"""Real LiveCall/toolbox/keypad/tap ownership; only carrier/provider are offline fakes."""
import asyncio
import json
import time

import pytest
import waitless.call.live as live
from lane_D.agent_support import agent_call
from .conftest import turns


@pytest.mark.parametrize('origin', ['end_call', 'hangup', 'disconnect', 'repeat', 'external_cancel'])
async def test_finish_origins_complete_without_recycle_timeout(agent_call, monkeypatch, origin):
    # Missing waiter exclusion cancels initiators; missing cancelling reentry
    # leaves the disconnect->carrier cancellation->finally path waiting on itself.
    h = agent_call
    await h.boot()
    observed = []
    started = time.monotonic()
    original_event = h.trace.event
    original_set = h.call._ended_event.set
    def mark_ended():
        observed.append({'kind': 'ended', 't_ms': h.clock.monotonic_ms(),
                         'elapsed_ms': round((time.monotonic() - started) * 1000, 3)})
        original_set()
    def event(ev):
        result = original_event(ev)
        if ev.event_type in ('hangup', 'call_ended'):
            observed.append({'kind': ev.event_type, 't_ms': ev.t_ms, 'seq': h.events()[-1].seq,
                             'elapsed_ms': round((time.monotonic() - started) * 1000, 3)})
        return result
    monkeypatch.setattr(h.call._ended_event, 'set', mark_ended)
    monkeypatch.setattr(h.trace, 'event', event)
    if origin == 'disconnect':
        async def hangup(_):
            await h.call._on_line_disconnected(None, None)
            await asyncio.Event().wait()
        monkeypatch.setattr(h.call.carrier, 'hangup', hangup)
    initiators = []
    if origin in ('end_call', 'disconnect'):
        await h.tool('end_call', {'outcome': 'confirmed', 'summary': 'Done'})
        initiators = list(h.call._toolbox._tasks)
        await h.clock.advance(9000)
    elif origin == 'hangup':
        caller = asyncio.create_task(h.call.hangup('user_hangup'))
        h.call._background.add(caller)
        caller.add_done_callback(h.call._background.discard)
        initiators = [caller]
    else:
        entered, release = asyncio.Event(), asyncio.Event()
        original = h.call._media_gate.clear_output
        async def clear():
            entered.set()
            await release.wait()
            await original()
        monkeypatch.setattr(h.call._media_gate, 'clear_output', clear)
        first = asyncio.create_task(h.call._finish(h.call.call_id))
        await entered.wait()
        second = asyncio.create_task(h.call._finish(h.call.call_id))
        initiators = [first, second]
        await turns()
        assert not second.done()
        owner = h.call._finish_task
        if origin == 'external_cancel':
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not owner.cancelled()
        release.set()
    await turns(300)
    assert h.call._finish_task.done(), 'finish still waiting for a recycle timeout'
    assert h.on_ended.calls == [h.call.call_id]
    if origin != 'disconnect':
        assert all(t.done() and not t.cancelled() for t in initiators
                   if not (origin == 'external_cancel' and t is initiators[0]))
    else:
        assert all(t.done() and t.cancelled() for t in initiators)
    assert not live.TEARDOWN_STATE.get(h.call.call_id)
    assert not h.call._finish_waiters
    assert not [t for t, (cid, _) in live.UNRECLAIMED.items() if cid == h.call.call_id]
    endings = [e.event_type for e in h.events() if e.event_type in ('hangup', 'call_ended')]
    assert endings == (['hangup', 'call_ended'] if origin in ('end_call', 'disconnect') else ['call_ended'])
    assert [o['kind'] for o in observed if o['kind'] in ('ended', 'call_ended')] == ['ended', 'call_ended']
    print('FINISH_TIMELINE ' + json.dumps({'origin': origin, 'events': observed}))


@pytest.mark.parametrize('component,collection', [('toolbox', '_tasks'), ('keypad', '_active'), ('tap', '_keypad_tasks')])
async def test_real_close_preserves_excluded_waiter_and_reclaims_other_work(agent_call, component, collection):
    # Ignoring exclude cancels the active finisher; excluding all work leaks the other task.
    h = agent_call
    await h.boot()
    obj = getattr(h.call, '_' + component)
    release = asyncio.Event()
    waiter = asyncio.create_task(release.wait())
    other = asyncio.create_task(release.wait())
    if collection == '_active':
        obj._active = waiter
    else:
        getattr(obj, collection).update((waiter, other))
    try:
        await obj.close(exclude=frozenset({waiter}))
        assert not waiter.done()
        if collection != '_active':
            assert other.cancelled()
        release.set()
        await waiter
    finally:
        release.set()
        await asyncio.gather(waiter, other, return_exceptions=True)
