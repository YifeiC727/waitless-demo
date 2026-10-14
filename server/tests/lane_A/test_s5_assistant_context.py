import pytest

from a_support import AEnv, ScriptedGate, sample_card
from waitless import models as m


@pytest.mark.parametrize("change", ["input-task", "result-task", "null-card"])
async def test_assistant_clears_card_when_current_task_changes(tmp_path, change):
    env = AEnv(tmp_path)
    gate = ScriptedGate({"assistant_next_action": "reply_only"})
    service = env.assistant(gate=gate)
    tools = [m.ToolInfo(name="prepare_task", description="Prepare a card")]
    try:
        await service.turn(m.AssistantTurnIn(
            conversation_id="conversation", tools=tools,
            tool_result=m.ToolResultIn(tool_use_id="first", name="prepare_task", is_error=False,
                                       structured={"task_id": "old-task", "task_card": sample_card().model_dump(mode="json")})))
        assert service.state("conversation").task_card is not None
        kwargs = {"task_id": "new-task", "event": "call_ended"} if change == "input-task" else {
            "tool_result": m.ToolResultIn(tool_use_id="next", name="prepare_task", is_error=False,
                                           structured={"task_id": "new-task"} if change == "result-task"
                                           else {"task_id": "old-task", "task_card": None})}
        await service.turn(m.AssistantTurnIn(conversation_id="conversation", tools=tools, **kwargs))
        assert gate.requests_for("assistant_next_action")[-1].context.task_card is None
        assert service.state("conversation").task_card is None
    finally:
        env.close()
