"""Callback errors belong to active finish waiters, after all resource steps."""
import asyncio
import gc
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import waitless.call.live as live
from lane_A.test_u119_app import env, real_live_session
from .conftest import turns


def summaries(call):
    return [e for e in call.deps.trace.events(call_id=call.call_id)
            if e.event_type == 'action' and e.action.get('kind') == 'teardown_summary']


@pytest.mark.parametrize('error_type', [RuntimeError, TimeoutError])
async def test_active_waiters_share_business_error_after_carrier_and_summary(call, error_type):
    # Swallowing callback errors, rethrowing to later callers, or aborting cleanup is wrong.
    entered, release = asyncio.Event(), asyncio.Event()
    error = error_type('callback business failure')
    async def callback(_):
        entered.set()
        await release.wait()
        raise error
    call.deps.on_ended = callback
    call.deps.settings.engine.speaker.teardown_step_timeout_ms = 1000
    first = asyncio.create_task(call._finish(call.call_id))
    await entered.wait()
    second = asyncio.create_task(call._finish(call.call_id))
    cancelled = asyncio.create_task(call._finish(call.call_id))
    await turns()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert not call._finish_task.done()
    release.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert outcomes[0] is error and outcomes[1] is error
    call.carrier.aclose.assert_awaited_once()
    assert not call._finish_waiters
    ended, = call.deps.trace.of_type('call_ended')
    summary, = summaries(call)
    assert ended.action['teardown_errors'] == []
    assert [(r['stage'], r['error_type']) for r in summary.action['teardown_errors']] == [('on_ended', error_type.__name__)]
    assert summary.seq > ended.seq
    await call._finish(call.call_id)  # Entered after owner completed: no old exception.
    assert len(summaries(call)) == 1
    call.carrier.aclose.assert_awaited_once()


@pytest.mark.parametrize('target', ['end_output', 'bridge', 'carrier'])
async def test_other_step_exception_is_recorded_without_escaping(call, target):
    # Only the callback's timely business Exception escapes, never arbitrary cleanup failures.
    async def fail(*_):
        raise LookupError('resource failure')
    if target == 'end_output':
        call._media_gate.clear_output = fail
    elif target == 'bridge':
        call._bridge = SimpleNamespace(close=fail)
    else:
        call.carrier.aclose = fail
    await call._finish(call.call_id)
    assert call.deps.on_ended.calls == [call.call_id]
    assert any(r['error_type'] == 'LookupError' for r in live.TEARDOWN_STATE[call.call_id])
    assert len(summaries(call)) == 1


@pytest.mark.parametrize('mode', ['cancelled', 'timeout_then_error'])
async def test_callback_cancel_or_expired_budget_does_not_escape(call, mode):
    async def callback(_):
        if mode == 'cancelled':
            raise asyncio.CancelledError
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError('after budget expired')
    call.deps.on_ended = callback
    await call._finish(call.call_id)
    expected = 'CancelledError' if mode == 'cancelled' else 'TimeoutError'
    assert [(r['stage'], r['error_type']) for r in live.TEARDOWN_STATE[call.call_id]] == [('on_ended', expected)]
    call.carrier.aclose.assert_awaited_once()
    assert len(summaries(call)) == 1


async def test_cancelled_reentry_does_not_receive_error_and_unobserved_owner_is_retrieved(call):
    # Owner errors without a live waiter must still be retrieved; cancelled finally cannot wait.
    owned = live.LiveCall(call.spec.model_copy(update={'call_id': uuid4().hex}), call.deps, carrier=call.carrier)
    owned._owns_carrier = True
    entered, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def callback(_):
        entered.set()
        await release.wait()
        raise RuntimeError('callback failure without waiter')
    owned.deps.on_ended = callback
    owned.deps.settings.engine.speaker.teardown_step_timeout_ms = 1000
    loop = asyncio.get_running_loop()
    warnings = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: warnings.append(context.get('message')))
    async def entry():
        try:
            await asyncio.Event().wait()
        finally:
            await owned._finish(owned.call_id)
            returned.set()
    caller = asyncio.create_task(entry())
    try:
        await turns()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert returned.is_set() and owned._finish_task is not None
        await entered.wait()
        assert not owned._finish_task.done()
        release.set()
        await turns()
        assert owned._finish_task.done()
        owned.carrier.aclose.assert_awaited_once()
        assert len(summaries(owned)) == 1
        del caller, owned
        gc.collect()
        await turns()
        assert warnings == []
    finally:
        release.set()
        loop.set_exception_handler(old_handler)


async def test_remote_retry_after_callback_error_closes_owned_carrier_once(env, monkeypatch):
    # A completed owner's old exception must not block retry's owned-client close.
    manager, injected = await real_live_session(env)
    carrier = SimpleNamespace(hangup=AsyncMock(side_effect=[RuntimeError('offline'), None]), aclose=AsyncMock())
    monkeypatch.setattr(live, 'CarrierClient', lambda **kwargs: carrier)
    session = live.LiveCall(injected.spec, manager.deps)
    session._call_sid = 'owned-retry-sid'
    manager._sessions[session.call_id] = session
    error = RuntimeError('local result failure')
    session.deps.on_ended = AsyncMock(side_effect=error)
    first = await manager.hangup('t')
    assert first.remote_hangup_pending and session.remote_hangup_pending
    assert carrier.aclose.await_count == 0
    assert [(r['stage'], r['error_type']) for r in live.TEARDOWN_STATE[session.call_id]] == [('on_ended', 'RuntimeError')]
    second = await manager.hangup('t')
    assert not second.remote_hangup_pending and not session.remote_hangup_pending
    assert carrier.aclose.await_count == 1
    assert session.deps.on_ended.await_count == 1
    assert len([e for e in env.trace.events(call_id=session.call_id) if e.event_type == 'call_ended']) == 1
    assert len(summaries(session)) == 1
    third = await manager.hangup('t')
    assert third.status == 'already_ended' and not third.remote_hangup_pending
    assert carrier.hangup.await_count == 2 and carrier.aclose.await_count == 1
    assert all(c.args == ('owned-retry-sid',) for c in carrier.hangup.await_args_list)


async def test_disconnect_background_waiter_retrieves_its_callback_error(call):
    # Reading the owner's exception alone cannot retrieve the wrapper task's error.
    async def callback(_):
        raise RuntimeError('background callback failure')
    owned = live.LiveCall(call.spec.model_copy(update={'call_id': uuid4().hex}), call.deps, carrier=call.carrier)
    owned._owns_carrier = True
    owned.deps.on_ended = callback
    loop = asyncio.get_running_loop()
    warnings = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: warnings.append(context.get('message')))
    try:
        await owned._on_line_disconnected(None, None)
        await turns()
        assert owned._finish_task.done() and not owned._background
        owned.carrier.aclose.assert_awaited_once()
        assert len(summaries(owned)) == 1
        del owned
        gc.collect()
        await turns()
        assert warnings == []
    finally:
        loop.set_exception_handler(old_handler)
