"""AC-21：/assistant/turn 对 Jev 发起 assistant_next_action 决策：候选 = 请求里带来的 tools/list 工具名，加 4 个固定动作；
instructions 和 criteria 文本与 skills/assisted-call/jev/questions.yaml 一致。

A 侧（本文件）：AssistantService.turn() 经 DecisionGate.decide("assistant_next_action", request) 决策，
request.candidates 恰好是请求里的 tools（id=工具名，description=tools/list 的 description）；
4 个固定动作由 G 的 gate 按 questions.yaml 的 extra_criteria 补，调用方不放（r3 缺口 A-G-6，定稿 gate_appends）；
响应的 decision、skill 字段来自这次决策与 assisted-call skill。
G 侧（gate.decide 用 questions.yaml 的 instructions 与 extra_criteria 组 Jev 题）由 G 线测试覆盖（AC-21 的 G 部分）。
决策门用 ScriptedGate（G 线替身）。
"""

import pytest

from fakes import FakeClock, FakeTrace
from waitless import models as m
from waitless.assistant import AssistantService

from a_support import (
    LAKESHORE_EXTRACTED,
    Pick,
    RecordingLlm,
    ScriptedGate,
    build_skills,
    candidate_ids,
    request_texts,
)

USER_TEXT = "I just moved. Can you start my electricity with Lakeshore Energy on November 1?"
TOOLS = [m.ToolInfo(name=n, description=f"{n.replace('_', ' ')} (fixture tool description #{i})")
         for i, n in enumerate(m.TOOL_NAMES)]


@pytest.fixture
def skills(tmp_path):
    return build_skills(tmp_path / "skills")


def _service(gate, skills, llm=None):
    return AssistantService(gate=gate, llm=llm or RecordingLlm(extracted=LAKESHORE_EXTRACTED, reply="On it."),
                            skills=skills, trace=FakeTrace(), clock=FakeClock())


async def test_ac21_candidates_come_from_request_tools(skills):
    gate = ScriptedGate({"assistant_next_action": Pick("prepare_task", confidence=0.96)})
    await _service(gate, skills).turn(m.AssistantTurnIn(conversation_id="conv-21", user_text=USER_TEXT, tools=TOOLS))
    requests = gate.requests_for("assistant_next_action")
    assert len(requests) == 1
    req = requests[0]
    assert req.point == "assistant_next_action"
    ids = candidate_ids(req)
    tool_names = {t.name for t in TOOLS}
    assert len(ids) == len(set(ids)), "duplicate candidates"
    assert set(ids) == tool_names, "only the request's tool names; the gate adds the 4 fixed actions (A-G-6)"
    by_id = {c.id: c for c in req.candidates}
    for tool in TOOLS:
        assert by_id[tool.name].description == tool.description
    # 定稿 A-01：非通话决策点的用户原话经 DecisionRequest.transcript 交给 gate
    assert USER_TEXT in request_texts(req)


async def test_ac21_response_carries_the_decision_and_skill(skills):
    gate = ScriptedGate({"assistant_next_action": Pick("prepare_task", confidence=0.88, policy_rule="top_choice")})
    out = await _service(gate, skills).turn(
        m.AssistantTurnIn(conversation_id="conv-21b", user_text=USER_TEXT, tools=TOOLS))
    assert out.conversation_id == "conv-21b"
    result = gate.results[0]
    answer = result.jev.response.answers["next_action"]
    assert out.decision.decision_id == result.decision_id
    assert out.decision.point == "assistant_next_action"
    assert out.decision.confidence == pytest.approx(answer.confidence)
    assert out.decision.probabilities == pytest.approx(answer.probabilities)
    assert out.decision.policy_rule == "top_choice"
    assert out.skill.name == "assisted-call"
    assert out.skill.commit == skills["assisted-call"].version.commit
    assert out.next.kind == "tool_call" and out.next.name == "prepare_task"
