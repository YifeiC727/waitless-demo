import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import waitless.call.live as live
from .conftest import Stubborn, trigger, turns


@pytest.mark.asyncio
async def test_finish_waiters_share_task_and_cancellation_does_not_cancel_owner(call):
    block = asyncio.Event()
    entered = asyncio.Event()
    async def end_output():
        entered.set()
        await block.wait()
    call._media_gate.end_output = end_output
    first = asyncio.create_task(call._finish(call.call_id))
    await entered.wait()
    second = asyncio.create_task(call._finish(call.call_id))
    await turns()
    try:
        assert not second.done()
        owner = call._finish_task
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not owner.cancelled()
        block.set()
        await second
        assert owner is call._finish_task and owner.done()
        assert call.deps.on_ended.calls == [call.call_id]
        call.carrier.aclose.assert_awaited_once()
    finally:
        block.set()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError, asyncio.CancelledError, TimeoutError])
@pytest.mark.parametrize('trace_broken', [False, True])
async def test_finish_each_failure_continues_and_records(call, error, trace_broken):
    async def fail():
        if error is TimeoutError:
            await asyncio.Event().wait()
        raise error()
    call._media_gate.clear_output = fail
    if trace_broken:
        call.deps.trace.event = lambda e: (_ for _ in ()).throw(OSError())
    await call._finish(call.call_id)
    assert call._media_gate.output_state == 'ended'
    assert call.deps.on_ended.calls == [call.call_id]
    call.carrier.aclose.assert_awaited_once()
    assert any(r['stage'] == 'end_output' for r in live.TEARDOWN_STATE[call.call_id])
    call._keypad.close.assert_awaited_once()
    call._task.cancel.assert_awaited_once()
    if not trace_broken:
        ended, = call.deps.trace.of_type('call_ended')
        assert any(r['stage'] == 'end_output' for r in ended.action['teardown_errors'])
        summary, = [e for e in call.deps.trace.of_type('action') if e.action['kind'] == 'teardown_summary']
        assert summary.seq > ended.seq


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['bridge', 'on_ended', 'carrier'])
@pytest.mark.parametrize('outcome', ['done', 'error', 'cancel'])
@pytest.mark.parametrize('trace_broken', [False, True])
async def test_post_ended_unreclaimed_snapshot_and_late_callback(call, target, outcome, trace_broken):
    block = Stubborn(outcome)
    if target == 'bridge':
        call._bridge = SimpleNamespace(close=block)
    elif target == 'on_ended':
        call.deps.on_ended = block
    else:
        call.carrier.aclose = block
    if trace_broken:
        call.deps.trace.event = lambda e: (_ for _ in ()).throw(OSError())
    await call._finish(call.call_id)
    cid = call.call_id
    task, = [t for t, (c, _) in live.UNRECLAIMED.items() if c == cid]
    assert not task.done() and block.cancelled
    records = live.TEARDOWN_STATE[cid]
    assert any(r['error_type'] == 'Unreclaimed' for r in records)
    if not trace_broken:
        ended, = call.deps.trace.of_type('call_ended')
        before = ended.model_dump(mode='json')
        assert ended.action['teardown_unreclaimed'] == []
        summary, = [e for e in call.deps.trace.of_type('action') if e.action['kind'] == 'teardown_summary']
        assert summary.action['teardown_unreclaimed']
        snapshot = summary.model_dump(mode='json')
    block.release.set()
    await turns()
    assert task not in live.UNRECLAIMED
    expected = {'done': 'LateDone', 'error': 'LookupError', 'cancel': 'LateCancelled'}[outcome]
    assert records[-1]['error_type'] == expected
    if not trace_broken:
        assert ended.model_dump(mode='json') == before
        assert summary.model_dump(mode='json') == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize('trace_broken', [False, True])
