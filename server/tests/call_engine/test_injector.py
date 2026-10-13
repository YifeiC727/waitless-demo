"""AC-71：决定要说的话经 SonicTextInjector 注入，trace 记一条 injection_sent（含 text 和 decision_id）；
注入后 Sonic 的下一段回应放行。

机制（tech §6.5、§7.1 AC-71；S0 V3）：调私有 `_send_text_event(text=SONIC_INSTRUCTION_PREFIX + text,
role=Role.USER, interactive=True)`；构造时检查 pipecat.__version__ == "1.12.0"（K3）。
injection_sent 的 action 按 A-08 约定为 {"text": 不含前缀的文字, "template": <键或 null>}。
"""

from typing import Any

import pytest

from call_engine._support import (
    SAY_TEMPLATES,
    MiniCall,
    make_result,
    record_decision,
    response_audio,
    audio_bytes,
)
from fakes import SonicResponse
from waitless import models as m


def make_injector(sonic: Any, trace: Any, spec: Any, clock: Any) -> Any:
    from waitless.call.injector import SonicTextInjector

    return SonicTextInjector(sonic, trace=trace, call_id=spec.call_id, task_id=spec.task_id, clock=clock)


async def test_ac71_inject_sends_prefixed_user_text_and_traces_it(
    make_fake_sonic: Any, trace: Any, spec: Any, fake_clock: Any
) -> None:
    sonic = make_fake_sonic()
    injector = make_injector(sonic, trace, spec, fake_clock)
    decision = record_decision(trace, make_result("say", say_text="The service address is 1200 W Example St.", intent="open_field"),
                               task_id=spec.task_id, call_id=spec.call_id, turn_seq=1)
    await injector.inject(decision.say_text, decision.decision_id)
    assert [(i.text, i.role, i.interactive, i.status) for i in sonic.injections] == [
        (m.SONIC_INSTRUCTION_PREFIX + decision.say_text, "USER", True, "sent")
    ]
    events = trace.of_type("injection_sent", call_id=spec.call_id)
    assert len(events) == 1
    assert events[0].decision_id == decision.decision_id
    assert events[0].action == {"text": decision.say_text, "template": None}
    assert trace.violations == []
    assert injector.pending == 0


async def test_ac71_inject_records_the_template_key(make_fake_sonic: Any, trace: Any, spec: Any, fake_clock: Any) -> None:
    sonic = make_fake_sonic()
    injector = make_injector(sonic, trace, spec, fake_clock)
    decision = record_decision(trace, make_result("handoff", intent="request_handoff", call_phase="live_rep"),
                               task_id=spec.task_id, call_id=spec.call_id, turn_seq=2)
    await injector.inject(SAY_TEMPLATES["handoff.announce"], decision.decision_id, template="handoff.announce")
    (event,) = trace.of_type("injection_sent", call_id=spec.call_id)
    assert (event.action or {}).get("template") == "handoff.announce"
    assert sonic.sent_texts == [m.SONIC_INSTRUCTION_PREFIX + SAY_TEMPLATES["handoff.announce"]]


def test_ac71_injector_refuses_other_pipecat_versions(
    monkeypatch: pytest.MonkeyPatch, make_fake_sonic: Any, trace: Any, spec: Any, fake_clock: Any
) -> None:
    import pipecat

    from waitless.call.injector import PIPECAT_VERSION_REQUIRED, PipecatVersionError

    assert PIPECAT_VERSION_REQUIRED == "1.12.0"
    make_injector(make_fake_sonic(), trace, spec, fake_clock)  # 1.12.0：可以构造
    monkeypatch.setattr(pipecat, "__version__", "1.13.0")
    with pytest.raises(PipecatVersionError):
        make_injector(make_fake_sonic(), trace, spec, fake_clock)


async def test_ac71_response_after_a_say_injection_reaches_the_line(
    spec: Any, deps: Any, engine: Any, make_fake_sonic: Any, trace: Any
) -> None:
    """对话中：Sonic 先擅自作答（扣住），决定是 say → 注入；注入后的下一段回应放行，擅自作答的那段不放。"""
    spoken = SonicResponse(text="We'd like to keep the deposit option, please.", audio_ms=240, chunk_ms=40)
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic, responses=[spoken]) as mc:
        mc.guard.set_state("conversation")
        await mc.sonic.emit_response(SonicResponse(text="Sure, autopay works.", audio_ms=400, chunk_ms=40))
        result = record_decision(
            trace, make_result("say", say_text="Please keep the deposit option.", intent="answer_precall:deposit_or_autopay"),
            task_id=spec.task_id, call_id=spec.call_id, turn_seq=3,
        )
        await mc.run(result)
    assert mc.sent_injections()[0] == result.say_text
    assert audio_bytes(mc.line_audio()) == response_audio(mc.sonic, 2)
    events = trace.of_type("injection_sent", call_id=spec.call_id)
    assert events and events[0].decision_id == result.decision_id
    assert trace.violations == []
