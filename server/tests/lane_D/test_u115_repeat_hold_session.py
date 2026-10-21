"""Pin the continuation clock semantics to the actual offline 1.12.0 methods."""
import asyncio
import ast
import importlib.metadata
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService
from pipecat.services.aws.nova_sonic.session_continuation import SessionContinuationParams
from waitless.call.pipeline import TappedNovaSonicService
from lane_D.test_u115_repeat_hold import repeat_call, settings, spec, engine, compiled, line_number, store


def test_pipecat_112_connection_clock_semantics_are_pinned():
    assert importlib.metadata.version('pipecat-ai') == '1.12.0'
    tree = ast.parse(inspect.getsource(AWSNovaSonicLLMService))
    methods = []
    for method in tree.body[0].body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(method):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            if any(isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                   and t.value.id == 'self' and t.attr == '_connected_time' for t in targets):
                methods.append(method.name)
    assert sorted(methods) == ['__init__', '_disconnect', '_finish_connecting_if_context_available', '_run_sc_handoff']
    assert TappedNovaSonicService._run_sc_handoff is not AWSNovaSonicLLMService._run_sc_handoff  # v5.27 adds output cleanup on promotion.


@pytest.mark.parametrize('age', [330, 350, 359.9, 360, 440])
async def test_repeated_calls_in_continuation_zone_are_immediate(repeat_call, age):
    h = repeat_call
    h.sonic._connected_time = h.clock.now().timestamp() - age
    await h.invoke()
    task, received = await h.start()
    assert task.done() and await task == {'ok': True}
    assert received == [{'ok': True}]
    assert not h.events('agent_tool_held') and not h.events('agent_tool_released')
    h.ledger()


@pytest.mark.parametrize('hold_ms,margin_ms,threshold', [(20000, 10000, 360), (5000, 2500, 31)])
async def test_cutoff_uses_shared_params_and_milliseconds(repeat_call, hold_ms, margin_ms, threshold):
    h = repeat_call
    params = h.sonic._sc._params
    params.transition_threshold_seconds = threshold
    h.spec.compiled.policy['global']['repeat_hold_ms'] = hold_ms
    h.sonic._continuation_hold_margin_ms = margin_ms
    cutoff = threshold - hold_ms / 1000 - margin_ms / 1000
    h.sonic._connected_time = h.clock.now().timestamp() - cutoff + .001
    await h.invoke()
    task, received = await h.start()
    assert not task.done() and received == []
    await h.speech()
    await task
    h.sonic._connected_time = h.clock.now().timestamp() - cutoff
    await h.invoke()  # first in new epoch
    task, _ = await h.start()
    assert task.done() and await task == {'ok': True}
    assert len(h.events('agent_tool_held')) == 1
    assert h.sonic._sc._params is params


@pytest.mark.parametrize('state', ['missing_time', 'none_time', 'disconnecting', 'missing_stream', 'closed'])
async def test_invalid_connection_and_closed_toolbox_never_hold(repeat_call, state):
    h = repeat_call
    await h.invoke()
    if state == 'missing_time':
        del h.sonic._connected_time
    elif state == 'none_time':
        h.sonic._connected_time = None
    elif state == 'disconnecting':
        h.sonic._disconnecting = True
    elif state == 'missing_stream':
        h.sonic._stream = None
    else:
        await h.box.close()
    task, _ = await h.start()
    assert task.done()
    assert (await task).get('error') == ('call_ended' if state == 'closed' else None)
    assert not h.events('agent_tool_held')
    assert h.sonic.connected_at() is None if state != 'closed' else True
    h.ledger()


async def test_execution_crosses_cutoff_then_does_not_hold(repeat_call, monkeypatch):
    h = repeat_call
    await h.invoke()
    h.sonic._connected_time = h.clock.now().timestamp() - 329.9
    async def execute(*args):
        await h.clock.sleep(.1)
        return {'ok': True}
    monkeypatch.setattr(h.box, '_update_status', execute)
    task, _ = await h.start()
    assert not task.done()
    await h.clock.advance(100)
    assert await asyncio.wait_for(task, 1) == {'ok': True}
    assert not h.events('agent_tool_held')


