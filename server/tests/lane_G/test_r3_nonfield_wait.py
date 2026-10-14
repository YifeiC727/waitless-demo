"""G-F2: field change cues must not turn ordinary waits into interruptions."""

import pytest

from fakes import round_answers
from g_support import build_kit, profile, speech_turn


@pytest.mark.parametrize("text", [
    "Please hold for the next available representative.",
    "All agents are busy; the next available agent will be with you shortly.",
    "One moment while I update the system.",
    "That option is unavailable at the moment, please hold.",
])
@pytest.mark.parametrize("interrupts", [0, 1])
async def test_nonfield_wait_does_not_consume_interrupt_budget(
    skills_dir, test_profile, fake_jev, fake_clock, text, interrupts
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("wait", call_phase="hold", needs_user=0.1, stakes=0))
    result = await kit.round(speech_turn(text), phase="hold", interrupts_used=interrupts)
    assert (result.intent, result.action) == ("wait", "wait")
    assert result.reason_code == "auto_high_conf"
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1
