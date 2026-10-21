"""U70-1: Lakeshore needs name, phone and address, and can key the phone."""

import shutil

import pytest
import yaml

from a_support import AEnv, LAKESHORE_EXTRACTED, RecordingLlm, ScriptedGate
from g_support import profile, speech_turn, standing_rules, task_card
from waitless.decision.gate import round_candidates
from waitless.models import IvrMap
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills


PHONE_TEXT = "Please enter your phone number, followed by the pound key."


def _ivr_map(skills_dir):
    path = skills_dir / "company-lakeshore-energy" / "references" / "ivr-map.yaml"
    return IvrMap.model_validate(yaml.safe_load(path.read_text()))


def test_u70_1_lakeshore_intents_need_name_phone_address(skills_dir):
    ivr = _ivr_map(skills_dir)
    assert [intent.id for intent in ivr.intents] == ["start_service", "stop_service", "move_service"]
    for intent in ivr.intents:
        assert intent.required_fields == ["name", "phone_number", "address"]


def test_u70_1_keypad_fields_add_phone_number(skills_dir):
    assert _ivr_map(skills_dir).keypad_fields == ["account_number", "phone_number"]


def test_u70_1_phone_entry_is_a_round_candidate(skills_dir, test_profile):
    compiled = compile_call(task_card(), load_skills(skills_dir),
                            profile(test_profile, account=None), standing_rules(), [])
    ids = [candidate.id for candidate in round_candidates(speech_turn(PHONE_TEXT), compiled)]
    assert "press_field:phone_number" in ids
    assert "press_field:account_number" not in ids


@pytest.mark.parametrize("intent", ["start_service", "stop_service", "move_service"])
async def test_u70_1_profile_without_account_is_ready(tmp_path, skills_dir, intent):
    copied = shutil.copytree(skills_dir, tmp_path / "skills", ignore=shutil.ignore_patterns("hidden"))
    env = AEnv(tmp_path, skills=load_skills(copied))
    try:
        env.store.save_profile({"name": "Alex Example", "phone_number": "(312) 555-0147",
                                "address": "1200 W Example St, Apt 4B"})
        service = env.task_service(
            gate=ScriptedGate({"company_skill": "company-lakeshore-energy", "task_intent": intent,
                               "precall_default": "keep_default"}),
            llm=RecordingLlm(extracted=LAKESHORE_EXTRACTED),
        )
        out = await service.prepare("Manage my electricity service on November 1.")
        assert out.status == "ready"
        assert out.missing == []
        assert out.message == "Review the task card and click Start."
    finally:
        env.close()
