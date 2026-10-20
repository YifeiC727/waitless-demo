"""E174: execute immediately, deliver repeated results once silence ends.

All clocks, provider streams and tool dependencies are offline. The live tap,
registry/compiler, tool handlers and trace persistence remain production code.
"""
import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams

from lane_D.agent_support import settings, spec, engine, compiled, line_number, store, trace, agent_call, invoke_tool
from call_engine._support import settle
from fakes import FakeTrace
from waitless import models as m
from waitless.call.agent_tools import AgentToolbox, OutputMonitor, load_tool_error_hints
from waitless.call.live import TranscriptTap
from waitless.call.media_gate import MediaGate
from waitless.call.pipeline import build_sonic_service
from waitless.decision.pending import PendingRegistry

STATUS = {'phase': 'hold', 'status_line': 'Waiting for the other side'}
ASK = {'question': 'Which day?', 'options': [{'label': 'Today'}, {'label': 'Tomorrow'}]}
JEV = {'question': 'Which authorized option?', 'options': ['One', 'Two']}


class RepeatHarness:
    def __init__(self, spec, settings, clock, store, monkeypatch):
        self.spec, self.settings, self.clock = spec, settings, clock
        policy = yaml.safe_load((settings.skills_dir / 'decide-option/policy.yaml').read_text())
        spec.compiled.policy['global'] = copy.deepcopy(policy['global'])
        spec.compiled.policy['global']['interrupt_budget'] = 4
        self.trace = FakeTrace(clock)
        self.pending = PendingRegistry(clock)
        self.injector = SimpleNamespace(send_event=AsyncMock())
        self.deps = SimpleNamespace(settings=settings, clock=clock, trace=self.trace,
            pending=self.pending, store=store, jev=None, on_ended=AsyncMock())
        self.media = MediaGate(dtmf=settings.engine.dtmf, clock=clock)
        self.monitor = OutputMonitor(clock=clock, media_gate=self.media)
        self.monitor.wait_idle = AsyncMock()
        self.box = AgentToolbox(spec=spec, deps=self.deps, pipeline_task=None,
            media_gate=self.media, monitor=self.monitor, injector=self.injector,
            carrier=SimpleNamespace(hangup=AsyncMock()), call_sid='offline',
            error_hints=load_tool_error_hints(settings.skills_dir))
        self.sonic = build_sonic_service(spec.compiled, settings, tools=self.box.schema())
        self.sonic._stream = object()
        self.sonic._connected_time = clock.now().timestamp()
        monkeypatch.setattr('waitless.call.pipeline.time', SimpleNamespace(
            time=lambda: clock.now().timestamp(), monotonic=time.monotonic))
        self.box.register(self.sonic)
        self.tap = TranscriptTap(deps=self.deps, spec=spec, media_gate=self.media,
            toolbox=self.box, monitor=self.monitor)
        self.tasks = []

    def events(self, kind):
        return [e for e in self.trace.events(call_id=self.spec.call_id) if e.event_type == kind]

    async def invoke(self, name='update_status', args=None, *, callback=None, ident=None):
        result = []
        async def collect(value):
            result.append(value)
            if callback:
                await callback(value)
        params = FunctionCallParams(function_name=name, tool_call_id=ident or uuid4().hex,
            arguments=copy.deepcopy(STATUS if args is None else args), llm=self.sonic,
            pipeline_worker=None, context=LLMContext(messages=[]), result_callback=collect)
        await self.box._invoke(name, params)
        assert len(result) == 1
        return result[0]

    async def start(self, name='update_status', args=None):
        received = []
        async def callback(value):
            received.append(value)
        task = asyncio.create_task(self.invoke(name, args, callback=callback))
        self.tasks.append(task)
        await settle(30)
        return task, received

    async def speech(self, text='New speech', *, role='USER', stage='FINAL', response=None):
        await self.tap.on_provider_text(m.ProviderTextEvent(role=role, stage=stage,
            text=text, t_ms=self.clock.monotonic_ms(), response=response, stop_reason='END_TURN'))
        await settle(30)

    def ledger(self):
        calls, results = self.events('agent_tool_call'), self.events('agent_tool_result')
        assert len(calls) == len(results)
        assert {e.action['tool_call_id'] for e in calls} == {e.action['tool_call_id'] for e in results}
        assert len({e.action['tool_call_id'] for e in results}) == len(results)


