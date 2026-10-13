"""AC-53：要求键入账户号时按键输入档案值，数字不等于档案值就改为 ask_repeat（G 线；tech §6.6 第 7 步）。

第 3 步可见测试。press_field 的数字只来自 CompiledCall.allowed_fields（歧义定稿 A-04：本公司的账户号），
不来自对方的话，也不来自大模型。
"""

from pathlib import Path

from fakes import round_answers
from g_support import ACCOUNT_NUMBER, KEYPAD_TEXT, build_kit, profile, round_pick, speech_turn


async def test_ac53_account_number_is_keyed_from_the_profile(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(
        round_answers("press_field:account_number", menu_confidence=0.9, call_phase="ai_agent", stakes=1)
    )
    result = await kit.round(speech_turn(KEYPAD_TEXT), phase="ai_agent")
    assert result.intent == "press_field:account_number"
    assert result.action == "press"
    assert result.digits == ACCOUNT_NUMBER + "#", "the caller asked for the pound key after the number"
    assert not (result.say_text and ACCOUNT_NUMBER in result.say_text), "keyed, not spoken"


async def test_ac53_digits_never_come_from_the_call(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(
        round_answers("press_field:account_number", menu_confidence=0.9, call_phase="ai_agent", stakes=1)
    )
    result = await kit.round(
        speech_turn("Please enter account number 99887766, followed by the pound key."), phase="ai_agent"
    )
    assert "99887766" not in (result.digits or "")
    if result.action == "press":
        assert (result.digits or "").rstrip("#") == ACCOUNT_NUMBER
    else:
        assert result.intent == "ask_repeat"


async def test_ac53_value_that_cannot_be_keyed_asks_to_repeat(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile, account="LE-ACCT-2048"))
    entry = round_pick(["press_field:account_number"], call_phase="ai_agent", stakes=1)
    fake_jev.push(entry)
    result = await kit.round(speech_turn(KEYPAD_TEXT), phase="ai_agent")
    assert result.action != "press"
    if entry.chosen["menu_choice"] == "press_field:account_number":
        assert result.intent == "ask_repeat"
