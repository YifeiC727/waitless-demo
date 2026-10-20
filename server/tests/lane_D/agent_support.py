"""D51 test utilities; frozen support and shared fakes stay untouched."""
import asyncio
import json
from importlib import import_module, util
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fakes import FakeSonicService, FakeJev
from pipecat.services.llm_service import FunctionCallParams
from pipecat.processors.aggregators.llm_context import LLMContext
from waitless.decision.pending import PendingRegistry
from call_engine._support import LiveCallHarness, drive, settle, wait_until
from call_engine.conftest import compiled, engine, line_number, settings, spec, store, trace


class AgentSonicService(FakeSonicService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.registrations = {}

    def register_function(self, name, handler, **kwargs):
        self.registrations[name] = kwargs
        super().register_function(name, handler, **kwargs)

    async def invoke_tool(self, name, args):
        return await invoke_tool(self, name, args)


async def invoke_tool(sonic, name, args):
    results = []
    assert name in sonic.registered_functions, f'missing agent tool: {name}'
    async def callback(result, *, properties=None):
        results.append(result)
    params = FunctionCallParams(function_name=name, tool_call_id=uuid4().hex,
        arguments=args, llm=sonic, pipeline_worker=None, context=LLMContext(messages=[]),
        result_callback=callback)
    await sonic.registered_functions[name](params)
    assert len(results) == 1
    return results[0]


class ExplodingGate:
    async def decide_round(self, *args, **kwargs):
        raise AssertionError('live called decide_round')
    async def decide(self, *args, **kwargs):
        raise AssertionError('live called decide')


class AgentCallHarness(LiveCallHarness):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.component_tools = True
        from waitless.call import pipeline, live
        # G1 assets are external to this lane. Model the frozen speaker contract
        # explicitly in offline fixtures until that independent branch merges.
        kwargs['monkeypatch'].setattr(live, 'load_speaker_config', lambda path: {
            'instructions': 'Classify the current speaker from context.', 'min_confidence': .7,
            'options': {'menu_or_recording': 'menu', 'hold': 'hold',
                'virtual_agent': 'bot', 'live_human': 'person'}})
        def build_sonic_service(compiled, settings, *, tools=None):
            self.sonic_builds.append(compiled)
            self.sonic.init_kwargs.update(tools=tools, system_instruction=compiled.sonic_prompt)
            return self.sonic
        kwargs['monkeypatch'].setattr(pipeline, 'build_sonic_service', build_sonic_service)
        self.deps.pending = PendingRegistry(self.clock)
        import yaml
        from pathlib import Path
        global_policy = yaml.safe_load((Path(self.settings.skills_dir) / 'decide-option/policy.yaml').read_text())['global']
        spec = self.spec
        spec.compiled.policy.setdefault('global', global_policy)
        for key, value in global_policy.items():
            spec.compiled.policy['global'].setdefault(key, value)

    async def boot(self):
        await self.start()
        await self.connect_line()
        assert getattr(self.call, '_toolbox', None) is not None, 'live needs AgentToolbox'
        if self.component_tools:
            self.enable_tool_components()

    def enable_tool_components(self):
        """Explicit isolated toolbox regression wiring; live attaches no handlers.

        Old E174/toolbox tests keep exercising their components. Tests of the
        deployed live pipeline set component_tools=False before boot.
        """
        box = self.call._toolbox
        if getattr(box, '_component_fixture', False):
            return
        box._component_fixture = True
        box.spec = box.spec.model_copy(update={'mode': 'text-sim'})
        sonic = self.call._injector.llm
        box.register(sonic)
        if hasattr(sonic, 'init_kwargs'):
            sonic.init_kwargs['tools'] = box.schema()
        self.call._context.set_tools(box.schema())
        self.call._tap.keypad = None
        g = self.call._media_gate
        g.output_state = 'allowed'
        if hasattr(sonic, 'frame_factory'):
            original = sonic.frame_factory
            def tagged(ev):
                identity = (0, ev.response)
                if ev.kind == 'response_start':
                    g.bind_response(identity)
                for frame in original(ev):
                    frame.response_identity = identity
                    frame.company_seq = g.company_seq
                    yield frame
            sonic.frame_factory = tagged

    async def tool(self, name, args):
        if self.component_tools:
            self.enable_tool_components()
        return await drive(invoke_tool(self.sonic, name, args), self.clock)

    def events(self, kind=None):
        return [e for e in self.trace.events(call_id=self.spec.call_id)
                if kind is None or e.event_type == kind]

    def event_payloads(self):
        return [json.loads(i.text.removeprefix('[Waitless event] ')) for i in self.sonic.injections]

    def assert_trace_contract(self):
        events = self.events()
        calls = self.events('agent_tool_call')
        results = self.events('agent_tool_result')
        assert len(calls) == len(results)
        assert {e.action['tool_call_id'] for e in calls} == {e.action['tool_call_id'] for e in results}
        assert not any(e.event_type == 'injection_sent' for e in events)
        decisions = {e.decision_id: e for e in self.events('decision')}
        for e in events:
            if e.event_type in {'dtmf_sent', 'pending_choice_created', 'user_answered',
                'pending_expired', 'handoff_announced', 'handoff_ready', 'handoff_started',
                'handoff_ended', 'hangup'}:
                assert e.decision_id in decisions
                d = decisions[e.decision_id]
                assert d.decision_point == 'agent_tool'
                if d.policy_decision['intent'] == 'keypad':
                    assert d.policy_decision['reason_code'] == 'jev_keypad'
                    assert d.policy_decision['policy_rule'] == 'phone-agent/keypad'
                elif d.policy_decision['intent'] == 'speaker':
                    assert d.policy_decision['policy_rule'] == 'phone-agent/speaker'
                else:
                    assert d.policy_decision['reason_code'] == 'agent_tool'
                    assert d.policy_decision['policy_rule'] == 'phone-agent'


async def reach_handoff_ready(h, engine):
    from fakes import jev_choice
    h.component_tools = False
    h.deps.jev = FakeJev(default={'keypad': jev_choice('none'), 'speaker': jev_choice('live_human')})
    await h.start()
    await h.connect_line()
    await h.sonic.emit_user_text('The representative has answered.')
    await settle(300)
    assert h.call.status().phase == 'handoff_ready'


@pytest.fixture
async def agent_call(monkeypatch, settings, spec, fake_clock, trace, fake_carrier, store):
    # Fixed prompt and file-backed skill fixture: G is implemented in a separate lane.
    spec.compiled.policy = {'global': {'interrupt_budget': 2, 'repeat_hold_ms': 20000,
        'ask_user': {'timeout_sec': 20, 'max_options': 3}}}
    spec.compiled.agent_tool_descriptions = {name: f'skill description: {name}' for name in
        ('update_status', 'ask_user', 'ask_jev', 'request_handoff', 'end_call')}
    h = AgentCallHarness(monkeypatch=monkeypatch, settings=settings, spec=spec,
        clock=fake_clock, trace=trace, gate=ExplodingGate(),
        sonic=AgentSonicService(clock=fake_clock), carrier=fake_carrier, store=store)
    h.deps.jev = FakeJev()
    try:
        yield h
    finally:
        await h.close()


def agent_module():
    assert util.find_spec('waitless.call.agent_tools') is not None, 'agent tools are not implemented'
    return import_module('waitless.call.agent_tools')


def tool_schema(compiled):
    return agent_module().AgentToolbox(spec=SimpleNamespace(compiled=compiled), deps=None,
        pipeline_task=None, media_gate=None, monitor=None, injector=None, carrier=None, call_sid=None).schema()