@pytest.fixture
async def repeat_call(spec, settings, fake_clock, store, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('offline: provider connection forbidden')
    from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService
    monkeypatch.setattr(AWSNovaSonicLLMService, 'create_client', forbidden)
    monkeypatch.setattr(AWSNovaSonicLLMService, '_start_connecting', forbidden)
    h = RepeatHarness(spec, settings, fake_clock, store, monkeypatch)
    try:
        yield h
    finally:
        await h.box.close()
        await h.tap.close()
        for task in h.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*h.tasks, return_exceptions=True)


async def test_first_call_and_end_call_are_never_held(repeat_call):
    h = repeat_call
    assert await h.invoke() == {'ok': True}
    for _ in range(2):
        assert await h.invoke('end_call', {'outcome': 'needs_follow_up', 'summary': 'Waiting'}) == {'ok': True}
    assert not h.events('agent_tool_held')
    h.ledger()


async def test_four_status_wordings_repeat_by_name_and_timeout_does_not_reset(repeat_call):
    h = repeat_call
    for index, wording in enumerate(('Waiting', 'Still waiting', 'Listening', 'Standing by')):
        task, received = await h.start(args={**STATUS, 'status_line': wording})
        if index:
            assert not task.done() and received == []
            # Side effect has already run; only delivery is held.
            assert h.box.status_line == wording
            await h.clock.advance(19999)
            assert not task.done()
            await h.clock.advance(1)
            result = await asyncio.wait_for(task, 1)
            assert result['hint'] == h.box._error_hints['repeated_without_speech']
        else:
            assert await task == {'ok': True}
    assert [e.action['reason'] for e in h.events('agent_tool_released')] == ['timeout'] * 3
    h.ledger()


@pytest.mark.parametrize('name,args', [('ask_user', ASK), ('ask_jev', JEV)])
async def test_alternating_tools_second_round_is_held(repeat_call, name, args):
    h = repeat_call
    await h.invoke(name, args)
    await h.invoke()
    tasks = [await h.start(name, args), await h.start()]
    assert all(not t.done() and not received for t, received in tasks)
    assert len(h.events('agent_tool_held')) == 2
    await h.speech()
    for t, received in tasks:
        await asyncio.wait_for(t, 1)
        assert len(received) == 1
    assert [e.action['reason'] for e in h.events('agent_tool_released')] == ['spoke', 'spoke']
    h.ledger()


async def test_same_millisecond_speech_broadcast_resets_all_waiters(repeat_call):
    h = repeat_call
    await h.invoke()
    tasks = [await h.start() for _ in range(8)]
    assert len(h.events('agent_tool_held')) == 8
    await h.speech()
    await asyncio.gather(*(t for t, _ in tasks))
    assert await h.invoke() == {'ok': True}  # Same clock tick, new epoch.
    assert len(h.events('agent_tool_held')) == 8
    assert all(len(received) == 1 for _, received in tasks)
    h.ledger()


async def test_speech_while_tool_executes_prevents_hold(repeat_call, monkeypatch):
    h = repeat_call
    await h.invoke()
    entered, finish = asyncio.Event(), asyncio.Event()
    async def execute(*args):
        entered.set()
        await finish.wait()
        return {'ok': True}
    monkeypatch.setattr(h.box, '_update_status', execute)
    task, _ = await h.start()
    assert entered.is_set() and not task.done()
    await h.speech()
    finish.set()
    assert await asyncio.wait_for(task, 1) == {'ok': True}
    assert not h.events('agent_tool_held')
    h.ledger()


@pytest.mark.parametrize('role', ['USER', 'ASSISTANT'])
async def test_real_tap_final_paths_release_original_result(repeat_call, role):
    h = repeat_call
    await h.invoke()
    task, received = await h.start()
    assert not task.done()
    await h.speech(role=role, response=1 if role == 'ASSISTANT' else None)
    if role == 'ASSISTANT':
        assert received == []
        await h.tap.on_response_end(1, h.clock.monotonic_ms())
    assert await asyncio.wait_for(task, 1) == {'ok': True}
    assert h.events('agent_tool_released')[0].action['reason'] == 'spoke'
    h.ledger()


async def test_tap_missing_final_chunk_flushes_at_six_seconds(repeat_call):
    h = repeat_call
    await h.invoke()
    task, _ = await h.start()
    await h.speech(role='ASSISTANT', stage='SPECULATIVE', response=3)
    await h.speech(role='ASSISTANT', stage='SPECULATIVE', response=3)
    await h.speech('Partial real speech', role='ASSISTANT', response=3)
    await h.tap.on_response_end(3, h.clock.monotonic_ms())
    await settle(20)
    await h.clock.advance(5999)
    assert not task.done()
    await h.clock.advance(1)
    assert await asyncio.wait_for(task, 1) == {'ok': True}
    assert len(h.events('waitless_utterance')) == 1


