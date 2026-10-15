"""U63-4: chat details reach update_task and the ready task card (E87/E88)."""

import json
import shutil

from a_support import AEnv, LAKESHORE_EXTRACTED, RecordingLlm, ScriptedGate, stub_services
from fakes import FakeClock, FakeJev, FakeTrace
from g_support import PointChooser, prefer
from waitless import models as m
from waitless.decision.gate import DecisionGate
from waitless.decision.policy import Policy
from waitless.decision.registry import load_registry
from waitless.mcp_server import TOOL_DESCRIPTIONS, ToolHandlers
from waitless.skills.loader import load_skills


REQUEST = "Start electricity at my new apartment on November 1."
DETAILS = "Alex Doe, (312) 555-0199, 1200 W Example St Apt 4B Chicago IL, account LE-000123"
FIELDS = {
    "name": "Alex Doe",
    "phone_number": "(312) 555-0199",
    "address": "1200 W Example St Apt 4B Chicago IL",
    "account_number": "000123",
}
TOOLS = [m.ToolInfo(name=name, description=description) for name, description in TOOL_DESCRIPTIONS.items()]


def _details_extracted():
    return m.ExtractedRequest(name="Alex Doe", phone_number="(312) 555-0199",
                              service_address="1200 W Example St Apt 4B Chicago IL", account_number="LE-000123")


async def test_u63_4_dp_a1_transcript_keeps_history(tmp_path):
    env = AEnv(tmp_path)
    gate = ScriptedGate({"assistant_next_action": ["prepare_task", "present_task_card", "update_task"]})
    service = env.assistant(gate=gate)
    prepared = m.ToolResultIn(tool_use_id="prepare-u63", name="prepare_task", is_error=False,
                              structured={"task_id": "task-u63", "status": "setup_required",
                                          "missing": [{"key": key, "label": key} for key in FIELDS]})
    try:
        await service.turn(m.AssistantTurnIn(conversation_id="history", user_text=REQUEST, tools=TOOLS))
        await service.turn(m.AssistantTurnIn(conversation_id="history", tool_result=prepared, tools=TOOLS))
        await service.turn(m.AssistantTurnIn(conversation_id="history", user_text=DETAILS, tools=TOOLS))
        expected = [REQUEST, json.dumps(prepared.model_dump(mode="json")), DETAILS]
        requests = gate.requests_for("assistant_next_action")
        assert [line.text for line in requests[2].transcript] == expected
        assert [line.text for line in requests[1].transcript] == expected[:2]
        # The caller retains the entire history; only Jev's build_state truncates it.
        for _ in range(7):
            await service.turn(m.AssistantTurnIn(conversation_id="history", event="call_ended", tools=TOOLS))
        assert [line.text for line in gate.requests_for("assistant_next_action")[-1].transcript] == (
            expected + ["call_ended"] * 7)
        assert [line.text for line in requests[0].transcript] == [REQUEST]
        await service.turn(m.AssistantTurnIn(conversation_id="other", user_text="Hello", tools=TOOLS))
        assert [line.text for line in gate.requests_for("assistant_next_action")[-1].transcript] == ["Hello"]
    finally:
        env.close()


async def test_u63_4_update_task_fields_come_from_l1(tmp_path):
    env = AEnv(tmp_path)
    llm = RecordingLlm(extracted=_details_extracted())
    service = env.assistant(gate=ScriptedGate({"assistant_next_action": "update_task"}), llm=llm)
    try:
        out = await service.turn(m.AssistantTurnIn(
            conversation_id="l1", task_id="task-u63", user_text=DETAILS, tools=TOOLS))
        assert out.next.arguments == {"task_id": "task-u63", "fields": FIELDS}
        # Conflicting labeled text cannot replace L1's non-null values.
        out = await service.turn(m.AssistantTurnIn(
            conversation_id="l1", task_id="task-u63", tools=TOOLS,
            user_text="name: Other Person; phone: 555-0100; address: Other Street; account number: 999"))
        assert out.next.arguments == {"task_id": "task-u63", "fields": FIELDS}
        for account, expected in [("LE-00 01-23", "000123"), ("unknown", None)]:
            llm.extracted = m.ExtractedRequest(account_number=account)
            out = await service.turn(m.AssistantTurnIn(
                conversation_id="l1", task_id="task-u63", user_text="account number: 999", tools=TOOLS))
            assert out.next.arguments == {"task_id": "task-u63", "fields": (
                {"account_number": expected} if expected is not None else {})}
    finally:
        env.close()


