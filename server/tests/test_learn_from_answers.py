"""AC-58：learn_from_answers 只学 stakes ≤1 的回答，下次同类问题自动套用（G 线；tech §6.6 第 6 步、§7.1）。

第 3 步可见测试。「同类」按 PriorChoice.question_key 等于本轮命中的 lexicons 标签来认（A-03）；套用的结果
按缺口 G-G-05（定稿 use_preference_pending_answer）：intent="use_preference:<question_key>"、action=say、
say_text = say_templates["pending.answer"].format(option_label=<偏好存的选项 label>, option_detail="")。
话题避开常设规则管的无纸化、语言，用回电时段。
"""

from pathlib import Path

import pytest

from fakes import round_answers
from g_support import (
    CALLBACK_TEXT,
    FEE_TEXT,
    NEUTRAL_TEXT,
    build_kit,
    loaded_policy,
    profile,
    round_jev_answers,
    speech_turn,
    topic_labels,
)
from waitless.models import USE_PREFERENCE_PREFIX, PriorChoice

NOTICE_TEXT = "Would you prefer we send account notices by text message or by email?"


@pytest.mark.parametrize("stakes, expected", [(0, True), (1, True), (2, False), (3, False)])
def test_ac58_should_learn_only_low_stakes(skills_dir: Path, stakes: int, expected: bool) -> None:
    assert loaded_policy(skills_dir).should_learn(stakes) is expected


@pytest.mark.parametrize("stakes, learn", [(1, True), (3, False)])
async def test_ac58_ask_user_outcome_marks_learning_by_stakes(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock, stakes: int, learn: bool
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    turn = speech_turn(NOTICE_TEXT)
    request = kit.round_request(turn, interrupts_used=0)
    answers = round_jev_answers(
        [c.id for c in request.candidates], "ask_user", menu_confidence=0.6, needs_user=0.7, stakes=stakes
    )
    outcome = kit.policy.evaluate_round(request, answers, kit.compiled)
    assert outcome.intent == "ask_user"
    assert outcome.learn is learn


async def test_ac58_preferences_reach_the_jev_state(skills_dir: Path, test_profile: dict, fake_jev, fake_clock) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    prior = [PriorChoice(question_key="notice_channel", choice="email", stakes=0, source="preference")]
    fake_jev.push(round_answers("open_answer", call_phase="live_rep"))
    await kit.round(speech_turn(NEUTRAL_TEXT), prior_choices=prior)
    assert fake_jev.calls[0].state.prior_choices == prior


async def test_ac58_a_preference_never_answers_a_money_question(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    prior = [PriorChoice(question_key="start_date_fee", choice="nov1", stakes=1, source="preference")]
    fake_jev.push(round_answers("open_answer", menu_confidence=0.9, call_phase="live_rep", needs_user=0.1, stakes=3))
    result = await kit.round(speech_turn(FEE_TEXT), prior_choices=prior, interrupts_used=0)
    assert result.intent == "ask_user" and result.action == "ask_user"


async def test_ac58_low_stakes_preference_is_reused_without_asking(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    labels = topic_labels(kit.compiled, CALLBACK_TEXT)
    assert labels, "lexicons.yaml needs a label for callback-time questions (G-G-05)"
    label_text = "Weekday evenings after 6 pm"
    prior = [PriorChoice(question_key=key, choice=label_text, stakes=0, source="preference") for key in labels]
    fake_jev.push(round_answers("ask_user", menu_confidence=0.6, call_phase="live_rep", needs_user=0.2, stakes=0))
    result = await kit.round(speech_turn(CALLBACK_TEXT), prior_choices=prior)
    assert len(fake_jev.calls) == 1
    assert result.intent in {USE_PREFERENCE_PREFIX + key for key in labels}
    assert result.action == "say"
    expected = kit.compiled.say_templates["pending.answer"].format(option_label=label_text, option_detail="")
    assert result.say_text == expected
