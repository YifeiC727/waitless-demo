"""G-F7: response syntax is not a preference topic."""

import pytest

from g_support import build_kit, profile, topic_labels


@pytest.mark.parametrize("text,expected", [
    ("Would you like outage text alerts?", []),
    ("What is the best time for a callback?", ["callback_time"]),
    ("Can you enter a PIN for the account?", []),
])
def test_response_syntax_does_not_become_topic(
    skills_dir, test_profile, fake_jev, fake_clock, text, expected
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    assert topic_labels(kit.compiled, text) == expected
    assert not {"question_openers", "keypad_entry"} & kit.compiled.lexicons.keys()