async def test_u63_4_labeled_values_have_no_trailing_separators(tmp_path):
    env = AEnv(tmp_path)
    service = env.assistant(gate=ScriptedGate({"assistant_next_action": "update_task"}), llm=RecordingLlm())
    try:
        out = await service.turn(m.AssistantTurnIn(
            conversation_id="labeled", task_id="task-u63", tools=TOOLS,
            user_text="My name is Alex Doe, my phone number is (312) 555-0199, "
                      "my address is 1200 W Example St Apt 4B Chicago IL, and my account number is LE-000123."))
        fields = out.next.arguments["fields"]
        assert fields["name"] == "Alex Doe"
        assert fields["phone_number"] == "(312) 555-0199"
        assert fields["address"] == "1200 W Example St Apt 4B Chicago IL"
        assert fields == FIELDS
        for separator in [",", ";", ":", "&", "and", ", AND &:"]:
            out = await service.turn(m.AssistantTurnIn(
                conversation_id="labeled", task_id="task-u63", tools=TOOLS,
                user_text=f"name: Alex Doe {separator}"))
            assert out.next.arguments["fields"] == {"name": "Alex Doe"}
        # Null L1 values use only this turn's explicit fields, without carrying old ones.
        out = await service.turn(m.AssistantTurnIn(
            conversation_id="labeled", task_id="task-u63", tools=TOOLS, user_text="Thanks"))
        assert out.next.arguments["fields"] == {}
    finally:
        env.close()


async def test_u63_4_chat_details_reach_ready_card(tmp_path, skills_dir):
    copied = shutil.copytree(skills_dir, tmp_path / "skills", ignore=shutil.ignore_patterns("hidden"))
    skills = load_skills(copied)
    registry = load_registry(skills).validate()
    policy = Policy.load(registry.policy_raw())
    clock = FakeClock()
    trace = FakeTrace(clock)
    jev = FakeJev(point_of=registry.point_of)
    jev.on("next_action", *(PointChooser(prefer(action), confidence=0.95) for action in (
        "prepare_task", "present_task_card", "update_task", "present_task_card")))
    jev.on("company", PointChooser(prefer("company-lakeshore-energy"), confidence=0.95))
    jev.on("intent", PointChooser(prefer("start_service"), confidence=0.95))
    jev.on("precall", PointChooser(prefer("keep_default"), confidence=0.95),
           PointChooser(prefer("keep_default"), confidence=0.95))
    env = AEnv(tmp_path, skills=skills)
    gate = DecisionGate(jev=jev, jev_model=env.settings.jev_model, registry=registry,
                        policy=policy, trace=trace, clock=clock)
    llm = RecordingLlm(extracted=lambda text: _details_extracted() if text == DETAILS else
                       LAKESHORE_EXTRACTED.model_copy(update={"service_address": None}))
    parts = env.parts(gate=gate, llm=llm)
    handlers = ToolHandlers(services=stub_services(env.settings), parts=parts)

    async def run_tool(out):
        assert out.next.kind == "tool_call"
        args = dict(out.next.arguments)
        if out.next.name == "update_task":
            args["fields"] = m.UpdateTaskFields.model_validate(args["fields"])
        result = await getattr(handlers, out.next.name)(**args)
        feedback = m.ToolResultIn(tool_use_id=out.next.tool_use_id, name=out.next.name,
                                  is_error=False, structured=result.model_dump(mode="json"))
        following = await parts.assistant.turn(m.AssistantTurnIn(
            conversation_id="ready", tools=TOOLS, task_id=result.task_id, tool_result=feedback))
        return result, following

    try:
        assert env.store.get_profile() == m.Profile()
        first = await parts.assistant.turn(m.AssistantTurnIn(
            conversation_id="ready", user_text=REQUEST, tools=TOOLS))
        prepared, shown = await run_tool(first)
        assert prepared.status == "setup_required"
        assert {field.key for field in prepared.missing} == set(FIELDS)
        assert shown.next.kind == "await_user" and shown.next.ui == "task_card"
        followup = await parts.assistant.turn(m.AssistantTurnIn(
            conversation_id="ready", task_id=prepared.task_id, user_text=DETAILS, tools=TOOLS))
        updated, final = await run_tool(followup)
        assert updated.status == "ready"
        assert updated.missing == []
        assert {field.key: field.display for field in updated.task_card.shared_fields} == FIELDS
        assert final.next.kind == "await_user"
        assert final.next.ui == "task_card"
        assert env.store.get_task(prepared.task_id)["status"] == "ready"
        assert [call.response.answers["next_action"].choice for call in jev.calls_for("next_action")] == [
            "prepare_task", "present_task_card", "update_task", "present_task_card"]
        assert jev.remaining == 0
        assert env.factory.sessions == []
    finally:
        env.close()
