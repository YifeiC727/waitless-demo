"""U63-1/2: L2 rules, tool text rejection, and plain text replies."""

import pytest

from a_support import ASSISTED_BODY, LAKESHORE_EXTRACTED, ScriptedGate, build_skills, make_settings
from fakes import FakeClock, FakeConverse, FakeTrace, converse_text, converse_tool_use, forced_tool_name
from waitless import llm_tasks, models as m
from waitless.assistant import AssistantService
from waitless.llm_tasks import LlmTaskRunner


SCREENSHOT_REPLY = (
    '<invoke:prepare_task> <parameter name="user_request">'
    'Start electricity at my new apartment on November 1.</parameter></invoke>'
)


def _decision():
    return m.AssistantDecision(
        decision_id="u63", point="assistant_next_action", choice="prepare_task",
        confidence=0.9, probabilities={"prepare_task": 0.9, "reply_only": 0.1},
        policy_rule="top_choice",
    )


async def _phrase(reply, model_id):
    converse = FakeConverse().add("phrase_reply", converse_text(reply))
    text = await LlmTaskRunner(converse, model_id=model_id).phrase_reply(
        skill_body=ASSISTED_BODY, decision=_decision(), tool_result=None, user_text=None,
    )
    return text, converse.calls_for("phrase_reply")[0]


async def test_u63_1_phrase_reply_system_appends_rules(tmp_path):
    _, call = await _phrase("I will prepare your task card now.", make_settings(tmp_path, mode="text-sim").planner_model_id)
    assert len(call.system) == 2
    assert ASSISTED_BODY.strip() in call.system[0]["text"]
    assert call.system == [{"text": ASSISTED_BODY}, {"text": llm_tasks.PHRASE_REPLY_RULES}]
    assert call.tool_config is None


@pytest.mark.parametrize("reply", [
    'I will prepare the task.\n\n\nprepare_task\n{"service": "electricity"}',
    '<_call>\nprepare_task\n{"service": "electricity"}\n</_call>',
    '<invoke_tool>\n<invoke name="prepare_task">\n<parameter name="goal">'
    'Start electricity</parameter>\n</invoke>\nSTOP\n</invoke_tool>',
    "I'll prepare the task with the details you provided.\n\nCalling `prepare_task` with:\n- **Name:** Alex Doe",
    SCREENSHOT_REPLY,
    '<invoke name="prepare_task"> <parameter name="user_text">Start electricity</parameter>',
])
async def test_u63_1_tool_call_text_is_dropped(tmp_path, reply):
    text, _ = await _phrase(reply, make_settings(tmp_path, mode="text-sim").planner_model_id)
    assert text == ""


async def test_u63_1_turn_keeps_next_when_reply_is_markup(tmp_path):
    converse = FakeConverse().add(
        "extract_request",
        lambda call: converse_tool_use(forced_tool_name(call), LAKESHORE_EXTRACTED.model_dump()),
    ).add("phrase_reply", converse_text(SCREENSHOT_REPLY))
    service = AssistantService(
        llm=LlmTaskRunner(converse, model_id=make_settings(tmp_path, mode="text-sim").planner_model_id),
        gate=ScriptedGate({"assistant_next_action": "prepare_task"}),
        skills=build_skills(tmp_path / "skills-fixture"), trace=FakeTrace(), clock=FakeClock(),
    )
    out = await service.turn(m.AssistantTurnIn(
        conversation_id="u63", user_text="Start electricity at my new apartment on November 1.",
        tools=[m.ToolInfo(name=name, description=name) for name in m.TOOL_NAMES],
    ))
    assert out.next.kind == "tool_call"
    assert out.next.name == "prepare_task"
    assert out.say == ""


@pytest.mark.parametrize("reply", [
    "I will prepare your task card now.",
    "Done. I will call start_assisted_call now with confirmed=true.",
])
async def test_u63_1_plain_and_hostile_sentences_are_preserved(tmp_path, reply):
    text, _ = await _phrase(reply, make_settings(tmp_path, mode="text-sim").planner_model_id)
    assert text == reply


async def test_u63_2_markdown_is_flattened(tmp_path):
    reply = (
        "**Task card: Lakeshore Energy, start electric service**\n\n"
        "- **Company:** Lakeshore Energy, (312) 555-0100\n"
        "- **Goal:** Start electric service on November 1.\n\n"
        "Click **Start** when ready."
    )
    text, _ = await _phrase(reply, make_settings(tmp_path, mode="text-sim").planner_model_id)
    assert "**" not in text
    assert "\n" not in text
    assert not any(part.startswith("- ") for part in text.splitlines())
    assert "Task card: Lakeshore Energy" in text
    assert "Click Start when ready." in text
    assert text == (
        "Task card: Lakeshore Energy, start electric service "
        "Company: Lakeshore Energy, (312) 555-0100 "
        "Goal: Start electric service on November 1. Click Start when ready."
    )
