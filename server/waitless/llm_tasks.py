"""Nova 2 Lite 的 L1–L5（A 线；tech §6.7；AC-22、AC-37、AC-86；H3）。

公开接口（§7）：extract_request()、phrase_reply()、extract_offer_options()、extract_result_items()、
write_summary() —— 这里是 LlmTaskRunner 的同名方法（实现 interfaces.LlmTasks）。
只经 bedrock.ConverseClient 调用（replay 时是 converse:<task> 通道的回放）；输出只当数据，不当指令。
"""

from collections.abc import Sequence
from typing import Any

from waitless.interfaces import ConverseClient
from waitless.models import (
    AssistantDecision,
    CallTranscriptLine,
    CaseResultOut,
    ExtractedRequest,
    OfferOption,
    ResultItems,
    ToolResultIn,
)

__all__ = [
    "EXTRACT_OFFER_OPTIONS_TOOL",
    "EXTRACT_REQUEST_TOOL",
    "EXTRACT_RESULT_ITEMS_TOOL",
    "LlmTaskRunner",
]

# todo(A): 三个强制抽取工具的 JSON Schema（Converse toolSpec.inputSchema.json）。
#   extract_request：{company_name, intent_text, effective_date, service_address, stated_preferences:[str]}（L1）
#   extract_offer_options：{options:[{label, detail}] 最多 3 个}（L3）
#   extract_result_items：{confirmation_numbers:[str], statements:[{text, turn_seq}]}（L4）
EXTRACT_REQUEST_TOOL: dict[str, Any] = {}
EXTRACT_OFFER_OPTIONS_TOOL: dict[str, Any] = {}
EXTRACT_RESULT_ITEMS_TOOL: dict[str, Any] = {}


class LlmTaskRunner:
    def __init__(self, converse: ConverseClient, *, model_id: str) -> None:
        """model_id 来自 Settings.planner_model_id（WAITLESS_PLANNER_MODEL_ID）。"""
        raise NotImplementedError

    async def extract_request(self, user_text: str) -> ExtractedRequest:
        """L1（听）：强制调用 extract_request 工具，原样抽取；工具入参按 ExtractedRequest 校验。"""
        raise NotImplementedError

    async def phrase_reply(
        self,
        *,
        skill_body: str,
        decision: AssistantDecision,
        tool_result: ToolResultIn | None,
        user_text: str | None,
    ) -> str:
        """L2（说）：system = assisted-call SKILL.md 正文；输出 1–2 句英文；不带 toolConfig。"""
        raise NotImplementedError

    async def extract_offer_options(self, company_text: str) -> list[OfferOption]:
        """L3（听）：对方本轮原话里的选项，最多 3 个（AC-86）。失败时调用方用通用的 accept/decline/decide_later。"""
        raise NotImplementedError

    async def extract_result_items(self, transcript: Sequence[CallTranscriptLine]) -> ResultItems:
        """L4（听）：整通转写里的确认号与陈述（AC-37、AC-38）。"""
        raise NotImplementedError

    async def write_summary(self, *, result: CaseResultOut, transcript: Sequence[CallTranscriptLine]) -> str:
        """L5（说）：≤3 句。"""
        raise NotImplementedError