@pytest.mark.parametrize('role,stage,text', [
    ('USER', 'SPECULATIVE', 'tentative'), ('ASSISTANT', 'SPECULATIVE', 'tentative'),
    ('USER', 'FINAL', ' \n '), ('ASSISTANT', 'FINAL', ' \t '),
    ('USER', 'FINAL', '[Waitless event] {"event":"user_choice"}'),
    ('ASSISTANT', 'FINAL', '[Waitless event] {"event":"keys_pressed"}'),
])
async def test_non_speech_does_not_release(repeat_call, role, stage, text):
    h = repeat_call
    await h.invoke()
    task, received = await h.start()
    await h.speech(text, role=role, stage=stage)
    assert not task.done() and received == []
    await h.speech('Actual company speech')
    await asyncio.wait_for(task, 1)
    h.ledger()


@pytest.mark.parametrize('reason', ['closed', 'handoff'])
async def test_release_before_close_or_handoff_events_and_no_new_holds(repeat_call, monkeypatch, reason):
    h = repeat_call
    await h.invoke()
    task, received = await h.start()
    assert not task.done()
    if reason == 'closed':
        await h.box.close()
        await h.box.close()
        assert (await asyncio.wait_for(task, 1))['error'] == 'call_ended'
        assert (await h.invoke())['error'] == 'call_ended'
    else:
        finish = asyncio.Event()
        async def executing(*args):
            await finish.wait()
            return {'ok': True}
        monkeypatch.setattr(h.box, '_update_status', executing)
        in_flight, _ = await h.start()
        assert await h.invoke('request_handoff', {'reason': 'Verification'}) == {'status': 'handoff_ready'}
        await settle(50)
        assert await task == {'ok': True}
        started = h.events('handoff_started')[0].seq
        assert h.events('agent_tool_released')[0].seq < started
        assert all(e.seq < started for e in h.events('agent_tool_result'))
        finish.set()
        assert await asyncio.wait_for(in_flight, 1) == {'ok': True}
        assert len(h.events('agent_tool_held')) == 1
        window = [e.event_type for e in h.trace.events() if e.seq > started]
        # Only in-flight results and the explicit content-free gate facts.
        from tests.lane_D.i19b_trace_support import GATE_FACTS
        assert set(window) <= {'agent_tool_result'} | GATE_FACTS
    assert len(received) == 1
    assert [e.action['reason'] for e in h.events('agent_tool_released')] == [reason]
    h.ledger()


@pytest.mark.parametrize('order', ['speech-close', 'close-speech', 'timeout-speech', 'handoff-close'])
async def test_competing_releases_have_one_terminal_and_no_tasks(repeat_call, order):
    h = repeat_call
    await h.invoke()
    task, received = await h.start()
    assert not task.done()
    if order == 'speech-close':
        await h.speech()
        await h.box.close()
    elif order == 'close-speech':
        await h.box.close()
        await h.speech()
    elif order == 'timeout-speech':
        await h.clock.advance(20000)
        await h.speech()
    else:
        await h.invoke('request_handoff', {'reason': 'Verify'})
        await h.box.close()
    await asyncio.wait_for(task, 1)
    await h.box.close()
    await settle(30)
    assert len(received) == len(h.events('agent_tool_released')) == 1
    assert not h.box._tasks
    assert h.clock.pending_sleepers == 0
    h.ledger()


@pytest.mark.parametrize('when', ['executing', 'held'])
async def test_cancellation_records_once_propagates_and_never_callbacks(repeat_call, monkeypatch, when):
    h = repeat_call
    await h.invoke()
    if when == 'executing':
        async def blocked(*a):
            await asyncio.Event().wait()
        monkeypatch.setattr(h.box, '_update_status', blocked)
    task, received = await h.start()
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert received == []
    assert h.events('agent_tool_result')[-1].action['result']['error'] == 'cancelled'
    await h.speech()
    await h.clock.advance(20000)
    assert received == []
    h.ledger()
    await h.box.close()
    assert h.clock.pending_sleepers == 0


