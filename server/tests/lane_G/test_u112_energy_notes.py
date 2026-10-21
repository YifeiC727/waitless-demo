"""U112: energy consent wording and the current-menu keypad fallback."""

import subprocess

import yaml


COMPANY_REFS = "company-lakeshore-energy/references/"
CATCH_ALL = (
    "After the complete menu has been heard, if no specific offered option fits "
    "the goal, an offered catch-all such as everything else or other services "
    "may fit. Apply this only to the current unanswered menu."
)


def _baseline(skills_dir, relative_path):
    return subprocess.check_output(
        [
            "git", "-C", str(skills_dir.parent), "show",
            f"c385749:skills/{relative_path}",
        ]
    )


def test_u112_g_1_notes_decline_sms_every_time(skills_dir):
    notes = (skills_dir / (COMPANY_REFS + "agent-notes.md")).read_text(
        encoding="utf-8"
    )
    assert "cannot receive text messages" in notes
    assert "decline each time it asks" in notes
    assert "accept only when" not in notes


def test_u112_g_2_notes_deposit_is_holder_decision(skills_dir):
    notes = (skills_dir / (COMPANY_REFS + "agent-notes.md")).read_text(
        encoding="utf-8"
    )
    assert "- Deposit." not in notes
    assert "autopay" not in notes.lower()
    assert "after a transfer" not in notes
    assert "authorized facts, and only when asked" in notes


def test_u112_g_3_deposit_question(skills_dir):
    relative_path = COMPANY_REFS + "pre-call-questions.yaml"
    text = (skills_dir / relative_path).read_text(encoding="utf-8")
    questions = yaml.safe_load(text)["questions"]
    deposit = next(q for q in questions if q["question_id"] == "deposit_consent")
    assert [o["id"] for o in deposit["options"]] == ["approve", "ask_me"]
    assert deposit["default"] is None
    assert deposit["tags"] == ["human_decision", "deposit"]
    assert "will confirm it on the call" in deposit["question"]
    assert "deposit_or_autopay" not in text
    assert "autopay" not in text
    # The only change to the old start-date definition is mode metadata.
    assert "human_decision" in questions[1]["tags"]
    original_questions = [{**q, "tags": [t for t in q["tags"] if t != "human_decision"]} for q in questions]
    assert original_questions[1:] == yaml.safe_load(_baseline(skills_dir, relative_path))["questions"][1:]


def test_u112_g_4_keypad_catch_all_sentence(skills_dir):
    relative_path = "phone-agent/references/keypad.yaml"
    current = yaml.safe_load((skills_dir / relative_path).read_text(encoding="utf-8"))
    baseline = yaml.safe_load(_baseline(skills_dir, relative_path))
    instructions = current.pop("instructions")
    original_instructions = baseline.pop("instructions")
    lookup_guidance = (
        "An option to look up, check or manage an existing account fits only "
        "when the goal is about an existing account; when the goal is to start "
        "new service, keep listening for an option to set up a new account or "
        "start service. Being able to key in a phone or account number is not "
        "a reason to choose a lookup option."
    )
    assert lookup_guidance in instructions
    assert instructions.endswith(CATCH_ALL)
    assert instructions == original_instructions + " " + lookup_guidance + " " + CATCH_ALL
    assert current == baseline


def test_u112_g_5_observed_notes_untouched(skills_dir):
    relative_path = COMPANY_REFS + "observed-ivr.md"
    assert (skills_dir / relative_path).read_bytes() == _baseline(skills_dir, relative_path)
