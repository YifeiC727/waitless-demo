"""U119: live only handles automation; text keeps its policy and choices."""

import json

import pytest

from g_support import (COMPANY_SKILL, copy_skills, decoy_profile, frontmatter,
                       profile, read_yaml, standing_rules, task_card, write_yaml)
from waitless.skills.compiler import compile_call
from waitless.skills.loader import SkillError, load_skills


SPEAKERS = {"menu_or_recording", "hold", "virtual_agent", "live_human"}
OLD_TOOLS = {"update_status", "ask_user", "ask_jev", "request_handoff", "end_call"}
HUMAN_TAG = "human_decision"


def compile_for(root, test_profile, *, mode="live-sim", card=None, prof=None):
    return compile_call(card or task_card(), load_skills(root),
                        prof or profile(test_profile), standing_rules(), [], mode=mode)


def test_live_prompt_is_automated_only(skills_dir, test_profile):
    compiled = compile_for(skills_dir, test_profile)
    _, body = frontmatter(skills_dir / "phone-agent/SKILL.md")
    assert compiled.sonic_prompt.startswith(body + "\n\n")
    assert "Waitless brings the account holder on and you no longer take part" in body
    assert "You never talk with a human representative" in body
    assert "During hold music" in body
    assert "say the one fitting choice once" in body
    assert "Give a fact only when asked" in body
    for old in (*OLD_TOOLS, "## Getting help", "## Ending the call", "## Bringing", "Deposit."):
        assert old not in compiled.sonic_prompt
    assert compiled.agent_tool_descriptions == {}


def test_tools_v2_keeps_text_descriptions(skills_dir, test_profile):
    raw = read_yaml(skills_dir / "phone-agent/references/tools.yaml")
    assert raw["version"] == 2
    assert raw["live_tools"] == []
    assert set(raw["text_tools"]) == OLD_TOOLS
    text = compile_for(skills_dir, test_profile, mode="text-sim")
    replay = compile_for(skills_dir, test_profile, mode="replay")
    legacy = compile_call(task_card(), load_skills(skills_dir), profile(test_profile), standing_rules(), [])
    assert text.agent_tool_descriptions == raw["text_tools"]
    assert replay.agent_tool_descriptions == text.agent_tool_descriptions
    assert legacy.model_dump() == text.model_dump()
    assert text.policy["precall_questions"]
    assert text.say_templates["handoff.resume"]


@pytest.mark.parametrize("company,ids", [
    (COMPANY_SKILL, {"deposit_consent", "start_date"}),
    ("company-bluefin-harbor-bank", {"callback_if_busy"}),
])
@pytest.mark.parametrize("selected", [True, False])
def test_live_ignores_saved_or_pending_human_choices(skills_dir, test_profile, company, ids, selected):
    from waitless.models import PrecallDecision

    raw = read_yaml(skills_dir / company / "references/pre-call-questions.yaml")
    assert {q["question_id"] for q in raw["questions"] if HUMAN_TAG in q["tags"]} == ids
    card = task_card(company_id=company.removeprefix("company-"), precall=False)
    for question in raw["questions"]:
        card.precall_decisions.append(PrecallDecision(
            question_id=question["question_id"], question=question["question"],
            options=question["options"], default=question["default"],
            selected=question["options"][0]["id"] if selected else None, needs_user=not selected))
    original = card.model_dump()
    live = compile_for(skills_dir, test_profile, card=card)
    assert live.task_card.precall_decisions == []  # KeypadDecider builds prior_choices from this card.
    from types import SimpleNamespace
    from waitless.call.keypad import KeypadDecider
    decider = KeypadDecider(
        spec=SimpleNamespace(compiled=live, call_id="u119-test-call"),
        deps=SimpleNamespace(trace=SimpleNamespace(events=lambda **kwargs: []),
                             store=SimpleNamespace(list_standing_rules=lambda: [],
                                                   list_preferences=lambda company_id: [])),
        pipeline_task=None, media_gate=None, injector=None, config={})
    assert decider._state().prior_choices == []
    assert live.policy["precall_questions"] == []
    assert not any(i["id"].startswith("answer_precall:") for i in live.intents)
    data = json.loads(live.sonic_prompt.split("# Task data\n", 1)[1])
    assert data["precall_choices"] == []
    for question in raw["questions"]:
        assert question["question"] not in live.sonic_prompt
    assert card.model_dump() == original
    text = compile_for(skills_dir, test_profile, mode="text-sim", card=card)
    assert text.task_card == card
    assert text.policy["precall_questions"] == [
        {**q, "tags": [tag for tag in q["tags"] if tag != HUMAN_TAG]} for q in raw["questions"]]
    if selected:
        assert len(json.loads(text.sonic_prompt.split("# Task data\n", 1)[1])["precall_choices"]) == len(ids)