@pytest.mark.parametrize('outcome', ['missing', 'send_error', 'concurrent', 'success'])
async def test_real_handoff_changes_age_only_after_success(repeat_call, monkeypatch, outcome):
    h = repeat_call
    s = h.sonic
    original = h.clock.now().timestamp() - 361
    s._connected_time = original
    s._sc.set_connected(original)
    s._prompt_name = 'old-prompt'
    old_stream = s._stream
    s.send_audio_input_start = AsyncMock()
    s._sc.close_old_session = AsyncMock()
    s._sc.cleanup_next_session = AsyncMock()
    async def receive(**kwargs):
        return
    s._receive_task_handler = receive
    def create_task(coro, **kwargs):
        task = asyncio.create_task(coro)
        h.tasks.append(task)
        return task
    s.create_task = create_task
    monkeypatch.setattr('pipecat.services.aws.nova_sonic.llm.time', SimpleNamespace(time=lambda: h.clock.now().timestamp()))
    if outcome != 'missing':
        s._sc._next_session = SimpleNamespace(client=None, stream=object(), prompt_name='new-prompt', input_audio_content_name='new-audio')
    if outcome == 'send_error':
        s.send_audio_input_start.side_effect = RuntimeError('offline send failure')
    if outcome == 'concurrent':
        s._sc._handoff_in_progress = True
    await s._run_sc_handoff()
    await h.invoke()
    task, received = await h.start()
    if outcome == 'success':
        assert s._stream is not old_stream and s._prompt_name == 'new-prompt'
        assert s.connected_at() == s._sc._connected_time == h.clock.now().timestamp()
        assert not task.done() and not received
        await h.speech()
    else:
        assert s._stream is old_stream and s._prompt_name == 'old-prompt'
        assert s.connected_at() == s._sc._connected_time == original
        assert task.done() and len(received) == 1
        assert not h.events('agent_tool_held')
    await task
    h.ledger()


async def test_real_initial_connection_and_disconnect_clock(repeat_call, monkeypatch):
    h, s = repeat_call, repeat_call.sonic
    s._connected_time = None
    s._ready_to_send_context = False
    await s._finish_connecting_if_context_available()
    assert s.connected_at() is None
    s._ready_to_send_context = True
    s._context = object()
    s._process_completed_function_calls = AsyncMock()
    s.get_llm_adapter = lambda: SimpleNamespace(get_llm_invocation_params=lambda *a, **k: {
        'tools': [], 'system_instruction': 'OFFLINE', 'messages': []}, from_standard_tools=lambda x: [])
    s._send_prompt_start_event = AsyncMock()
    s._send_text_event = AsyncMock()
    s._send_audio_input_start_event = AsyncMock()
    s._sc.start_monitor = lambda: None
    s.create_task = lambda coro, **kw: h.tasks.append(asyncio.create_task(coro)) or h.tasks[-1]
    s._receive_task_handler = AsyncMock()
    monkeypatch.setattr('pipecat.services.aws.nova_sonic.llm.time', SimpleNamespace(time=lambda: h.clock.now().timestamp()))
    await s._finish_connecting_if_context_available()
    assert s.connected_at() == s._sc._connected_time == h.clock.now().timestamp()
    s._disconnecting = True
    assert s.connected_at() is None
    s._disconnecting = False
    s._stream = SimpleNamespace(close=AsyncMock())
    s.cancel_task = AsyncMock()
    s._sc.stop_monitor = AsyncMock()
    monkeypatch.setattr('pipecat.services.aws.nova_sonic.llm.asyncio.sleep', AsyncMock())
    await s._disconnect()
    assert s.connected_at() is None and s._sc._connected_time is None
