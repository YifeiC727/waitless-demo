"""D-F3: abandoned continuation drains only when the original stream is ready."""
from types import SimpleNamespace

import pytest

from call_engine.conftest import compiled, engine, gate, line_number, settings, spec, store, trace
from call_engine._support import make_result, record_decision
from waitless.call.injector import SonicTextInjector
from waitless.call.agent_tools import event_text


def make_injector(spec, trace, clock):
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)

    llm = SimpleNamespace(_stream=object(), _prompt_name="active", _audio_input_started=True,
        _sc=SimpleNamespace(handoff_in_progress=True), _send_text_event=send)
    injector = SonicTextInjector(llm, trace=trace, call_id=spec.call_id,
        task_id=spec.task_id, clock=clock)
    return injector, llm, sent


def decision(spec, trace, seq):
    return SimpleNamespace(say_text=event_text({'event': 'user_choice', 'choice_id': str(seq), 'status': 'expired'}))


async def test_abandoned_continuation_before_inject_sends_once(spec, trace, fake_clock):
    injector, llm, sent = make_injector(spec, trace, fake_clock)
    old_stream = llm._stream
    assert await injector.flush() == 0
    llm._sc.handoff_in_progress = False
    result = decision(spec, trace, 1)
    await injector.send_event(result.say_text)
    await injector.flush()
    assert [s["text"] for s in sent] == [result.say_text]
    assert injector.pending == 0 and llm._stream is old_stream
    for _ in range(3):
        assert await injector.flush() == 0
    events = trace.of_type("injection_sent", call_id=spec.call_id)
    assert len(sent) == 1 and events == []


@pytest.mark.parametrize("unready", [None, "_stream", "_prompt_name", "_audio_input_started"])
async def test_abandoned_continuation_drains_in_order_after_ready(unready, spec, trace, fake_clock):
    injector, llm, sent = make_injector(spec, trace, fake_clock)
    queued = [decision(spec, trace, seq) for seq in range(1, 4)]
    for result in queued:
        await injector.send_event(result.say_text)
    assert injector.pending == len(queued) and not sent
    assert not trace.of_type("injection_sent", call_id=spec.call_id)
    assert await injector.flush() == 0
    llm._sc.handoff_in_progress = False
    if unready:
        ready_value = getattr(llm, unready)
        setattr(llm, unready, None)
        assert await injector.flush() == 0
        assert injector.pending == len(queued) and not sent
        setattr(llm, unready, ready_value)
    assert await injector.flush() == len(queued)
    assert injector.pending == 0
    for _ in range(3):
        assert await injector.flush() == 0
    assert [s["text"] for s in sent] == [r.say_text for r in queued]
    events = trace.of_type("injection_sent", call_id=spec.call_id)
    assert events == []
