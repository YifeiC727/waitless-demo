"""U110: public utility facts must not become invented choices or keypad routes."""

import yaml


def test_energy_notes_do_not_invent_deposit_autopay_tradeoff(skills_dir):
    notes = (
        skills_dir / "company-lakeshore-energy/references/agent-notes.md"
    ).read_text(encoding="utf-8").lower()

    assert "do not assume an option number" in notes
    assert "autopay" not in notes
    assert "- deposit." not in notes
    assert "- start date." not in notes
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
        if question["question_id"] == "deposit_consent"
    )

    assert deposit_choice["default"] is None


def test_observed_ivr_preserves_route_specific_keypad_evidence(skills_dir):
    company_dir = skills_dir / "company-lakeshore-energy"
    observed = (company_dir / "references/observed-ivr.md").read_text(
        encoding="utf-8"
    ).lower()
    body = (company_dir / "SKILL.md").read_text(encoding="utf-8").lower()

    assert "everything else: press 3" in observed
    assert "for a new residential start-service request on this exact prompt, press 4" in observed
    assert "`my home` or `my business`" in observed
    assert "do not infer any later key until its prompt is heard" in observed
    assert "account number: 1; phone number: 2" in observed
    assert "phone number: 1; account number: 2" in observed
    assert "pressed star (`*`)" in observed
    assert "not a general rule that star always" in observed
    assert "never cache a field-to-digit mapping" in observed
    assert "do not accept this handoff automatically" in observed
    assert "consent to use the web app does not by itself authorize sending a text" in observed
    assert "declining the web app is a known dead end" in observed
    assert "retry the call during staffed hours" in observed
    assert "observed-ivr.md" in body
