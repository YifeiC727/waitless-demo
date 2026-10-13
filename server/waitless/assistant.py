"""`/assistant/turn`：Alexa+ 模拟端的大脑（A 线；tech §6.2、§6.6 DP-A1；AC-21、AC-22、AC-23；H3、K11）。

公开接口（§7）：AssistantService.turn(AssistantTurnIn) -> AssistantTurnOut、ARG_BUILDERS。
每步顺序（§6.2）：有 user_text 时 L1 抽字段 → Jev DP-A1 选下一步 → 策略转成 next →
参数由程序按 ARG_BUILDERS 填（不经大模型）→ L2 生成 say。
"""

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, Field

from waitless.interfaces import Clock, DecisionGate, LlmTasks, Trace
from waitless.models import AssistantTurnIn, AssistantTurnOut, ExtractedRequest, Skill

__all__ = ["ARG_BUILDERS", "AssistantService", "ConversationState"]


class ConversationState(BaseModel):
    """内存里的对话状态（按 conversation_id）。A 线内部，可扩展。"""

    conversation_id: str
    fields: ExtractedRequest = Field(default_factory=ExtractedRequest)
    request_text: str | None = None
    last_user_text: str | None = None
    task_id: str | None = None
    turns: int = 0


ArgBuilder = Callable[[ConversationState, AssistantTurnIn], dict[str, Any]]

# 工具名 → 参数构造函数（§6.2 参数来源表）。用户动作工具不在这里：它们永远不会出现在 next 里（AC-23）。
# todo(A): find_official_number: {company: fields.company_name, intent: fields.intent_text}
#          prepare_task:         {request: 原始 user_text, provider_hint: fields.company_name}
#          update_task:          {task_id, fields: 用户本轮说出的可代说字段}
#          get_call_status:      {task_id}
#          get_case_result:      {task_id}
ARG_BUILDERS: dict[str, ArgBuilder] = {}


class AssistantService:
    def __init__(
        self,
        *,
        gate: DecisionGate,
        llm: LlmTasks,
        skills: Mapping[str, Skill],
        trace: Trace,
        clock: Clock,
    ) -> None:
        raise NotImplementedError

    async def turn(self, inp: AssistantTurnIn) -> AssistantTurnOut:
        # todo(A): DP-A1 候选 = 请求 tools 的工具名（description 作 criteria）+ ASSISTANT_FIXED_ACTIONS；
        #          instructions 与 criteria 文本取自 skills/assisted-call/jev/questions.yaml（AC-21）。
        #          缺口 A-G-6（定稿 gate_appends）：DecisionRequest.candidates 只放请求里的工具名；4 个固定动作由 G 的 gate
        #          按 questions.yaml 的 extra_criteria 补进 criteria，这里不放。
        # todo(A): DecisionRequest 只交文字：transcript 放用户原话（speaker=user），不调 build_state（A-01、A-26）；
        #          建卡前 context.task_card=null（A-09）。返回的 action 恒为 wait，选中的工具或固定动作在 intent（A-02）。
        # todo(A): Lite 只做 L1（强制抽取工具）和 L2（system=assisted-call SKILL.md 正文）；
        #          Lite 文字里出现「call start_assisted_call」之类不影响 next（AC-22）。
        # todo(A): Jev 选中 USER_ACTION_TOOLS 之一时由 policy 改成 present_task_card → next=await_user（AC-23）。
        # todo(A): 映射：工具名 → next.tool_call；reply_only、ask_clarify → 只 say（next=done）；
        #          present_task_card → await_user(ui=task_card)；wait_for_call → poll。
        # todo(A): decision 字段给网页的追踪面板画概率条（AC-12）；skill 字段给 assisted-call 的 commit。
        raise NotImplementedError

    def state(self, conversation_id: str) -> ConversationState | None:
        raise NotImplementedError