async def test_finish_commit_pending_never_cancels_commit_and_waiter_may_cancel(call, trace_broken):
    block = asyncio.Event()
    entered = asyncio.Event()
    original = call._media_gate.clear_output
    count = 0
    async def clear():
        nonlocal count
        count += 1
        if count == 1:
            entered.set()
            await block.wait()
        await original()
    call._media_gate.clear_output = clear
    call.deps.settings.engine.speaker.teardown_step_timeout_ms = 1000
    handoff = asyncio.create_task(call.system_handoff(trigger(), kind='needs_user'))
    await entered.wait()
    call.deps.settings.engine.speaker.teardown_step_timeout_ms = 15
    if trace_broken:
        call.deps.trace.event = lambda e: (_ for _ in ()).throw(OSError())
    finisher = asyncio.create_task(call._finish(call.call_id))
    await turns()
    finisher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finisher
    await call._finish(call.call_id)
    assert call.ended
    assert not call._handoff_commit.cancelled() and not call._handoff_commit.done()
    assert any(r['error_type'] == 'CommitPending' for r in live.TEARDOWN_STATE[call.call_id])
    assert call.deps.on_ended.calls == [call.call_id]
    call.carrier.aclose.assert_awaited_once()
    block.set()
    await handoff
    assert call._failure_hangup is None
    assert not call.deps.trace.of_type('handoff_announced', 'handoff_ready')


@pytest.mark.asyncio
async def test_clean_finish_has_no_pending_owned_tasks(call):
    await call.system_handoff(trigger(), kind='live_human')
    await call._finish(call.call_id)
    await turns()
    assert call._finish_task.done() and call._handoff_commit.done()
    assert not [t for t, (cid, _) in live.UNRECLAIMED.items() if cid == call.call_id]
    assert not [t for t in call._background if not t.done()]
    assert call._media_gate.output_state == 'ended'


@pytest.mark.asyncio
async def test_failure_hangup_with_repeated_cleaner_failure_still_finishes(call):
    async def fail():
        raise RuntimeError()
    call._media_gate.clear_output = fail
    await call.system_handoff(trigger(), kind='live_human')
    await call._failure_hangup
    assert call.ended and call.deps.on_ended.calls == [call.call_id]
    call.carrier.hangup.assert_awaited_once()
    call.carrier.aclose.assert_awaited_once()
    assert any(r['stage'] == 'end_output' for r in live.TEARDOWN_STATE[call.call_id])


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['done', 'error', 'cancel'])
@pytest.mark.parametrize('trace_broken', [False, True])
async def test_external_finish_retains_commit_child_after_active_call_is_removed(call, outcome, trace_broken):
    import gc
    import weakref
    from uuid import uuid4
    # Construct outside the fixture cache so deleting the active reference is real.
    owned = live.LiveCall(call.spec.model_copy(update={'call_id': uuid4().hex}),
                          call.deps, carrier=call.carrier)
    for name in ('_media_gate', '_toolbox', '_tap', '_keypad', '_listen_hub', '_task', '_call_sid'):
        setattr(owned, name, getattr(call, name))
    owned._owns_carrier = True
    block = Stubborn(outcome)
    original = owned._media_gate.clear_output
    count = 0
    async def clear():
        nonlocal count
        count += 1
        if count == 1:
            await block()
        else:
            await original()
    owned._media_gate.clear_output = clear
    if trace_broken:
        owned.deps.trace.event = lambda e: (_ for _ in ()).throw(OSError())
    waiter = asyncio.create_task(owned.system_handoff(trigger(), kind='needs_user'))
    await block.started.wait()
    await owned._finish(owned.call_id)
    await waiter
    cid = owned.call_id
    task, = [t for t, (c, _) in live.UNRECLAIMED.items() if c == cid]
    assert any(r['error_type'] == 'Unreclaimed' and r['after_ended']
               for r in live.TEARDOWN_STATE[cid])
    assert owned._failure_hangup is None
    assert owned.deps.on_ended.calls == [cid]
    owned.carrier.aclose.assert_awaited_once()
    assert not owned.deps.trace.of_type('handoff_announced', 'handoff_ready')
    ref = weakref.ref(owned)
    del owned, waiter
    gc.collect()
    assert task in live.UNRECLAIMED and ref() is not None
    block.release.set()
    await turns()
    gc.collect()
    assert task not in live.UNRECLAIMED
    assert ref() is None
    assert live.TEARDOWN_STATE[cid][-1]['error_type'] == {
        'done': 'LateDone', 'error': 'LookupError', 'cancel': 'LateCancelled'}[outcome]
