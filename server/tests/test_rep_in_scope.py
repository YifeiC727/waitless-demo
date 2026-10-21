"""AC-57：坐席询问可代说字段（地址、开通日期、账户号）时由 AI 自己回答，不转接（G 线；tech §6.6 第 7 步）。

第 3 步可见测试。另含歧义定稿 A-16（field_templates）：say-templates.yaml 有 open_answer.<field> 模板，
lexicons.yaml 有 field_cues，compile_call 带上模板、把线索展平成 lexicons["field_cues.<field>"]
（text-sim 的 release 靠它们组织回答）。
"""

from pathlib import Path

import pytest

from fakes import round_answers
from waitless.models import FIELD_CUES_PREFIX, OPEN_ANSWER_TEMPLATE_PREFIX
from g_support import (
    ACCOUNT_NUMBER,
    KEYPAD_TEXT,
    build_kit,
    find_key,
    profile,
    read_yaml,
    speech_turn,
)

IN_SCOPE = [
    "What is the service address for the new account?",
    "And what date would you like the service to start?",
    "Can you read me the account number on file?",
]


@pytest.mark.parametrize("text", IN_SCOPE)
async def test_ac57_rep_questions_in_scope_are_answered_by_the_ai(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock, text: str
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("open_answer", menu_confidence=0.9, call_phase="live_rep", needs_user=0.3, stakes=1))
    result = await kit.round(speech_turn(text), phase="live_rep")
    assert result.intent == "open_answer"
    assert result.action == "release"


async def test_ac57_keypad_request_is_handled_by_the_ai(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("press_field:account_number", menu_confidence=0.9, call_phase="live_rep", stakes=1))
    result = await kit.round(speech_turn(KEYPAD_TEXT), phase="live_rep")
    assert result.intent == "press_field:account_number" and result.action == "press"
    assert result.intent != "request_handoff"


def test_ac57_field_answer_templates_are_compiled(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    """A-16：open_answer.address 与 open_answer.start_date 两个模板随 compile_call 带上，并能用档案和任务卡的值填好。"""
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    values = {**kit.compiled.allowed_fields, "effective_date": kit.compiled.task_card.effective_date}
    address = kit.compiled.say_templates[OPEN_ANSWER_TEMPLATE_PREFIX + "address"].format(**values)
    assert test_profile["address"] in address
    start = kit.compiled.say_templates[OPEN_ANSWER_TEMPLATE_PREFIX + "start_date"].format(**values)
    assert kit.compiled.task_card.effective_date in start
    assert ACCOUNT_NUMBER not in address
    raw = find_key(read_yaml(skills_dir / "decide-option" / "references" / "lexicons.yaml"), "field_cues")
    assert raw and isinstance(raw[0], dict) and raw[0].get("address"), "lexicons.yaml needs field_cues.address"
    assert kit.compiled.lexicons[FIELD_CUES_PREFIX + "address"] == list(raw[0]["address"]), "flattened as field_cues.<field>"
