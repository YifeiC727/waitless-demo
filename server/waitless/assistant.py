"""`/assistant/turn`：Alexa+ 模拟端的大脑（A 线；tech §6.2、§6.6 DP-A1；AC-21、AC-22、AC-23；H3、K11）。

公开接口（§7）：AssistantService.turn(AssistantTurnIn) -> AssistantTurnOut、ARG_BUILDERS。
每步顺序（§6.2）：有 user_text 时 L1 抽字段 → Jev DP-A1 选下一步 → 策略转成 next →
参数由程序按 ARG_BUILDERS 填（不经大模型）→ L2 生成 say。
"""

import asyncio
import json
import re
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
    current_fields: dict[str, str] = Field(default_factory=dict)
    task_card: TaskCard | None = None
    transcript: list[TranscriptLine] = Field(default_factory=list)


ArgBuilder = Callable[[ConversationState, AssistantTurnIn], dict[str, Any]]

# 工具名 → 参数构造函数（§6.2 参数来源表）。用户动作工具不在这里：它们永远不会出现在 next 里（AC-23）。
ARG_BUILDERS: dict[str, ArgBuilder] = {
    "find_official_number": lambda state, inp: {"company": state.fields.company_name or "", "intent": state.fields.intent_text or ""},
    "prepare_task": lambda state, inp: {"request": state.request_text or "", "provider_hint": state.fields.company_name},
    "update_task": lambda state, inp: {"task_id": state.task_id, "fields": dict(state.current_fields)},
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
        async with self._locks.setdefault(inp.conversation_id, asyncio.Lock()):
            state = self._conversations.setdefault(inp.conversation_id, ConversationState(conversation_id=inp.conversation_id))
            state.turns += 1
            previous_task_id = state.task_id
            if inp.task_id:
                state.task_id = inp.task_id
            if inp.tool_result and not inp.tool_result.is_error:
                state.task_id = inp.tool_result.structured.get("task_id", state.task_id)
            if state.task_id != previous_task_id:
                state.task_card = None
            if inp.tool_result and not inp.tool_result.is_error and "task_card" in inp.tool_result.structured:
                card = inp.tool_result.structured["task_card"]
                state.task_card = TaskCard.model_validate(card) if card is not None else None
            if inp.user_text is not None:
                if hasattr(self, "_begin_recording"):
                    self._begin_recording(inp.user_text)
                state.last_user_text = inp.user_text
                state.request_text = state.request_text or inp.user_text
                fields = await self.llm.extract_request(inp.user_text)
                state.current_fields = _explicit_fields(inp.user_text)
                for key, value in {"name": fields.name, "phone_number": fields.phone_number,
                                   "address": fields.service_address, "account_number": fields.account_number}.items():
                    if value is None:
                        continue
                    if key == "account_number":
                        value = _account_digits(value)
                        state.current_fields.pop(key, None)
                        if value is None:
                            continue
                    state.current_fields[key] = value
                merged = state.fields.model_dump()
                merged.update({k: v for k,v in fields.model_dump().items() if v is not None and v != []})
                state.fields = ExtractedRequest.model_validate(merged)
            context = DecisionContext(task_card=state.task_card, allowed_fields={}, standing_rules=[], prior_choices=[],
                                      interrupts_used=0, phase="idle")
            if inp.user_text is not None:
                state.transcript.append(TranscriptLine(speaker="user", text=inp.user_text))
            if inp.tool_result:
                state.transcript.append(TranscriptLine(speaker="user", text=json.dumps(inp.tool_result.model_dump(mode="json"))))
            if inp.event:
                state.transcript.append(TranscriptLine(speaker="user", text=inp.event))
            result = await self.gate.decide("assistant_next_action", DecisionRequest(
                     decision_id=uuid.uuid4().hex, point="assistant_next_action", call_id=None,
                     task_id=state.task_id or inp.conversation_id, turn=None,
                     candidates=[Candidate(id=t.name, label=t.name, description=t.description) for t in inp.tools],
                     context=context, transcript=state.transcript))
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
        state = self._conversations.get(conversation_id)
        return state.model_copy(deep=True) if state else None


def _explicit_fields(text: str) -> dict[str, str]:
    """Read explicitly named profile values; extraction never selects a business action."""
    fields = {}
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        fields.update({k: v for k, v in data.items()
                       if k in {"name", "phone_number", "address", "account_number"} and isinstance(v, str)})
    labels = {"name": "name", "phone_number": r"phone(?:[_ ]number)?",
              "address": "address", "account_number": r"account[_ ]number"}
    # Include the whole next label so prefixes such as "my service" or a
    # company name do not become part of the preceding profile value.
    next_label = re.compile(
        r"\b(?:my\s+(?:[A-Za-z][\w-]*[ \t]+)*?)?"
        r"(?:name|phone(?:[_ ]number)?|(?:service\s+)?address|account[_ ]number)"
        r"\s*(?:is\s+|[:=]\s*)", re.IGNORECASE)
    sentence_end = re.compile(r"[.?!](?:\s+(?=[A-Z])|\s*$)")
    for field, label in labels.items():
        match = re.search(r"\b(?:my\s+)?" + label + r"\s*(?:is\s+|[:=]\s*)([^;\n]+)", text, re.IGNORECASE)
        if match:
            value = match.group(1)
            following = next_label.search(value)
            if following:
                value = value[:following.start()]
            for ending in sentence_end.finditer(value):
                # A street direction/initial such as "W. Example" is part of
                # an address even though the following word is capitalized.
                if (field == "address" and value[ending.start()] == "."
                        and re.search(r"\b[A-Za-z]\.$", value[:ending.start() + 1])):
                    continue
                value = value[:ending.start()]
                break
            value = value.strip().rstrip(".?!").strip()
            value = re.sub(r"(?:\s*(?:,|;|:|&|\band\b))+\s*$", "", value, flags=re.IGNORECASE).strip()
            if field == "account_number":
                value = _account_digits(value)
                if value is None:
                    continue
            fields[field] = value
    return fields


def _account_digits(value: str) -> str | None:
    digits = re.search(r"[0-9](?:[0-9 \t-]*[0-9])?", value)
    return re.sub(r"[ \t-]", "", digits.group()) if digits is not None else None
