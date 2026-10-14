"""G-F3: baseline service date wording names an authorized fact."""

import pytest

from fakes import round_answers
from g_support import build_kit, profile, speech_turn, task_card


@pytest.mark.parametrize("text", [
    "On what date should service start?",
    "When would you like service to begin?",
])
@pytest.mark.parametrize("pick,needs,stakes", [
    ("open_answer", 0.1, 3),
    ("request_handoff", 0.8, 0),
    ("ask_user", 0.8, 3),
])
@pytest.mark.parametrize("interrupts", [0, 1])
async def test_service_date_fact_is_released_despite_jev_uncertainty(
    skills_dir, test_profile, fake_jev, fake_clock, text, pick, needs, stakes, interrupts
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers(pick, needs_user=needs, stakes=stakes, call_phase="live_rep"))
    result = await kit.round(speech_turn(text), interrupts_used=interrupts)
    assert (result.intent, result.action) == ("open_answer", "release")
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1


@pytest.mark.parametrize("text", [
    "Can we change your service start to the next available date?",
    "When would you like service to begin? There is a fee.",
])
@pytest.mark.parametrize("interrupts,expected", [(0, "ask_user"), (1, "end_needs_follow_up")])
async def test_changed_service_date_still_requires_explicit_choice(
    skills_dir, test_profile, fake_jev, fake_clock, text, interrupts, expected
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile),
                    card=task_card(precall=False))
    fake_jev.push(round_answers("open_answer", call_phase="live_rep", needs_user=0.1, stakes=0))
    result = await kit.round(speech_turn(text), interrupts_used=interrupts)
    assert result.intent == expected
    assert result.action == ("ask_user" if interrupts == 0 else "end")
