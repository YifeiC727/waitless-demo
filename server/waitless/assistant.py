"""`/assistant/turn`：Alexa+ 模拟端的大脑（A 线；tech §6.2、§6.6 DP-A1；AC-21、AC-22、AC-23；H3、K11）。

公开接口（§7）：AssistantService.turn(AssistantTurnIn) -> AssistantTurnOut、ARG_BUILDERS。
每步顺序（§6.2）：有 user_text 时 L1 抽字段 → Jev DP-A1 选下一步 → 策略转成 next →
参数由程序按 ARG_BUILDERS 填（不经大模型）→ L2 生成 say。
"""

import asyncio
import json
import uuid
from waitless.models import (Candidate, DecisionContext, DecisionRequest, TranscriptLine, TaskCard, AssistantDecision,
    AssistantSkillRef, NextAwaitUser, NextDone, NextPoll, NextToolCall, USER_ACTION_TOOLS)

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
ARG_BUILDERS: dict[str, ArgBuilder] = {
    "find_official_number": lambda state, inp: {"company": state.fields.company_name or "", "intent": state.fields.intent_text or ""},
    "prepare_task": lambda state, inp: {"request": state.request_text or "", "provider_hint": state.fields.company_name},
    "update_task": lambda state, inp: {"task_id": state.task_id, "fields": {}},
    "get_call_status": lambda state, inp: {"task_id": state.task_id},
    "get_case_result": lambda state, inp: {"task_id": state.task_id},
}


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
        self.gate, self.llm, self.skills, self.trace, self.clock = gate, llm, skills, trace, clock
        self._conversations = {}
        self._locks = {}

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
        async with self._locks.setdefault(inp.conversation_id, asyncio.Lock()):
            state = self._conversations.setdefault(inp.conversation_id, ConversationState(conversation_id=inp.conversation_id))
            state.turns += 1
            if inp.task_id:
                state.task_id = inp.task_id
            if inp.tool_result and not inp.tool_result.is_error:
                state.task_id = inp.tool_result.structured.get("task_id", state.task_id)
            if inp.user_text is not None:
                state.last_user_text = inp.user_text
                state.request_text = state.request_text or inp.user_text
                fields = await self.llm.extract_request(inp.user_text)
                merged = state.fields.model_dump()
                merged.update({k: v for k,v in fields.model_dump().items() if v is not None and v != []})
                state.fields = ExtractedRequest.model_validate(merged)
            context = DecisionContext(task_card=None, allowed_fields={}, standing_rules=[], prior_choices=[],
                                      interrupts_used=0, phase="idle")
            if inp.tool_result and not inp.tool_result.is_error:
                card = inp.tool_result.structured.get("task_card")
                if card:
                    context.task_card = TaskCard.model_validate(card)
            lines = []
            if state.last_user_text:
                lines.append(TranscriptLine(speaker="user", text=state.last_user_text))
            if inp.tool_result:
                lines.append(TranscriptLine(speaker="user", text=json.dumps(inp.tool_result.model_dump(mode="json"))))
            if inp.event:
                lines.append(TranscriptLine(speaker="user", text=inp.event))
            result = await self.gate.decide("assistant_next_action", DecisionRequest(
                     decision_id=uuid.uuid4().hex, point="assistant_next_action", call_id=None,
                     task_id=state.task_id or inp.conversation_id, turn=None,
                     candidates=[Candidate(id=t.name, label=t.name, description=t.description) for t in inp.tools],
                     context=context, transcript=lines))
            answer = result.jev.response.answers["next_action"]
            decision = AssistantDecision(decision_id=result.decision_id, point="assistant_next_action", choice=result.intent,
                          confidence=answer.confidence, probabilities=answer.probabilities, policy_rule=result.policy_rule)
            intent = result.intent
            if intent in USER_ACTION_TOOLS or intent == "present_task_card":
                next_step = NextAwaitUser(kind="await_user", ui="task_card")
            elif intent == "wait_for_call" and state.task_id:
                next_step = NextPoll(kind="poll", task_id=state.task_id)
            elif intent in ARG_BUILDERS and intent in {t.name for t in inp.tools}:
                arguments = ARG_BUILDERS[intent](state, inp)
                next_step = NextToolCall(kind="tool_call", tool_use_id=uuid.uuid4().hex, name=intent, arguments=arguments)
            else:
                next_step = NextDone(kind="done")
            skill = self.skills["assisted-call"]
            say = await self.llm.phrase_reply(skill_body=skill.body, decision=decision,
                                            tool_result=inp.tool_result, user_text=inp.user_text)
            return AssistantTurnOut(conversation_id=inp.conversation_id, say=say, next=next_step, decision=decision,
                                    skill=AssistantSkillRef(name=skill.name, commit=skill.version.commit))

    def state(self, conversation_id: str) -> ConversationState | None:
        raise NotImplementedError
