"""AC-56（G 侧）：对方提议做调查时按常设规则婉拒（decline_offer），不打扰用户（G 线；tech §6.6 第 5 步）。

第 3 步可见测试。「初始化后 standing_rules 有 3 条」是 A 线 store 的初始数据，A 线测；这里把同样的 3 条
规则放进 DecisionContext。
"""

from pathlib import Path

from fakes import round_answers
from g_support import SURVEY_TEXT, build_kit, profile, speech_turn


async def test_ac56_survey_is_declined_without_asking(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("open_answer", menu_confidence=0.9, call_phase="live_rep", needs_user=0.05, stakes=0))
    result = await kit.round(speech_turn(SURVEY_TEXT))
    assert result.intent == "decline_offer"
    assert result.action == "say" and result.say_text
    assert len(kit.trace.decisions()) == 1


async def test_ac56_survey_decline_wins_over_a_low_stakes_ask_pick(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("ask_user", menu_confidence=0.6, call_phase="live_rep", needs_user=0.2, stakes=0))
    result = await kit.round(speech_turn(SURVEY_TEXT))
    assert result.intent == "decline_offer"
    assert result.action != "ask_user"