def test_live_keeps_unmarked_fact_questions(skills_dir, test_profile, tmp_path):
    root = copy_skills(skills_dir, tmp_path)
    path = root / COMPANY_SKILL / "references/pre-call-questions.yaml"
    raw = read_yaml(path)
    question = dict(raw["questions"][0], question_id="contact_method", question="Use phone contact?", tags=["contact"])
    raw["questions"].append(question)
    write_yaml(path, raw)
    card = task_card(precall=False)
    from waitless.models import PrecallDecision
    card.precall_decisions.append(PrecallDecision(
        **{k: question[k] for k in ("question_id", "question", "options", "default")},
        selected="approve", needs_user=False))
    compiled = compile_for(root, test_profile, card=card)
    assert [q["question_id"] for q in compiled.policy["precall_questions"]] == ["contact_method"]
    assert compiled.task_card == card
    assert "Use phone contact?" in compiled.sonic_prompt
    assert any(i["id"] == "answer_precall:contact_method" for i in compiled.intents)


def test_speaker_is_jev_only(skills_dir, test_profile):
    raw = read_yaml(skills_dir / "phone-agent/references/speaker.yaml")
    assert set(raw) == {"instructions", "min_confidence", "options", "automated_max_human_p", "probability_sum_tolerance"}
    assert set(raw["options"]) == SPEAKERS
    assert 0 <= raw["min_confidence"] <= 1
    instructions = raw["instructions"].lower()
    for word in ("context", "thanks for holding", "account holder", "not sufficient"):
        assert word in instructions
    compiled = compile_for(skills_dir, test_profile)
    assert raw["instructions"] not in compiled.sonic_prompt
    assert "min_confidence" not in compiled.sonic_prompt
    assert not SPEAKERS.intersection(compiled.jev_templates)


@pytest.mark.parametrize("change", [
    "file_missing", "instructions_missing", "instructions_blank", "instructions_number",
    "min_confidence_missing", "min_confidence_negative", "min_confidence_high",
    "min_confidence_string", "min_confidence_bool", "min_confidence_nan",
    "options_missing", "option_missing", "option_extra", "option_blank", "options_list",
])
def test_bad_speaker_fails_at_startup(skills_dir, tmp_path, change):
    root = copy_skills(skills_dir, tmp_path)
    path = root / "phone-agent/references/speaker.yaml"
    valid = {"instructions": "Read the whole call context.", "min_confidence": 0.7,
             "automated_max_human_p": .2, "probability_sum_tolerance": .05,
             "options": {key: key for key in SPEAKERS}}
    if change == "file_missing":
        path.unlink(missing_ok=True)
    else:
        if change.endswith("_missing") and change != "option_missing":
            valid.pop(change.removesuffix("_missing"))
        elif change == "instructions_blank": valid["instructions"] = " "
        elif change == "instructions_number": valid["instructions"] = 7
        elif change.startswith("min_confidence_"):
            valid["min_confidence"] = {"negative": -0.1, "high": 1.1, "string": "0.7",
                                       "bool": True, "nan": float("nan")}[change.removeprefix("min_confidence_")]
        elif change == "option_missing": valid["options"].pop("hold")
        elif change == "option_extra": valid["options"]["unknown"] = "Unknown"
        elif change == "option_blank": valid["options"]["hold"] = " "
        elif change == "options_list": valid["options"] = list(SPEAKERS)
        write_yaml(path, valid)
    with pytest.raises(SkillError, match="speaker"):
        load_skills(root)


