"""Sonic needs the authorized task date even if the goal does not repeat it."""

import pytest

from g_support import decoy_profile, task_card
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills


@pytest.mark.parametrize("date", ["2027-02-19", "2027-04-06"])
def test_effective_date_reaches_prompt_without_expanding_profile_whitelist(
    skills_dir, test_profile, decoy_sensitive, date
):
    card = task_card(goal="Start electric service", precall=False)
    card.effective_date = date
    compiled = compile_call(card, load_skills(skills_dir),
                            decoy_profile(test_profile, decoy_sensitive), [], [])
    assert date in compiled.sonic_prompt
    assert "effective_date" not in compiled.allowed_fields
    assert "start_date" not in compiled.allowed_fields
    for value in decoy_sensitive.values():
        assert value not in compiled.sonic_prompt


def test_absent_effective_date_is_not_invented(skills_dir, test_profile, decoy_sensitive):
    card = task_card(goal="Start electric service", precall=False)
    card.effective_date = None
    compiled = compile_call(card, load_skills(skills_dir),
                            decoy_profile(test_profile, decoy_sensitive), [], [])
    assert "None" not in compiled.sonic_prompt
    assert "2026-11-01" not in compiled.sonic_prompt
