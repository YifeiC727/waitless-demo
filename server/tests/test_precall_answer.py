"""AC-54：坐席问押金或自动扣款时，按任务卡上已确认的值注入话术，不生成待决选项（G 线；tech §6.6 第 4 步）。

第 3 步可见测试。say_text 用 say-templates.yaml 的 answer_precall 模板，填入任务卡所选选项的 label。
"""

from pathlib import Path

from fakes import round_answers
from g_support import AUTOPAY_LABEL, DEPOSIT_LABEL, DEPOSIT_TEXT, build_kit, profile, speech_turn


async def test_ac54_deposit_question_is_answered_from_the_task_card(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("ask_user", menu_confidence=0.95, call_phase="live_rep", needs_user=0.6, stakes=3))
    result = await kit.round(speech_turn(DEPOSIT_TEXT))
    assert result.intent == "answer_precall:deposit_or_autopay"
    assert result.action == "say"
    assert DEPOSIT_LABEL in result.say_text
    assert AUTOPAY_LABEL not in result.say_text
    assert result.pending is None


async def test_ac54_open_answer_pick_is_overridden_by_the_card(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("open_answer", menu_confidence=0.95, call_phase="live_rep", needs_user=0.1, stakes=3))
    result = await kit.round(speech_turn(DEPOSIT_TEXT))
    assert result.intent == "answer_precall:deposit_or_autopay"
    assert result.action == "say" and DEPOSIT_LABEL in result.say_text
