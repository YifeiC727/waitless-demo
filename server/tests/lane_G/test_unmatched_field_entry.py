"""AC-53: only a field requested in this turn may be keyed."""

import pytest

from fakes import round_answers
from g_support import build_kit, profile, speech_turn


@pytest.mark.parametrize("text", [
    "Thanks for holding. How can I help you today?",
    "Please wait while I look up the service request.",
    "Enter the reference number I just gave you, followed by pound.",
])
async def test_no_field_cue_does_not_allow_account_entry(
    skills_dir, test_profile, fake_jev, fake_clock, text
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("press_field:account_number", call_phase="ai_agent", stakes=1))
    result = await kit.round(speech_turn(text), phase="ai_agent")
    assert (result.intent, result.action) == ("ask_repeat", "say")
    assert result.digits is None
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1
