"""U100: bank skill, official sources, and company-specific prompt assembly."""

import re

from g_support import frontmatter, profile, read_yaml, standing_rules, task_card
from waitless.company.config import load_company
from waitless.models import IvrMap
from waitless.official_number import FixtureFetcher, find_official_number
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills

BANK = "bluefin-harbor-bank"
SKILL = "company-bluefin-harbor-bank"


def test_u100_bank_skill_loads(skills_dir):
    skill = load_skills(skills_dir)[SKILL]
    assert skill.metadata["waitless-kind"] == "company"
    assert skill.metadata["company-id"] == BANK
    assert "live-lookup-service" not in skill.metadata
    assert "electric" not in skill.description.lower()


def test_u100_bank_ivr_map(skills_dir):
    ivr = IvrMap.model_validate(read_yaml(skills_dir / SKILL / "references/ivr-map.yaml"))
    assert len(ivr.intents) == 1
    intent, = ivr.intents
    assert intent.id == "duplicate_charge"
    assert intent.required_fields == ["name", "phone_number"]
    assert intent.menu_path == ["2", "1"]
    assert ivr.confirmation_pattern == r"^BH-\d{5}$"
    assert re.fullmatch(ivr.confirmation_pattern, "BH-12345")
    assert not re.fullmatch(ivr.confirmation_pattern, "LE-12345")
    assert ivr.keypad_fields == []


def test_u100_bank_official_number_verified(company_dir, skills_dir):
    trusted = read_yaml(skills_dir / "find-official-number/references/trusted-sources.yaml")
    fetcher = FixtureFetcher.from_company_dir(company_dir)
    for cid, intent in ((BANK, "duplicate_charge"), ("lakeshore-energy", "start_service")):
        result = find_official_number(cid, intent, fetcher, trusted_sources=trusted)
        assert result.verified
        assert result.official_number == load_company(company_dir, cid).line_number


def test_u100_bank_prompt_uses_bank_notes(skills_dir, company_dir, test_profile):
    bank = load_company(company_dir, BANK)
    ivr = IvrMap.model_validate(read_yaml(skills_dir / SKILL / "references/ivr-map.yaml"))
    intent, = ivr.intents
    assert intent.id == "duplicate_charge"
    card = task_card(company_id=BANK, precall=False, goal=intent.goal.format(
        effective_date="2026-10-08", intent_text="two charges from Quillmark Grocers"))
    card = card.model_copy(update={"company": card.company.model_copy(update={
        "name": bank.name, "official_number": bank.line_number,
    })})
    compiled = compile_call(card, load_skills(skills_dir), profile(test_profile), standing_rules(), [])
    _, phone_body = frontmatter(skills_dir / "phone-agent/SKILL.md")
    bank_notes = (skills_dir / SKILL / "references/agent-notes.md").read_text(encoding="utf-8")
    energy_notes = (skills_dir / "company-lakeshore-energy/references/agent-notes.md").read_text(encoding="utf-8")
    assert phone_body.splitlines()[0] in compiled.sonic_prompt
    assert bank_notes.splitlines()[0] in compiled.sonic_prompt
    assert energy_notes.splitlines()[0] not in compiled.sonic_prompt


def test_u100_bank_notes_are_plain_description(skills_dir):
    text = (skills_dir / SKILL / "references/agent-notes.md").read_text(encoding="utf-8")
    assert all(marker not in text for marker in (r"\d", "^", "$", "{"))
    assert re.search(r"\bpress\s+[0-9]", text, re.I) is None
