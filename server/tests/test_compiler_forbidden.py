"""AC-42 与 AC-107（G 侧白名单函数）：诱饵 SSN、出生日期、卡号进不了 Sonic 提示和 Jev state。

G 线；tech §6.6、§6.9、§7.1 AC-42/AC-107；第 3 步可见测试。AC-107 的 text-sim 全流程测试由 E 线写，
这里只测它依赖的同一份白名单：select_allowed_fields()、compile_call()、build_agent_prompt()、build_state()。
"""

import json
from pathlib import Path


from fakes import round_answers
from g_support import (
    ACCOUNT_NUMBER,
    COMPANY_SKILL,
    COMPANY_ID,
    GOAL,
    NEUTRAL_TEXT,
    build_kit,
    decoy_profile,
    line,
    profile,
    require_skills_dir,
    speech_turn,
    standing_rules,
    task_card,
)
from waitless.interfaces import ForbiddenField
from waitless.models import FORBIDDEN_FIELD_KEYS, Profile
from waitless.decision.jev import build_state
from waitless.skills.compiler import build_agent_prompt, compile_call, select_allowed_fields
from waitless.skills.loader import load_skills


def test_ac42_decoys_never_reach_the_compiled_call(skills_dir: Path, test_profile: dict, decoy_sensitive: dict) -> None:
    prof = decoy_profile(test_profile, decoy_sensitive)
    compiled = compile_call(task_card(), load_skills(require_skills_dir(skills_dir)), prof, standing_rules(), [])
    blob = compiled.model_dump_json()
    for value in decoy_sensitive.values():
        assert value not in compiled.sonic_prompt
        assert value not in blob
    assert not set(compiled.allowed_fields) & set(FORBIDDEN_FIELD_KEYS)
    # 可代说字段照常保留（含本公司的账户号，歧义表 A-04）
    assert compiled.allowed_fields.get("name") == test_profile["name"]
    assert compiled.allowed_fields.get("address") == test_profile["address"]
    assert compiled.allowed_fields.get("account_number") == ACCOUNT_NUMBER


async def test_ac42_decoys_never_reach_the_jev_state(
    skills_dir: Path, test_profile: dict, decoy_sensitive: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=decoy_profile(test_profile, decoy_sensitive))
    fake_jev.push(round_answers("open_answer", call_phase="live_rep"))
    await kit.round(speech_turn(NEUTRAL_TEXT))
    assert len(fake_jev.calls) == 1
    state = fake_jev.calls[0].state
    assert not set(state.user_profile) & set(FORBIDDEN_FIELD_KEYS)
    state_json = state.model_dump_json()
    recorded = json.dumps([ev.jev_request for ev in kit.trace.decisions()])
    for value in decoy_sensitive.values():
        assert value not in state_json
        assert value not in recorded


def test_ac107_select_allowed_fields_drops_forbidden_keys_even_if_whitelisted(
    test_profile: dict, decoy_sensitive: dict
) -> None:
    prof = decoy_profile(test_profile, decoy_sensitive)
    got = select_allowed_fields(prof, ["name", "address", *FORBIDDEN_FIELD_KEYS], company_id=COMPANY_ID)
    assert got == {"name": test_profile["name"], "address": test_profile["address"]}


def test_ac107_select_allowed_fields_skips_empty_values(test_profile: dict) -> None:
    prof = profile(test_profile, phone_number=None, address="")
    got = select_allowed_fields(prof, ["name", "phone_number", "address"], company_id=COMPANY_ID)
    assert got == {"name": test_profile["name"]}


def test_ac107_select_allowed_fields_takes_this_companys_account(test_profile: dict) -> None:
    """歧义定稿 A-04（map_by_company）：白名单含 account_number 时只取本公司的账户号。"""
    prof = Profile(**test_profile, accounts={COMPANY_ID: ACCOUNT_NUMBER, "harbor-water": "77001234"})
    got = select_allowed_fields(prof, ["name", "account_number"], company_id=COMPANY_ID)
    assert got == {"name": test_profile["name"], "account_number": ACCOUNT_NUMBER}
    assert "77001234" not in got.values()


def test_ac107_build_state_keeps_forbidden_values_out(decoy_sensitive: dict) -> None:
    allowed = {"name": "Alex Example", **decoy_sensitive}
    try:
        state = build_state(
            task_goal=GOAL,
            allowed_fields=allowed,
            standing_rules=standing_rules(),
            prior_choices=[],
            transcript=[line("company", NEUTRAL_TEXT)],
        )
    except (ValueError, ForbiddenField):
        return  # 拒绝也算守住
    assert state.user_profile == {"name": "Alex Example"}
    dumped = state.model_dump_json()
    for value in decoy_sensitive.values():
        assert value not in dumped


def test_ac107_build_agent_prompt_uses_only_allowed_fields(
    skills_dir: Path, test_profile: dict, decoy_sensitive: dict
) -> None:
    skills = load_skills(require_skills_dir(skills_dir))
    allowed = {"name": test_profile["name"], "address": test_profile["address"]}
    prompt = build_agent_prompt(
        task=task_card(),
        allowed_fields=allowed,
        rules=standing_rules(),
        company=skills[COMPANY_SKILL],
        skills=skills,
    )
    assert test_profile["address"] in prompt, "authorized facts belong in the task data"
    for value in decoy_sensitive.values():
        assert value not in prompt


def test_ac107_build_agent_prompt_never_prints_forbidden_values(
    skills_dir: Path, test_profile: dict, decoy_sensitive: dict
) -> None:
    skills = load_skills(require_skills_dir(skills_dir))
    try:
        prompt = build_agent_prompt(
            task=task_card(),
            allowed_fields={"name": test_profile["name"], **decoy_sensitive},
            rules=standing_rules(),
            company=skills[COMPANY_SKILL],
            skills=skills,
        )
    except (ValueError, ForbiddenField):
        return
    for value in decoy_sensitive.values():
        assert value not in prompt
