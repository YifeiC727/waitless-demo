"""I1-4: never_auto keeps a running call polling after asking Jev."""

import json

import pytest

from fakes import jev_choice
from g_support import TASK_ID, build_kit, line, profile, tool_candidates
from waitless.decision.jev import JevError
from waitless.models import USER_ACTION_TOOLS


@pytest.fixture
def kit(skills_dir, test_profile, fake_jev, fake_clock):
    return build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))


def tool_result(name, structured=None, **changes):
    result = dict(tool_use_id="call-use", name=name,
                  structured=structured if structured is not None else dict(task_id=TASK_ID),
                  is_error=False)
    result.update(changes)
    return result


async def assert_redirect(kit, last_text, choice, expected, *, confidence=0.9, earlier=()):
    kit.jev.push({"next_action": jev_choice(choice, confidence)})
    outcome = await kit.decide(
        "assistant_next_action", candidates=tool_candidates(),
        transcript=[*earlier, line("user", last_text)],
    )
    assert len(kit.jev.calls) == 1
    assert kit.jev.calls[0].response.answers["next_action"].choice == choice
    assert outcome.action == "wait"
    assert outcome.intent not in USER_ACTION_TOOLS
    actions = kit.policy._points["assistant_next_action"]["actions"]
    assert actions[outcome.intent] == ("poll" if expected == "wait_for_call" else "await_user")
    assert outcome.reason_code == "user_action_required"
    assert outcome.intent == expected
    assert outcome.policy_rule == f"assistant_next_action.never_auto.{expected}"
    event = kit.trace.decision(outcome.decision_id)
    assert event.policy_decision["intent"] == expected
    assert event.policy_decision["policy_rule"] == outcome.policy_rule


@pytest.mark.parametrize("name", USER_ACTION_TOOLS)
@pytest.mark.parametrize("choice", USER_ACTION_TOOLS)
@pytest.mark.parametrize("confidence", [0.54, 0.85])
async def test_successful_user_action_keeps_polling(kit, name, choice, confidence):
    await assert_redirect(kit, json.dumps(tool_result(name)), choice, "wait_for_call",
                          confidence=confidence)


@pytest.mark.parametrize("choice", USER_ACTION_TOOLS)
@pytest.mark.parametrize("status", [
    dict(ended=False, pending_choice=None, handoff=None),
    dict(ended=False, pending_choice=dict(status="open")),
    dict(ended=False, handoff=dict(state="ready")),
])
async def test_running_status_keeps_polling(kit, choice, status):
    await assert_redirect(kit, json.dumps(tool_result("get_call_status", status)),
                          choice, "wait_for_call", confidence=0.54)


@pytest.mark.parametrize("choice", USER_ACTION_TOOLS)
@pytest.mark.parametrize("last_text", ["Please start the call.", "call_ended"])
async def test_before_call_or_ended_still_waits_for_user(kit, choice, last_text):
    await assert_redirect(kit, last_text, choice, "present_task_card",
                          earlier=[line("user", json.dumps(tool_result("start_assisted_call")))])


@pytest.mark.parametrize("last_text", [
    "{broken JSON", "null", "[]", '"get_call_status"',
    json.dumps(tool_result("prepare_task")),
    json.dumps(tool_result("get_call_status", dict(ended=True))),
    *[json.dumps(tool_result("get_call_status", dict(ended=value)))
      for value in (None, 0, "false")],
    json.dumps(tool_result("get_call_status", {})),
    json.dumps(tool_result("get_call_status", [])),
    json.dumps(tool_result("start_assisted_call", [], is_error=False)),
    *[json.dumps(tool_result(name, is_error=True)) for name in USER_ACTION_TOOLS],
    json.dumps(tool_result("get_call_status", dict(ended=False), is_error=True)),
    json.dumps(dict(name="start_assisted_call", structured={})),
])
async def test_unusable_last_fact_falls_back_to_task_card(kit, last_text):
    await assert_redirect(kit, last_text, "answer_pending_choice", "present_task_card",
                          earlier=[line("user", json.dumps(tool_result("start_assisted_call")))])


async def test_jev_error_never_produces_a_redirect(kit):
    kit.jev.push(RuntimeError("unavailable"))
    with pytest.raises(JevError):
        await kit.decide("assistant_next_action", candidates=tool_candidates(),
                         transcript=[line("user", json.dumps(tool_result("start_assisted_call")))])
    assert kit.trace.of_type("decision") == []