async def test_close_during_execution_cannot_register_a_late_hold(repeat_call, monkeypatch):
    h = repeat_call
    await h.invoke()
    async def ignores_cancel(*a):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {'ok': True}
    monkeypatch.setattr(h.box, '_update_status', ignores_cancel)
    task, _ = await h.start()
    await h.box.close()
    assert (await asyncio.wait_for(task, 1))['error'] == 'call_ended'
    assert not h.events('agent_tool_held')
    h.ledger()


@pytest.mark.parametrize('finish', ['answer', 'expire'])
async def test_ask_user_side_effects_answer_or_expiry_precede_old_asked_result(repeat_call, finish):
    h = repeat_call
    first = await h.invoke('ask_user', ASK)
    choice = h.pending.current(h.spec.call_id)
    await h.box.on_user_answer(choice, '1')
    # Registry is independently authoritative: emulate the public answer boundary.
    h.pending.answer(choice.choice_id, '1')
    task, received = await h.start('ask_user', ASK)
    assert not task.done() and received == []
    current = h.pending.current(h.spec.call_id)
    assert current.choice_id != first['choice_id']
    guard, _ = await h.start('ask_user', ASK)
    assert len(h.events('pending_choice_created')) == h.box.interrupts_used == 2
    if finish == 'answer':
        h.pending.answer(current.choice_id, '2')
        await h.box.on_user_answer(current, '2')
    else:
        # Expiry itself is not speech and does not release old results.
        await h.clock.advance(20000)
    assert h.injector.send_event.await_count == 2
    if finish == 'answer':
        assert not task.done()
        await h.speech()
    result = await asyncio.wait_for(task, 1)
    protected = await asyncio.wait_for(guard, 1)
    assert result['status'] == 'asked'
    assert protected['error'] == 'choice_already_open'
    assert protected['hint'] == h.box._error_hints['choice_already_open']
    assert len(h.events('pending_choice_created')) == 2
    h.ledger()


async def test_calls_are_isolated_and_trace_round_trips_minimal_actions(repeat_call, tmp_path):
    h = repeat_call
    await h.invoke()
    task, _ = await h.start()
    assert not task.done()
    other_spec = h.spec.model_copy(update={'call_id': 'other-call'})
    other = AgentToolbox(spec=other_spec, deps=h.deps, pipeline_task=None, media_gate=h.media,
        monitor=h.monitor, injector=h.injector, carrier=None, call_sid='other')
    other.register(h.sonic)
    try:
        received = []
        params = FunctionCallParams(function_name='update_status', tool_call_id='other',
            arguments=STATUS, llm=h.sonic, pipeline_worker=None, context=LLMContext(messages=[]),
            result_callback=AsyncMock(side_effect=lambda r: received.append(r)))
        await other._invoke('update_status', params)
        assert received == [{'ok': True}] and not task.done()
    finally:
        await other.close()
    await h.speech()
    await task
    from waitless.store import Store
    from waitless.trace import Trace
    db = Store(tmp_path / 'repeat.sqlite3')
    try:
        trace = Trace(db, h.clock)
        held, released = h.events('agent_tool_held'), h.events('agent_tool_released')
        assert set(held[0].action) == {'tool', 'tool_call_id'}
        assert set(released[0].action) == {'tool', 'tool_call_id', 'reason', 'held_ms'}
        assert released[0].action['held_ms'] == 0
        for event in (*held, *released):
            assert event.event_type not in m.CONSEQUENTIAL_EVENTS
            trace.event(event)
        assert [e.action for e in trace.events(call_id=h.spec.call_id)] == [e.action for e in (*held, *released)]
    finally:
        db.close()
    h.ledger()


@pytest.mark.parametrize('field,values', [
    ('repeat_hold_ms', [None, True, False, '5000', 0, -1, 20001]),
    ('continuation_hold_margin_ms', [None, True, False, '10000', 0, -1]),
])
def test_required_strict_configuration(repeat_call, field, values):
    from waitless.decision.policy import GlobalPolicy
    h = repeat_call
    cls = GlobalPolicy if field == 'repeat_hold_ms' else m.EngineConfig
    raw = copy.deepcopy(h.spec.compiled.policy['global']) if field == 'repeat_hold_ms' else h.settings.engine.model_dump()
    missing = dict(raw)
    missing.pop(field, None)
    with pytest.raises(ValidationError):
        cls.model_validate(missing)
    for value in values:
        with pytest.raises(ValidationError):
            cls.model_validate({**raw, field: value})
    assert cls.model_validate({**raw, field: 1})


