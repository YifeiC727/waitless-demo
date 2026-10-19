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
            f"origin/feat/real-call-engine:skills/{relative_path}",
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
    deposit = next(line for line in notes.splitlines() if line.startswith("- Deposit."))
    assert "decides on this call after a transfer" in deposit
    assert "does not replace the account holder's own answer" in deposit
    assert (
        "Do not present autopay as a way to replace or waive a deposit unless "
        "the representative explicitly offers that choice on this call."
    ) in deposit
    # The sole autopay mention is the prohibition, never a promise of a waiver.
    assert notes.lower().count("autopay") == 1


def test_u112_g_3_deposit_question(skills_dir):
    relative_path = COMPANY_REFS + "pre-call-questions.yaml"
    text = (skills_dir / relative_path).read_text(encoding="utf-8")
    questions = yaml.safe_load(text)["questions"]
    deposit = next(q for q in questions if q["question_id"] == "deposit_consent")
    assert [o["id"] for o in deposit["options"]] == ["approve", "ask_me"]
    assert deposit["default"] is None
    assert deposit["tags"] == ["deposit"]
    assert "will confirm it on the call" in deposit["question"]
    assert "deposit_or_autopay" not in text
    assert "autopay" not in text
    assert questions[1:] == yaml.safe_load(_baseline(skills_dir, relative_path))["questions"][1:]


def test_u112_g_4_keypad_catch_all_sentence(skills_dir):
    relative_path = "phone-agent/references/keypad.yaml"
    current = yaml.safe_load((skills_dir / relative_path).read_text(encoding="utf-8"))
    baseline = yaml.safe_load(_baseline(skills_dir, relative_path))
    instructions = current.pop("instructions")
    original_instructions = baseline.pop("instructions")
    assert instructions.endswith(CATCH_ALL)
    assert instructions == original_instructions + " " + CATCH_ALL
    assert current == baseline


def test_u112_g_5_observed_notes_untouched(skills_dir):
    relative_path = COMPANY_REFS + "observed-ivr.md"
    assert (skills_dir / relative_path).read_bytes() == _baseline(skills_dir, relative_path)
