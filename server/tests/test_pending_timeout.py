"""AC-50（G 侧）：待决选项 20 秒没回答就过期，走 safe_default（G 线；tech §6.5 ask_user、§7.1）。

第 3 步可见测试。A 线的 get_call_status 读出 expired 由 A 线测；这里测 PendingRegistry 本身与策略里的
超时设定。时间全用 FakeClock，不睡真实时间。
"""

import itertools
from datetime import timedelta
from pathlib import Path

import pytest

from g_support import CALL_ID, loaded_policy, merged_policy_raw, option, profile, require_skills_dir, standing_rules, task_card
from waitless.decision.pending import PendingRegistry
from waitless.interfaces import ToolFailure
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills

QUESTION = "We can start on November 1 for a $25 fee, or November 3 at no charge. Which one?"
OPTIONS = [
    option("nov1", "November 1 with a $25 fee", "Start on November 1; $25 expedite fee"),
    option("nov3", "November 3, no fee", "Start on November 3 at no charge"),
]


def _registry(clock) -> PendingRegistry:
    ids = itertools.count(1)
    return PendingRegistry(clock, ids=lambda: f"ch-{next(ids)}")


def _create(reg: PendingRegistry, *, call_id: str = CALL_ID, decision_id: str = "dec-7", timeout_s: float = 20):
    return reg.create(
        call_id=call_id, decision_id=decision_id, question_text=QUESTION, options=OPTIONS, timeout_s=timeout_s
    )


def test_ac50_create_opens_a_choice_that_expires_in_twenty_seconds(fake_clock) -> None:
    reg = _registry(fake_clock)
    fake_clock.advance_sync(5_000)
    choice = _create(reg)
    assert choice.status == "open"
    assert choice.question_text == QUESTION
    assert [o.id for o in choice.options] == ["nov1", "nov3"]
    assert choice.created_at == fake_clock.now()
    assert choice.expires_at - choice.created_at == timedelta(seconds=20)
    assert reg.get(choice.choice_id).status == "open"
    assert reg.current(CALL_ID).choice_id == choice.choice_id
    assert reg.decision_of(choice.choice_id) == "dec-7"


def test_ac50_unanswered_choice_expires_after_twenty_seconds(fake_clock) -> None:
    reg = _registry(fake_clock)
    choice = _create(reg)
    fake_clock.advance_sync(19_999)
    assert reg.expire_due() == []
    assert reg.get(choice.choice_id).status == "open"
    fake_clock.advance_sync(2)
    expired = reg.expire_due()
    assert [c.choice_id for c in expired] == [choice.choice_id]
    assert all(c.status == "expired" for c in expired)
    assert reg.get(choice.choice_id).status == "expired"
    assert reg.current(CALL_ID) is None
    assert reg.decision_of(choice.choice_id) == "dec-7", "the safe default reuses the creating decision_id"


def test_ac50_answer_after_expiry_reports_expired(fake_clock) -> None:
    reg = _registry(fake_clock)
    choice = _create(reg)
    fake_clock.advance_sync(20_001)
    reg.expire_due()
    late = reg.answer(choice.choice_id, "nov3")
    assert late.status == "expired"
    assert reg.get(choice.choice_id).status == "expired"


def test_ac50_answer_in_time_is_recorded_and_never_expires(fake_clock) -> None:
    reg = _registry(fake_clock)
    choice = _create(reg)
    fake_clock.advance_sync(10_000)
    answered = reg.answer(choice.choice_id, "nov3")
    assert answered.status == "answered"
    fake_clock.advance_sync(15_000)
    assert reg.expire_due() == []
    assert reg.get(choice.choice_id).status == "answered"


def test_ac50_unknown_choice_and_unknown_option(fake_clock) -> None:
    reg = _registry(fake_clock)
    choice = _create(reg)
    with pytest.raises(ToolFailure) as unknown:
        reg.answer("ch-does-not-exist", "nov1")
    assert unknown.value.code == "unknown_choice"
    with pytest.raises(ToolFailure) as invalid:
        reg.answer(choice.choice_id, "dec5")
    assert invalid.value.code == "invalid_option"
    assert reg.get(choice.choice_id).status == "open"


def test_ac50_policy_timeout_and_safe_default(skills_dir: Path, test_profile: dict) -> None:
    raw = merged_policy_raw(require_skills_dir(skills_dir))
    assert raw["global"]["ask_user"]["on_timeout"] == "safe_default"
    assert raw["decision_points"]["call_round"]["safe_defaults"]["ask_user_timeout"] == "decline_and_follow_up"
    assert loaded_policy(skills_dir).global_.ask_user["timeout_sec"] == 20
    compiled = compile_call(task_card(), load_skills(skills_dir), profile(test_profile), standing_rules(), [])
    assert compiled.say_templates["pending.safe_default"].strip()