async def test_registry_compiler_toolbox_5000ms_end_to_end(repeat_call, skills_dir, tmp_path, test_profile):
    from g_support import copy_skills, edit_yaml, profile, task_card, standing_rules
    from waitless.skills.loader import load_skills
    from waitless.skills.compiler import compile_call
    from waitless.decision.registry import load_registry
    h = repeat_call
    root = copy_skills(skills_dir, tmp_path)
    edit_yaml(root / 'decide-option/policy.yaml', lambda raw: raw['global'].update(repeat_hold_ms=5000))
    skills = load_skills(root)
    registry = load_registry(skills)
    assert registry.policy_raw()['global']['repeat_hold_ms'] == 5000
    compiled = compile_call(task_card(), skills, profile(test_profile), standing_rules(), [])
    h.spec.compiled.policy['global'] = compiled.policy['global']
    await h.invoke()
    task, received = await h.start()
    assert not task.done()
    await h.clock.advance(4999)
    assert received == []
    await h.clock.advance(1)
    assert (await task)['hint'] == h.box._error_hints['repeated_without_speech']
    assert h.events('agent_tool_released')[0].action['held_ms'] == 5000
    h.ledger()


async def test_note_speech_listener_returns_before_waiters_finish(repeat_call):
    h = repeat_call
    await h.invoke()
    task, received = await h.start()
    assert not task.done()
    await h.tap.on_provider_text(m.ProviderTextEvent(role='USER', stage='FINAL',
        text='Company has spoken', t_ms=h.clock.monotonic_ms(), stop_reason='END_TURN'))
    # The listener has no await on the hold/drain; notification is synchronous.
    assert received == []
    assert await asyncio.wait_for(task, 1) == {'ok': True}


async def test_pending_handoff_disables_new_holds_before_window(repeat_call):
    h = repeat_call
    await h.invoke()
    idle = asyncio.Event()
    async def waiting(*args):
        await idle.wait()
    h.monitor.wait_idle.side_effect = waiting
    assert await h.invoke('request_handoff', {'reason': 'Verify'}) == {'status': 'handoff_ready'}
    assert await asyncio.wait_for(h.invoke(), 1) == {'ok': True}
    assert not h.events('agent_tool_held') and not h.events('handoff_started')
    h.ledger()


@pytest.mark.parametrize('cause', ['disconnect', 'end_call'])
async def test_live_close_paths_release_hold_and_finish_call(agent_call, monkeypatch, cause):
    h = agent_call
    await h.boot()
    # Offline Sonic double has no provider session; this test exercises the live
    # lifecycle and wiring after the real service qualification tests above.
    monkeypatch.setattr(h.sonic, 'repeat_hold_allowed', lambda hold_ms: True, raising=False)
    await h.tool('update_status', STATUS)
    task = asyncio.create_task(invoke_tool(h.sonic, 'update_status', STATUS))
    try:
        await settle(100)
        assert not task.done() and len(h.events('agent_tool_held')) == 1
        if cause == 'disconnect':
            await h.call._on_line_disconnected(h.transport, None)
        else:
            await h.tool('end_call', {'outcome': 'needs_follow_up', 'summary': 'Still waiting'})
            await h.clock.advance(h.settings.engine.handoff.announce_idle_ms + 1)
            await settle(200)
        assert (await asyncio.wait_for(task, 1))['error'] == 'call_ended'
        assert h.call.ended
        assert [e.action['reason'] for e in h.events('agent_tool_released')] == ['closed']
        h.assert_trace_contract()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_callback_delivery_stays_managed_without_duplicate_ledger(repeat_call):
    h = repeat_call
    entered = asyncio.Event()
    async def callback(result):
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(h.invoke(callback=callback))
    h.tasks.append(task)
    await asyncio.wait_for(entered.wait(), 1)
    await h.box.close()
    assert task.cancelled()
    assert len(h.events('agent_tool_result')) == 1
    h.ledger()


async def test_speech_then_close_without_a_scheduler_turn_keeps_first_reason(repeat_call):
    h = repeat_call
    await h.invoke()
    task, received = await h.start()
    assert not task.done()
    h.box.note_speech()
    # close runs before any waiter gets another scheduler turn.
    await h.box.close()
    assert await task == {'ok': True}
    assert [e.action['reason'] for e in h.events('agent_tool_released')] == ['spoke']
    assert received == [{'ok': True}]
    h.ledger()
