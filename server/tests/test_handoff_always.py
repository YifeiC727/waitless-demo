"""AC-52：对方索要 handoff_always 的信息时转接用户，话术不含任何字段值（G 线；tech §6.6 求值第 2 步）。

第 3 步可见测试。Jev 选了别的也一样（硬规则先于 Jev 的选择生效）；不在 live_rep/identity_check 阶段时
先要求转人工（ask_for_representative），不转接。
"""

from pathlib import Path

from fakes import round_answers
from g_support import DOB_TEXT, assert_no_field_values, build_kit, decoy_profile, placeholders, speech_turn
from waitless.models import FORBIDDEN_FIELD_KEYS

FIELD_KEYS = {"name", "phone_number", "address", "account_number", "email", "zip", *FORBIDDEN_FIELD_KEYS}


def _kit(skills_dir, test_profile, decoys, fake_jev, fake_clock):
    return build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=decoy_profile(test_profile, decoys))


async def test_ac52_date_of_birth_request_hands_off(
    skills_dir: Path, test_profile: dict, decoy_sensitive: dict, fake_jev, fake_clock
) -> None:
    kit = _kit(skills_dir, test_profile, decoy_sensitive, fake_jev, fake_clock)
    fake_jev.push(round_answers("open_answer", menu_confidence=0.9, call_phase="live_rep", needs_user=0.2, stakes=1))
    result = await kit.round(speech_turn(DOB_TEXT), phase="live_rep")
    assert result.intent == "request_handoff"
    assert result.action == "handoff"
    assert_no_field_values(result.say_text, kit, decoy_sensitive.values())


async def test_ac52_jev_handoff_with_needs_user_hands_off(
    skills_dir: Path, test_profile: dict, decoy_sensitive: dict, fake_jev, fake_clock
) -> None:
    kit = _kit(skills_dir, test_profile, decoy_sensitive, fake_jev, fake_clock)
    fake_jev.push(round_answers("request_handoff", menu_confidence=0.9, call_phase="live_rep", needs_user=0.8, stakes=2))
    result = await kit.round(
        speech_turn("I'm sorry, only the customer can authorize this. Please put them on the line."),
        phase="live_rep",
    )
    assert result.intent == "request_handoff" and result.action == "handoff"


async def test_ac52_outside_live_rep_asks_for_a_representative(
    skills_dir: Path, test_profile: dict, decoy_sensitive: dict, fake_jev, fake_clock
) -> None:
    kit = _kit(skills_dir, test_profile, decoy_sensitive, fake_jev, fake_clock)
    fake_jev.push(round_answers("open_answer", menu_confidence=0.9, call_phase="ai_agent", needs_user=0.2, stakes=1))
    result = await kit.round(speech_turn("Please say or enter the date of birth on the account."), phase="ai_agent")
    assert result.intent != "request_handoff" and result.action != "handoff"
    assert "ask_for_representative" in {result.intent, result.reason_code}
    assert_no_field_values(result.say_text, kit, decoy_sensitive.values())


def test_ac52_handoff_templates_carry_no_field_values(
    skills_dir: Path, test_profile: dict, decoy_sensitive: dict, fake_jev, fake_clock
) -> None:
    kit = _kit(skills_dir, test_profile, decoy_sensitive, fake_jev, fake_clock)
    for key in ("handoff.announce", "handoff.ask_user"):
        template = kit.compiled.say_templates[key]
        assert not placeholders(template) & FIELD_KEYS, f"{key} must not fill in field values: {template!r}"
        assert_no_field_values(template, kit, decoy_sensitive.values())
