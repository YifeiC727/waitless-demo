"""I-19 F3: real handshake, durable Store/ResultService and one-way finish barrier."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from pydantic import ValidationError
from lane_A.test_u119_app import env, real_live_session
from call_engine._support import AsgiWs, HANDOFF_HELLO, settle
from waitless import models as m


async def ready_call(env):
    manager, session = await real_live_session(env)
    session._phase = 'handoff_ready'
    session._handoff_id = 'handoff-test'
    session._handoff_decision_id = 'handoff-decision'
    session._system_handoff = True
    session._toolbox = SimpleNamespace(action_phase='handoff_ready', phase='live_rep',
        status_line='handoff_ready', interrupts_used=1, close=AsyncMock())
    cleaned = asyncio.Event()
    cleaned.set()
    session._media_gate = SimpleNamespace(in_handoff=True, _cleaned=cleaned,
        attach_bridge=lambda bridge: None, end_output=AsyncMock())
    env.trace.event(m.TraceEvent(task_id=session.task_id, call_id=session.call_id, event_type='decision',
        decision_id='handoff-decision', decision_point='agent_tool', jev_request={}, jev_response={}, policy_decision={}))
    return manager, session


async def attach(session, hello=HANDOFF_HELLO, configure=None):
    ws = AsgiWs(path='/ws/handoff/offline')
    await ws.websocket.accept()
    if configure:
        configure(ws.websocket)
    task = asyncio.create_task(session.attach_handoff(ws.websocket, session._handoff_id))
    ws.json(dict(hello))
    await settle(150)
    return ws, task


def saved(env, session):
    return next(r for r in env.store.list_calls(session.task_id) if r['call_id'] == session.call_id)


@pytest.mark.parametrize('cancel_waiter', [False, True])
@pytest.mark.parametrize('first_error', [None, RuntimeError, asyncio.CancelledError])
async def test_slow_confirmation_survives_company_end_and_waiter_cancel(env, cancel_waiter, first_error):
    manager, session = await ready_call(env)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def confirm(call_id):
        assert not session._handoff_lock.locked()
        calls.append(call_id)
        if len(calls) == 1 and first_error:
            raise first_error()
        entered.set()
        await release.wait()
        await manager.handoff_connected(call_id)
    session.deps.on_handoff_connected = confirm
    ws, joining = await attach(session)
    assert entered.is_set(), 'valid hello/ready must start durable confirmation'
    if cancel_waiter:
        joining.cancel()
        await asyncio.gather(joining, return_exceptions=True)
    finishing = asyncio.create_task(session._finish(session.call_id))
    await settle(100)
    assert not finishing.done(), 'finish must wait for accepted confirmation'
    assert saved(env, session)['result_json'] is None
    release.set()
    await asyncio.wait_for(finishing, 2)
    ws.disconnect()
    await asyncio.gather(joining, return_exceptions=True)
    result = m.CallResult.model_validate_json(saved(env, session)['result_json'])
    assert result.outcome == 'needs_follow_up' and result.awaiting_user_result
    assert saved(env, session)['user_joined_at']
    assert len(calls) == (2 if first_error else 1)
    assert not manager.results.llm.calls and not manager.results.gate.requests


@pytest.mark.parametrize('failure', ['exception', 'cancelled', 'timeout'])
async def test_confirmation_exhaustion_has_explicit_follow_up(env, monkeypatch, failure):
    manager, session = await ready_call(env)
    calls = []
    timeouts = []
    async def confirm(call_id):
        calls.append(call_id)
        if failure == 'cancelled': raise asyncio.CancelledError()
        if failure == 'exception': raise RuntimeError('offline store failure')
        await asyncio.Event().wait()
    session.deps.on_handoff_connected = confirm
    if failure == 'timeout':
        original = asyncio.wait_for
        bound = env.settings.engine.handoff.confirm_timeout_s
        async def accelerated(awaitable, timeout):
            if timeout == bound:
                timeouts.append(timeout)
                timeout = .01
            return await original(awaitable, timeout)
        monkeypatch.setattr(asyncio, 'wait_for', accelerated)
    ws, joining = await attach(session)
    assert calls, 'valid handshake must attempt persistence'
    ws.disconnect()
    await joining
    await session._finish(session.call_id)
    row = saved(env, session)
    result = m.CallResult.model_validate_json(row['result_json'])
    assert len(calls) == env.settings.engine.handoff.confirm_attempts
    if failure == 'timeout': assert timeouts == [bound] * len(calls)
    assert row['user_joined_at'] is None
    assert result.outcome == 'needs_follow_up' and not result.awaiting_user_result
    assert '接手记录未保存' in result.summary
    diagnostic = [e for e in env.trace.events(call_id=session.call_id)
                  if e.event_type == 'error' and (e.action or {}).get('code') == 'handoff_confirmation_failed']
    assert len(diagnostic) == 1
    assert diagnostic[0].action['attempts'] == len(calls)
    assert env.store.get_task(session.task_id)['status'] == 'needs_follow_up'
    assert not manager.results.llm.calls and not manager.results.gate.requests


async def test_reconnect_confirms_again_without_replacing_first_timestamp(env):
    manager, session = await ready_call(env)
    connected = AsyncMock(wraps=manager.handoff_connected)
    session.deps.on_handoff_connected = connected
    ws, joining = await attach(session)
    assert saved(env, session)['user_joined_at']
    first = saved(env, session)['user_joined_at']
    ws.disconnect()
    await joining
    await env.clock.advance(1000)
    ticket = await session.join()
    second, rejoining = await attach(session)
    assert connected.await_count == 2
    assert saved(env, session)['user_joined_at'] == first
    second.disconnect()
    await rejoining
    await session._finish(session.call_id)


@pytest.mark.parametrize('invalid', ['bad_hello', 'before_ready', 'ready_failure', 'stale_generation'])
async def test_unaccepted_handshake_never_registers_confirmation(env, monkeypatch, invalid):
    manager, session = await ready_call(env)
    connected = AsyncMock(wraps=manager.handoff_connected)
    session.deps.on_handoff_connected = connected
    if invalid == 'before_ready':
        checks = iter([False, True])
        monkeypatch.setattr(session, '_handoff_expired', lambda: next(checks))
    def configure(websocket):
        original = websocket.send_json
        async def send(value):
            if value.get('type') == 'ready' and invalid == 'ready_failure':
                raise RuntimeError('offline ready send failed')
            await original(value)
            if value.get('type') == 'ready' and invalid == 'stale_generation':
                session._handoff_generation += 1
        websocket.send_json = send
    ws, joining = await attach(session, {'type': 'bad'} if invalid == 'bad_hello' else HANDOFF_HELLO, configure)
    ws.disconnect()
    await asyncio.gather(joining, return_exceptions=True)
    assert connected.await_count == 0
    assert saved(env, session)['user_joined_at'] is None
    assert not session._handoff_ever_joined
    await session._finish(session.call_id)


@pytest.mark.parametrize('field', ['confirm_attempts', 'confirm_timeout_s'])
@pytest.mark.parametrize('bad', ['missing', None, True, '2', 0, -1])
def test_confirmation_policy_is_required_and_strict(engine_config_path, field, bad):
    data = yaml.safe_load(engine_config_path.read_text())
    if bad == 'missing': data['handoff'].pop(field, None)
    else: data['handoff'][field] = bad
    with pytest.raises(ValidationError):
        m.EngineConfig.model_validate(data)
