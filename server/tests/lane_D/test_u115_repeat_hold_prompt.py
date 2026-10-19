"""Actual Pipecat handler/aggregator/provider prompt and toolResult paths offline."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (FunctionCallsStartedFrame, FunctionCallInProgressFrame,
    FunctionCallResultFrame, FunctionCallCancelFrame, InterruptionFrame, LLMContextFrame)
from pipecat.processors.aggregators.async_tool_messages import ASYNC_TOOL_INSTRUCTIONS
from pipecat.processors.aggregators.llm_response_universal import LLMAssistantAggregator
from pipecat.processors.frame_processor import FrameProcessor, FrameProcessorSetup, FrameDirection
from pipecat.services.llm_service import FunctionCallFromLLM
from pipecat.utils.asyncio.task_manager import TaskManager
from pipecat.utils.async_tool_cancellation import ASYNC_TOOL_CANCELLATION_INSTRUCTIONS
from waitless.skills.loader import load_skills
from lane_D.test_u115_repeat_hold import repeat_call, settings, spec, engine, compiled, line_number, store, STATUS
from call_engine._support import settle


class Wire:
    def __init__(self):
        self.events = []
        self.input_stream = self
    async def send(self, event):
        self.events.append(json.loads(event.value.bytes_)['event'])
    def results(self):
        return [e['toolResult'] for e in self.events if 'toolResult' in e]


@pytest.fixture
async def provider_path(repeat_call):
    h, s = repeat_call, repeat_call.sonic
    clock = SystemClock()
    clock.start()
    tm = TaskManager(loop=asyncio.get_running_loop())
    setup = FrameProcessorSetup(clock=clock, task_manager=tm,
        pipeline_worker=SimpleNamespace(app_resources=None, worker_runner=None))
    from pipecat.processors.aggregators.llm_context import LLMContext
    context = h.context = LLMContext(messages=[], tools=h.box.schema())
    a = h.aggregator = LLMAssistantAggregator(context)
    # Nova.setup starts a network connection: only initialize FrameProcessor.
    await FrameProcessor.setup(s, setup)
    await FrameProcessor.setup(a, setup)
    h.wire = Wire()
    s._stream, s._prompt_name, s._context = h.wire, 'offline-prompt', context
    h.frames, h.callbacks = [], []
    async def broadcast(frame_cls, **kwargs):
        frame = frame_cls(**kwargs)
        h.frames.append(frame)
        if isinstance(frame, FunctionCallsStartedFrame):
            await a._handle_function_calls_started(frame)
        elif isinstance(frame, FunctionCallInProgressFrame):
            await a._handle_function_call_in_progress(frame)
        elif isinstance(frame, FunctionCallResultFrame):
            await a._handle_function_call_result(frame)
        elif isinstance(frame, FunctionCallCancelFrame):
            await a._handle_function_call_cancel(frame)
    async def feedback(direction):
        await s._handle_context(context)
    s.broadcast_frame = broadcast
    s.push_frame = AsyncMock()
    a.push_context_frame = feedback
    try:
        yield h
    finally:
        await h.box.close()
        for task in list(tm.current_tasks()):
            if task is not asyncio.current_task():
                await tm.cancel_task(task)
        await FrameProcessor.cleanup(s)
        await FrameProcessor.cleanup(a)


async def wait_for(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.parametrize('new_user_context', [False, True])
async def test_real_handler_interruption_aggregator_sends_held_result_once(provider_path, new_user_context):
    h, s, a = provider_path, provider_path.sonic, provider_path.aggregator
    assert all(
        not s._functions[name].cancel_on_interruption for name in ('update_status', 'ask_user', 'ask_jev', 'request_handoff', 'end_call'))
    assert s._function_call_timeout_secs is None
    await h.invoke()  # Seed the silence epoch using the real toolbox.
    await s.run_function_calls([FunctionCallFromLLM(context=h.context,
        tool_call_id='held-call', function_name='update_status', arguments=STATUS)])
    await wait_for(lambda: h.events('agent_tool_held'))
    assert h.wire.results() == [] and s._function_call_tasks
    await s.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
    await a._handle_interruptions(InterruptionFrame())
    assert s._function_call_tasks
    if new_user_context:
        h.context.add_message({'role': 'user', 'content': 'New company speech'})
    await h.speech()
    await wait_for(lambda: not s._function_call_tasks)
    await s._process_completed_function_calls(send_new_results=True)
    await s._process_completed_function_calls(send_new_results=True)
    assert len(h.wire.results()) == 1
    start = [e['contentStart'] for e in h.wire.events if e.get('contentStart', {}).get('type') == 'TOOL']
    assert [e['toolResultInputConfiguration']['toolUseId'] for e in start] == ['held-call']
    assert json.loads(h.wire.results()[0]['content']) == {'ok': True}
    assert not any(isinstance(f, FunctionCallCancelFrame) for f in h.frames)
    h.ledger()


async def test_explicit_pipecat_deadline_cancels_hold_and_rejects_late_callback(provider_path, monkeypatch):
    h, s = provider_path, provider_path.sonic
    await h.invoke()
    original = s._functions['update_status'].handler
    async def capture(params):
        h.callbacks.append(params.result_callback)
        await original(params)
    s.register_function('update_status', capture, cancel_on_interruption=False, timeout_secs=.02)
    await s.run_function_calls([FunctionCallFromLLM(context=h.context,
        tool_call_id='deadline', function_name='update_status', arguments=STATUS)])
    await wait_for(lambda: h.events('agent_tool_held'))
    await wait_for(lambda: not s._function_call_tasks)
    assert h.events('agent_tool_result')[-1].action['result']['error'] == 'cancelled'
    assert not h.wire.results()
    frames = len([f for f in h.frames if isinstance(f, FunctionCallResultFrame)])
    await h.callbacks[0]({'ok': True})
    await h.speech()
    assert len([f for f in h.frames if isinstance(f, FunctionCallResultFrame)]) == frames
    assert not h.wire.results()
    h.ledger()


async def test_actual_composed_system_instruction_preserves_skill_and_other_addons(provider_path):
    h, s = provider_path, provider_path.sonic
    body = load_skills(h.settings.skills_dir)['phone-agent'].body
    await s._update_settings(s.Settings(system_instruction=body))
    async def handler(params):
        pass
    s.register_function('cancel_probe', handler, cancel_on_interruption=False, cancellable_by_llm=True)
    s.append_system_instruction('UNRELATED APPENDIX')
    s._filter_incomplete_user_turns = True
    s._user_turn_completion_config = SimpleNamespace(completion_instructions='TURN COMPLETION APPENDIX')
    await s.process_frame(LLMContextFrame(h.context), FrameDirection.DOWNSTREAM)
    for _ in range(3):
        s._compose_system_instruction()
    effective, _ = s.get_setup_params()
    assert body in effective
    assert ASYNC_TOOL_INSTRUCTIONS not in effective
    assert 'owed to the user' not in effective and 'State a short result outright' not in effective
    assert 'UNRELATED APPENDIX' in effective and 'TURN COMPLETION APPENDIX' in effective
    assert ASYNC_TOOL_CANCELLATION_INSTRUCTIONS in effective
    assert s._has_async_tools() and s._cancel_tool_names
    sent = []
    async def send_text(*, text, role, **kwargs):
        sent.append(text)
    s._send_text_event = send_text
    s._send_prompt_start_event = AsyncMock()
    s._send_audio_input_start_event = AsyncMock()
    s._ready_to_send_context = True
    s._receive_task_handler = AsyncMock()
    s._sc.start_monitor = lambda: None
    await s._finish_connecting_if_context_available()
    assert sent[0] == effective
    await s._update_settings(s.Settings(system_instruction=body + '\nRUNTIME UPDATE'))
    updated, _ = s.get_setup_params()
    assert body in updated and 'RUNTIME UPDATE' in updated
    assert ASYNC_TOOL_INSTRUCTIONS not in updated and 'UNRELATED APPENDIX' in updated
