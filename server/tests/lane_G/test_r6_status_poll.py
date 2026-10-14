"""I1-2: a repeated status lookup returns to polling after asking Jev."""

import json

import pytest

from fakes import jev_choice
from g_support import TASK_ID, build_kit, line, profile, tool_candidates
from waitless.decision.jev import JevError
from waitless.decision.policy import Policy
from waitless.models import USER_ACTION_TOOLS


@pytest.fixture
def kit(skills_dir, test_profile, fake_jev, fake_clock):
    return build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))


def status_result(**changes):
    structured = dict(task_id=TASK_ID, ended=False, pending_choice=None, handoff=None)
    structured.update(changes)
    return dict(tool_use_id="status-use", name="get_call_status", structured=structured, is_error=False)


async def decide(kit, result, *, choice="get_call_status", confidence=0.9, suffix=()):
    kit.jev.push({"next_action": jev_choice(choice, confidence)})
    transcript = [line("user", "Check the call.")]
    if result is not None:
        transcript.append(line("user", json.dumps(result)))
    transcript.extend(suffix)
    outcome = await kit.decide("assistant_next_action", candidates=tool_candidates(), transcript=transcript)
    assert len(kit.jev.calls) == 1
    assert kit.jev.calls[0].response.answers["next_action"].choice == choice
    event = kit.trace.decision(outcome.decision_id)
    assert event.policy_decision["intent"] == outcome.intent
    assert event.policy_decision["policy_rule"] == outcome.policy_rule
    assert outcome.action == "wait"  # DP-A1's public contract; A maps the intent to poll.
    return outcome


@pytest.mark.parametrize("confidence", [0.84, 0.85, 0.99])
@pytest.mark.parametrize("changes", [
    {}, {"pending_choice": {"status": "answered"}}, {"pending_choice": {"status": "expired"}},
    {"handoff": {"state": "active"}}, {"handoff": {"state": "ended"}},
])
async def test_repeated_running_status_returns_to_poll(kit, confidence, changes):
    outcome = await decide(kit, status_result(**changes), confidence=confidence)
    assert outcome.intent == "wait_for_call"
    assert outcome.policy_rule == "assistant_next_action.poll_while_running"
    assert outcome.reason_code == ("auto_high_conf" if confidence >= 0.85 else "low_stakes_top")
    assert kit.policy._points["assistant_next_action"]["actions"][outcome.intent] == "poll"


@pytest.mark.parametrize("changes", [
    {"pending_choice": {"status": "open"}}, {"handoff": {"state": "ready"}}, {"ended": True},
    {"task_id": "another-task"}, {"task_id": None}, {"ended": None}, {"ended": 0}, {"ended": "false"},
    {"pending_choice": []}, {"pending_choice": {}}, {"pending_choice": {"status": "unknown"}},
    {"handoff": []}, {"handoff": {}}, {"handoff": {"state": "unknown"}},
])
async def test_actionable_ended_or_unusable_status_preserves_jev(kit, changes):
    outcome = await decide(kit, status_result(**changes))
    assert outcome.intent == "get_call_status"
    assert outcome.policy_rule == "assistant_next_action"


@pytest.mark.parametrize("result", [
    None, [], "not JSON", {"name": "get_call_status", "structured": []},
    {"name": "get_call_status", "structured": {}},
    {**status_result(), "name": "start_assisted_call"},
    {**status_result(), "is_error": True},
])
async def test_first_lookup_other_tool_or_bad_result_preserves_jev(kit, result):
    outcome = await decide(kit, result)
    assert outcome.intent == "get_call_status"
    assert outcome.policy_rule == "assistant_next_action"


@pytest.mark.parametrize("last_text", ["call_ended", "Check again", "{broken JSON"])
async def test_only_last_transcript_line_is_checked(kit, last_text):
    outcome = await decide(kit, status_result(), suffix=[line("user", last_text)])
    assert outcome.intent == "get_call_status"


@pytest.mark.parametrize("choice", ["reply_only", "get_case_result", "wait_for_call", *USER_ACTION_TOOLS])
async def test_other_jev_choices_and_never_auto_are_preserved(kit, choice):
    outcome = await decide(kit, status_result(), choice=choice)
    assert outcome.intent == ("present_task_card" if choice in USER_ACTION_TOOLS else choice)
    assert outcome.policy_rule == "assistant_next_action"
    if choice in USER_ACTION_TOOLS:
        assert outcome.reason_code == "user_action_required"


async def test_mapping_is_enabled_by_the_skill_rule(kit):
    raw = kit.registry.policy_raw()
    rules = raw["decision_points"]["assistant_next_action"]["rules"]
    rules[:] = [rule for rule in rules if "poll_while_running" not in rule]
    kit.gate._policy = Policy.load(raw)
    outcome = await decide(kit, status_result())
    assert outcome.intent == "get_call_status"


async def test_jev_failure_never_produces_a_poll_decision(kit):
    kit.jev.push(RuntimeError("unavailable"))
    with pytest.raises(JevError):
        await kit.decide("assistant_next_action", candidates=tool_candidates(),
                         transcript=[line("user", json.dumps(status_result()))])
    assert kit.trace.of_type("decision") == []
