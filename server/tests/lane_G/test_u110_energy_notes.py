"""U110: public utility facts must not become invented choices or keypad routes."""

import yaml


def test_energy_notes_do_not_invent_deposit_autopay_tradeoff(skills_dir):
    notes = (
        skills_dir / "company-lakeshore-energy/references/agent-notes.md"
    ).read_text(encoding="utf-8").lower()

    assert "do not assume an option number" in notes
    assert "do not present autopay as a way to replace or waive a deposit" in notes
    assert "there is no default" in notes
    assert "default is the deposit" not in notes


def test_energy_skill_does_not_claim_an_unverified_ivr_route(skills_dir):
    company_dir = skills_dir / "company-lakeshore-energy"
    ivr = yaml.safe_load(
        (company_dir / "references/ivr-map.yaml").read_text(encoding="utf-8")
    )
    body = (company_dir / "SKILL.md").read_text(encoding="utf-8").lower()

    assert all(intent["menu_path"] == [] for intent in ivr["intents"])
    assert "never permission to press keys in advance" in body
    assert "options actually heard" in body


def test_energy_skill_does_not_default_a_financial_choice(skills_dir):
    questions = yaml.safe_load(
        (skills_dir / "company-lakeshore-energy/references/pre-call-questions.yaml")
        .read_text(encoding="utf-8")
    )["questions"]
    deposit_choice = next(
        question for question in questions
        if question["question_id"] == "deposit_or_autopay"
    )

    assert deposit_choice["default"] is None
