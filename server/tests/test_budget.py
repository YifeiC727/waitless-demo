"""AC-51（G 侧）：同一通电话第二次需要打扰时被预算拦下，按 budget_exhausted 结束（G 线；tech §6.6、§7.1）。

第 3 步可见测试。interrupts_used 由调用方（LiveCall/TextCall）填进 DecisionContext；这里直接给出。
calls.outcome=needs_follow_up 由 A 线 results.finalize() 判定，A 线测；G 侧断言策略结果是 end + budget_exhausted。
"""

from pathlib import Path

from fakes import round_answers
from g_support import DOB_TEXT, FEE_TEXT, SURGE_TEXT, build_kit, profile, speech_turn


def _kit(skills_dir, test_profile, fake_jev, fake_clock):
    return build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))


async def test_ac51_second_interruption_is_refused(skills_dir: Path, test_profile: dict, fake_jev, fake_clock) -> None:
    kit = _kit(skills_dir, test_profile, fake_jev, fake_clock)
    fake_jev.push(
        round_answers("ask_user", menu_confidence=0.6, call_phase="live_rep", needs_user=0.7, stakes=3),
        round_answers("ask_user", menu_confidence=0.95, call_phase="live_rep", needs_user=0.9, stakes=3),
    )
    first = await kit.round(speech_turn(FEE_TEXT, seq=10), interrupts_used=0)
    assert first.action == "ask_user" and first.intent == "ask_user"

    second = await kit.round(speech_turn(SURGE_TEXT, seq=14), interrupts_used=1)
    assert second.action == "end"
    assert second.reason_code == "budget_exhausted"
    assert second.intent not in ("ask_user", "request_handoff")
    assert second.pending is None
    assert len(fake_jev.calls) == 2, "Jev is still asked; the budget rule acts on its answer"
    assert [e.decision_id for e in kit.trace.decisions()] == [first.decision_id, second.decision_id]


async def test_ac51_handoff_also_needs_budget(skills_dir: Path, test_profile: dict, fake_jev, fake_clock) -> None:
    kit = _kit(skills_dir, test_profile, fake_jev, fake_clock)
    fake_jev.push(round_answers("request_handoff", menu_confidence=0.95, call_phase="live_rep", needs_user=0.95, stakes=3))
    result = await kit.round(speech_turn(DOB_TEXT), interrupts_used=1)
    assert result.action == "end"
    assert result.reason_code == "budget_exhausted"
    assert result.intent != "request_handoff"