@pytest.mark.parametrize("threshold", [0, 1])
def test_speaker_threshold_boundaries(skills_dir, tmp_path, threshold):
    root = copy_skills(skills_dir, tmp_path)
    path = root / "phone-agent/references/speaker.yaml"
    raw = read_yaml(path)
    raw["min_confidence"] = threshold
    write_yaml(path, raw)
    assert "phone-agent" in load_skills(root)


@pytest.mark.parametrize("company", [COMPANY_SKILL, "company-bluefin-harbor-bank"])
def test_company_notes_cover_automation_only(skills_dir, company):
    notes = (skills_dir / company / "references/agent-notes.md").read_text()
    for removed in ("Getting help", "Topics the", "- Deposit.", "- Start date.", "- A callback.",
                    "Save interruptions", "specialist waits quietly", "after a transfer"):
        assert removed not in notes
    assert "authorized facts" in notes
    if company == COMPANY_SKILL:
        for required in ('"My home"', '"My business"', "cannot receive text messages", "decline each time it asks"):
            assert required in notes
    else:
        for required in ("merchant", "date", "amount", "not a finished task", "follow-up"):
            assert required in notes


def test_live_still_filters_forbidden_facts(skills_dir, test_profile, decoy_sensitive):
    compiled = compile_for(skills_dir, test_profile, prof=decoy_profile(test_profile, decoy_sensitive))
    for value in decoy_sensitive.values():
        assert value not in compiled.sonic_prompt
        assert value not in compiled.allowed_fields.values()


def test_live_ignores_retired_card_choices_and_direct_prompt_calls(skills_dir, test_profile):
    from waitless.skills.compiler import build_agent_prompt

    card = task_card()  # The historical deposit_or_autopay ID is no longer declared.
    skills = load_skills(skills_dir)
    compiled = compile_for(skills_dir, test_profile, card=card)
    assert compiled.task_card.precall_decisions == []
    prompt = build_agent_prompt(task=card, skills=skills, company=skills[COMPANY_SKILL],
                                allowed_fields=compiled.allowed_fields, rules=standing_rules(), mode="live-sim")
    assert prompt == compiled.sonic_prompt
    assert "Pay a deposit or enroll in autopay?" not in prompt
    assert card.precall_decisions


@pytest.mark.parametrize("change", ["file_missing", "version", "live_missing", "live_nonempty",
                                   "live_null", "text_missing", "text_empty", "text_blank", "extra"])
def test_bad_tools_v2_fails_at_startup(skills_dir, tmp_path, change):
    root = copy_skills(skills_dir, tmp_path)
    path = root / "phone-agent/references/tools.yaml"
    raw = {"version": 2, "live_tools": [], "text_tools": {"ask_user": "Ask the holder."}}
    if change == "file_missing":
        path.unlink()
    else:
        if change == "version": raw["version"] = 1
        elif change == "live_missing": raw.pop("live_tools")
        elif change == "live_nonempty": raw["live_tools"] = ["ask_user"]
        elif change == "live_null": raw["live_tools"] = None
        elif change == "text_missing": raw.pop("text_tools")
        elif change == "text_empty": raw["text_tools"] = {}
        elif change == "text_blank": raw["text_tools"]["ask_user"] = " "
        elif change == "extra": raw["other"] = "ignored"
        write_yaml(path, raw)
    with pytest.raises(SkillError, match="tools"):
        load_skills(root)


def test_invalid_mode_is_explicitly_rejected(skills_dir, test_profile):
    with pytest.raises(SkillError, match="mode"):
        compile_for(skills_dir, test_profile, mode="unknown")
