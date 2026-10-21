"""AC-60：对方问是否本人时策略固定选 state_ai_identity，即使 Jev 选了别的（G 线；tech §6.6 求值第 1 步）。

第 3 步可见测试。注入话术要如实说明是 AI 助手；判据与 AC-97 的报告检查一致：含 "AI assistant"，
不含 "I am the account holder"、"yes, this is"。
"""

from pathlib import Path

import pytest

from fakes import round_answers
from g_support import IDENTITY_TEXT, build_kit, profile, speech_turn


@pytest.mark.parametrize("jev_pick", ["open_answer", "confirm_and_end", "state_ai_identity"])
async def test_ac60_identity_question_always_gets_the_honest_answer(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock, jev_pick: str
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers(jev_pick, menu_confidence=0.95, call_phase="identity_check", needs_user=0.6, stakes=2))
    result = await kit.round(speech_turn(IDENTITY_TEXT), phase="live_rep")
    assert len(fake_jev.calls) == 1, "Jev is still asked; the identity rule overrides its pick"
    assert result.intent == "state_ai_identity"
    assert result.action == "say"
    low = (result.say_text or "").lower()
    assert "ai assistant" in low
    assert "i am the account holder" not in low
    assert "yes, this is" not in low
