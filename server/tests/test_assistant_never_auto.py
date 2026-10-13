"""AC-23：Jev 在 assistant_next_action 里选中用户动作工具时，服务端把 next 改成 await_user（显示按钮），不返回该工具调用。

机制（tech §7.1）：assisted-call policy 的 never_auto 规则（G 线 Policy.evaluate_point）把 4 个用户动作工具改成
present_task_card；assistant.py 按 DecisionResult.intent（策略之后的结果）生成 next，而不是按 Jev 的原始最高项。
决策门用 ScriptedGate：Jev 原始选择是用户动作工具，策略结果是 present_task_card。
"""

import pytest

from fakes import FakeClock, FakeTrace
from waitless import models as m
from waitless.assistant import AssistantService

from a_support import LAKESHORE_EXTRACTED, Pick, RecordingLlm, ScriptedGate, build_skills

TOOLS = [m.ToolInfo(name=n, description=f"{n} description") for n in m.TOOL_NAMES]


@pytest.mark.parametrize("raw_choice", m.USER_ACTION_TOOLS)
async def test_ac23_user_action_tool_choice_becomes_await_user(tmp_path, raw_choice):
    gate = ScriptedGate({"assistant_next_action": Pick("present_task_card", choice=raw_choice, confidence=0.97,
                                                       policy_rule="never_auto")})
    svc = AssistantService(gate=gate, llm=RecordingLlm(extracted=LAKESHORE_EXTRACTED, reply="Please review the card."),
                           skills=build_skills(tmp_path / "skills"), trace=FakeTrace(), clock=FakeClock())
    out = await svc.turn(m.AssistantTurnIn(conversation_id="c23", user_text="Go ahead and call them now.",
                                           tools=TOOLS, task_id="task-23"))
    assert out.next.kind == "await_user"
    assert out.next.ui == "task_card"
    assert out.decision.policy_rule == "never_auto"
    assert out.decision.decision_id == gate.results[0].decision_id
    # 返回体里任何位置都不出现对用户动作工具的调用
    dumped = out.model_dump(mode="json")
    assert dumped["next"].get("name") is None
