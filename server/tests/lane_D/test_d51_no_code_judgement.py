from lane_D.agent_support import *
from lane_D.test_d51_tools import ASK

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
    await h.boot()
    await h.sonic.emit_user_text('For service, press 1.')
    await h.tool('press_keys', {'digits': '1'})
    await h.sonic.emit_user_text('Which date would you prefer?')
    await h.tool('ask_user', ASK)
    choice = h.call.status().pending_choice
    await h.call.answer(choice.choice_id, choice.options[0].id)
    await h.tool('request_handoff', {'reason': 'Verify'})
    await h.clock.advance(h.settings.engine.handoff.announce_idle_ms + 10)
    await settle(200)
    joined = await h.call.join()
    await h.open_handoff(joined)
    await h.call.return_to_agent(joined.handoff_id)
    await h.tool('end_call', {'outcome': 'needs_follow_up', 'summary': 'Follow up'})
    await h.clock.advance(9000)
    await wait_until(lambda: h.call.ended)
    h.assert_trace_contract()

async def test_live_call_never_calls_gate(agent_call):
    await full_call(agent_call)

async def test_no_guard_or_injection_events(agent_call):
    h = agent_call
    await full_call(h)
    assert not {'guard_released', 'guard_blocked', 'say_mismatch', 'injection_sent', 'resume_injected'} & {e.event_type for e in h.events()}
    assert not {'call_round', 'said_as_decided'} & {e.decision_point for e in h.events('decision')}
