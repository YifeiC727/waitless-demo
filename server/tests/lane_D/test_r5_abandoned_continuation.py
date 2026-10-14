"""D-F3: abandoned continuation drains only when the original stream is ready."""
from types import SimpleNamespace

import pytest

from call_engine.conftest import compiled, engine, gate, line_number, settings, spec, store, trace
from call_engine._support import make_result, record_decision
from waitless.call.injector import SonicTextInjector
from waitless.models import SONIC_INSTRUCTION_PREFIX


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
    return record_decision(trace, make_result("say", say_text=f"Continue task step {seq}."),
        task_id=spec.task_id, call_id=spec.call_id, turn_seq=seq)


async def test_abandoned_continuation_before_inject_sends_once(spec, trace, fake_clock):
    injector, llm, sent = make_injector(spec, trace, fake_clock)
    old_stream = llm._stream
    assert await injector.flush() == 0
    llm._sc.handoff_in_progress = False
    result = decision(spec, trace, 1)
    await injector.inject(result.say_text, result.decision_id)
    await injector.flush()
    assert [s["text"] for s in sent] == [SONIC_INSTRUCTION_PREFIX + result.say_text]
    assert injector.pending == 0 and llm._stream is old_stream
    for _ in range(3):
        assert await injector.flush() == 0
    events = trace.of_type("injection_sent", call_id=spec.call_id)
    assert len(sent) == len(events) == 1
    assert events[0].decision_id == result.decision_id


@pytest.mark.parametrize("unready", [None, "_stream", "_prompt_name", "_audio_input_started"])
async def test_abandoned_continuation_drains_in_order_after_ready(unready, spec, trace, fake_clock):
    injector, llm, sent = make_injector(spec, trace, fake_clock)
    queued = [decision(spec, trace, seq) for seq in range(1, 4)]
    for result in queued:
        await injector.inject(result.say_text, result.decision_id)
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
    assert [s["text"] for s in sent] == [SONIC_INSTRUCTION_PREFIX + r.say_text for r in queued]
    events = trace.of_type("injection_sent", call_id=spec.call_id)
    assert [e.decision_id for e in events] == [r.decision_id for r in queued]
    assert [e.action["text"] for e in events] == [r.say_text for r in queued]
