import asyncio
import gc
import weakref
from unittest.mock import AsyncMock
import pytest
import waitless.call.live as live
from .conftest import trigger, turns, Stubborn


@pytest.mark.asyncio
async def test_lock_released_and_repeated_waiter_shares_commit(call):
    block = asyncio.Event()
    entered = asyncio.Event()
    original = call._media_gate.clear_output
    async def clear():
        entered.set()
        await block.wait()
        await original()
    call._media_gate.clear_output = clear
    first = asyncio.create_task(call.system_handoff(trigger(), kind='needs_user'))
    await turns()
    try:
        assert entered.is_set()
        assert not call._handoff_lock.locked()
        commit = call._handoff_commit
        second = asyncio.create_task(call.system_handoff(trigger(), kind='live_human'))
        await turns()
        assert not second.done()
        assert call._handoff_commit is commit
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not commit.cancelled()
        block.set()
        await second
        assert call._handoff_kind == 'needs_user'
        assert len(call.deps.trace.of_type('handoff_ready')) == 1
        assert call._failure_hangup is None
    finally:
        block.set()
        await asyncio.gather(first, return_exceptions=True)
        await call._finish(call.call_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError, asyncio.CancelledError, TimeoutError])
@pytest.mark.parametrize('trace_broken', [False, True])
async def test_commit_failure_converges_even_without_trace(call, error, trace_broken):
    # C1-a verifies commit -> failure-hangup ownership; full finish is C1-b.
    async def finish(cid):
        call._ended_event.set()
    call._finish = AsyncMock(side_effect=finish)
    async def clear():
        if error is TimeoutError:
            await asyncio.Event().wait()
        raise error()
    call._media_gate.clear_output = clear
    if trace_broken:
        call.deps.trace.event = lambda e: (_ for _ in ()).throw(OSError())
    await call.system_handoff(trigger(), kind='live_human')
    await call._failure_hangup
    assert call.ended
    call.carrier.hangup.assert_awaited_once()
    assert not call.deps.trace.of_type('handoff_announced', 'handoff_ready')
    assert live.TEARDOWN_STATE[call.call_id]
    assert not call._handoff_commit.cancelled()


@pytest.mark.asyncio
async def test_announced_retained_when_ready_trace_fails(call):
    event = call.deps.trace.event
    def write(e):
        if e.event_type == 'handoff_ready':
            raise OSError()
        return event(e)
    call.deps.trace.event = write
    await call.system_handoff(trigger(), kind='live_human')
    await call._failure_hangup
    assert len(call.deps.trace.of_type('handoff_announced')) == 1
    assert not call.deps.trace.of_type('handoff_ready')
    assert call.ended


@pytest.mark.asyncio
async def test_precheck_is_inside_lock_and_fallback_uses_current_check(call):
    checks = []
    def check():
        checks.append(call._handoff_lock.locked())
        return False
    await call.system_handoff(trigger(), kind='needs_user', precheck=check)
    assert checks == [True]
    assert not call._system_handoff
    await call.system_handoff(trigger(), kind='needs_user', precheck=lambda: True)
    assert call._system_handoff
    await call._finish(call.call_id)


@pytest.mark.asyncio
async def test_stale_normal_trigger_does_not_commit(call):
    await call.system_handoff(dict(seq=0, decision_id='old'), kind='live_human')
    assert not call._system_handoff
    await call._finish(call.call_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['done', 'error', 'cancel'])
@pytest.mark.parametrize('trace_broken', [False, True])
async def test_ended_inflight_commit_retained_through_gc(call, outcome, trace_broken):
    block = Stubborn(outcome)
    call._media_gate.clear_output = block
    if trace_broken:
        call.deps.trace.event = lambda e: (_ for _ in ()).throw(OSError())
    waiter = asyncio.create_task(call.system_handoff(trigger(), kind='needs_user'))
    await turns()
    assert block.started.is_set()
    # External end while commit's independent child is still in progress.
    call._ended_event.set()
    await waiter
    tasks = [t for t, (cid, _) in live.UNRECLAIMED.items() if cid == call.call_id]
    assert len(tasks) == 1 and not tasks[0].done()
    cid = call.call_id
    rows = live.TEARDOWN_STATE[cid]
    assert any(r['error_type'] == 'Unreclaimed' and r['after_ended'] for r in rows)
    assert call._failure_hangup is None
    assert not call.deps.trace.of_type('handoff_announced', 'handoff_ready')
    ref = weakref.ref(call)
    del call
    gc.collect()
    assert tasks[0] in live.UNRECLAIMED
    block.release.set()
    await turns()
    assert tasks[0] not in live.UNRECLAIMED
    expected = {'done': 'LateDone', 'error': 'LookupError', 'cancel': 'LateCancelled'}[outcome]
    assert any(r['error_type'] == expected for r in live.TEARDOWN_STATE[cid])


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError, asyncio.CancelledError])
async def test_ended_step_exception_is_resource_record_only(call, error):
    async def clear():
        call._ended_event.set()
        raise error()
    call._media_gate.clear_output = clear
    await call.system_handoff(trigger(), kind='live_human')
    assert call._failure_hangup is None
    assert any(r['after_ended'] for r in live.TEARDOWN_STATE[call.call_id])
    assert not call.deps.trace.of_type('handoff_announced', 'handoff_ready')


@pytest.mark.asyncio
async def test_fallback_decision_copies_source_and_links_notifications(call):
    from call_engine._support import jev_exchange
    exchange = jev_exchange('agent_tool')
    source = dict(jev_request=exchange.request.model_dump(mode='json'),
                  jev_response=exchange.response.model_dump(mode='json'))
    await call.system_handoff(dict(seq=0, source_decision_id='source',
        reason_code='speaker_quiet_after_uncertain', quiet_audio_ms=5000,
        quiet_wall_ms=5000, **source), kind='needs_user', precheck=lambda: True)
    decision, = call.deps.trace.of_type('decision')
    assert decision.decision_id != 'source'
    assert decision.jev_request == source['jev_request']
    assert decision.jev_response == source['jev_response']
    assert decision.policy_decision == dict(intent='speaker_fallback', action='handoff',
        handoff_kind='needs_user', reason_code='speaker_quiet_after_uncertain',
        policy_rule='phone-agent/speaker-fallback', source_decision_id='source',
        quiet_audio_ms=5000, quiet_wall_ms=5000)
    assert all(e.decision_id == decision.decision_id
        for e in call.deps.trace.of_type('handoff_announced', 'handoff_ready'))
    await call._finish(call.call_id)


@pytest.mark.asyncio
async def test_existing_media_handoff_is_not_committed_again(call):
    await call._media_gate.enter_handoff()
    await call.system_handoff(trigger(), kind='live_human')
    assert not call._system_handoff
    assert not call.deps.trace.of_type('handoff_announced', 'handoff_ready')
    await call._finish(call.call_id)
