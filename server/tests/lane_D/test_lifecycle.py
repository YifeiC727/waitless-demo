"""Live lifecycle with agent tools and continuation-safe data delivery."""
import pytest
from pipecat.frames.frames import LLMContextFrame
from call_engine._support import settle, wait_until
from lane_D.agent_support import *
from lane_D.test_d51_tools import ASK
from waitless.call.injector import SonicTextInjector
from waitless.call.agent_tools import event_text


@pytest.mark.parametrize('completed', [False, True], ids=['abandoned', 'completed'])
async def test_continuation_observed_before_any_injection(make_fake_sonic, trace, spec, fake_clock, completed):
    sonic = make_fake_sonic()
    injector = SonicTextInjector(sonic, trace=trace, call_id=spec.call_id, task_id=spec.task_id, clock=fake_clock)
    sonic.begin_continuation()
    await injector.flush()
    old_stream = sonic._stream
    if completed:
        sonic.complete_continuation()
    else:
        sonic.clear_handoff_flag()
    text = event_text({'event': 'handoff_ended'})
    await injector.send_event(text)
    assert injector.pending == 0
    assert (sonic._stream is old_stream) is (not completed)
    assert sonic.sent_texts == [text]
    assert sonic.injections[0].generation == (2 if completed else 1)
    for _ in range(3):
        assert await injector.flush() == 0
    assert not trace.of_type('injection_sent', call_id=spec.call_id)


async def test_live_call_seeds_sonic_context(agent_call, monkeypatch):
    h = agent_call
    h.component_tools = False
    contexts = []
    original = h.sonic.process_frame
    async def capture(frame, direction):
        if isinstance(frame, LLMContextFrame):
            contexts.append(frame.context)
        await original(frame, direction)
    monkeypatch.setattr(h.sonic, 'process_frame', capture)
    await h.boot()
    await wait_until(lambda: contexts)
    assert contexts == [h.call._context]
    assert contexts[0].get_messages() == []
    assert not getattr(contexts[0].tools, 'standard_tools', [])


async def test_pending_answer_uses_selected_label(agent_call):
    h = agent_call
    await h.boot()
    await h.tool('ask_user', ASK)
    choice = h.call.status().pending_choice
    await h.call.answer(choice.choice_id, choice.options[0].id)
    assert h.event_payloads()[-1]['label'] == 'Monday'
    h.assert_trace_contract()
