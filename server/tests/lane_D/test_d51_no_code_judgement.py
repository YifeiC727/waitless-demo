from lane_D.agent_support import *
from lane_D.test_d51_tools import ASK
from fakes import FakeJev, jev_choice

async def test_pipeline_has_no_output_guard_or_turn_detector(agent_call):
    h = agent_call
    await h.boot()
    def walk(proc):
        yield proc
        for child in getattr(proc, 'processors', []):
            yield from walk(child)
    processors = list(walk(h.call._task._pipeline))
    assert not any(type(p).__name__ in {'OutputGuard', 'TurnDetector'} for p in processors)
    assert sum(type(p).__name__ == 'OutputMonitor' for p in processors) == 1
    assert sum(type(p).__name__ == 'LLMUserAggregator' for p in processors) == 1
    assert sum(type(p).__name__ == 'LLMAssistantAggregator' for p in processors) == 1

async def full_call(h):
    h.component_tools = False
    h.deps.jev = FakeJev([
        {'keypad': jev_choice('1'), 'speaker': jev_choice('menu_or_recording'), 'transfer': jev_choice('none')},
        {'keypad': jev_choice('none'), 'speaker': jev_choice('live_human'), 'transfer': jev_choice('none')}])
    await h.boot()
    await h.sonic.emit_user_text('For service, press 1.')
    await settle(300)
    await h.sonic.emit_user_text('The representative has answered.')
    await settle(300)
    joined = await h.call.join()
    await h.open_handoff(joined)
    await h.call.hangup('user_hangup')
    await wait_until(lambda: h.call.ended)
    assert not h.events('handoff_ended') and not h.sonic.registered_functions
    h.assert_trace_contract()

async def test_live_call_never_calls_gate(agent_call):
    await full_call(agent_call)

async def test_no_guard_or_injection_events(agent_call):
    h = agent_call
    await full_call(h)
    assert not {'guard_released', 'guard_blocked', 'say_mismatch', 'injection_sent', 'resume_injected'} & {e.event_type for e in h.events()}
    assert not {'call_round', 'said_as_decided'} & {e.decision_point for e in h.events('decision')}
