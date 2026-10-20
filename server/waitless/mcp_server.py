"""MCP server：11 个工具（A 线；tech §6.1、v5.27；AC-18、AC-19、AC-20、AC-24、AC-32）。

公开接口（§7）：build_mcp(services) -> MCPServer；工具函数与 §6.1 同名；输出模型在 models.py。
工具处理逻辑放在 ToolHandlers（便于单测），build_mcp 只做注册：

    mcp = MCPServer("waitless")
    @mcp.tool(name="prepare_task", structured_output=True)
    async def prepare_task(request: str, provider_hint: str | None = None) -> PrepareTaskOut:
        return await handlers.prepare_task(request, provider_hint)

入参约束（长度、严格布尔与整数）与 models 里的 *In 模型一致，用 Annotated[..., Field(...)] 写在工具函数签名上，
使 tools/list 的 inputSchema 带上这些约束。歧义表 A-28（定稿 keep_two_in_sync，暂定）：A 线负责两份保持一致，
隐藏测试会对比 tools/list 的 inputSchema 与 *In 模型的 model_json_schema()。业务错误以 ToolFailure 抛出，统一转成
isError=true、content[0].text = {"code","message"} 的结果（§6.1 公共约定）。
"""

from typing import Annotated
from pydantic import Field, StrictBool, StrictInt
from waitless.models import Id

from typing import TYPE_CHECKING, Any

from waitless.interfaces import AppServices, ToolFailure
from waitless.models import (
    AnswerChoiceOut,
    CallStatusOut,
    CaseResultOut,
    FinalOutcome,
    HangupCallOut,
    JoinCallOut,
    OfficialNumberOut,
    PrepareTaskOut,
    ReportCallResultOut,
    ReturnOut,
    StartCallOut,
    UpdateTaskFields,
    UpdateTaskOut,
)

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from waitless.app import AppParts

__all__ = ["TOOL_DESCRIPTIONS", "ToolHandlers", "build_mcp", "tool_error_result"]

TOOL_DESCRIPTIONS: dict[str, str] = {
    "find_official_number": "Verify a company's official phone number from trusted sources.",
    "prepare_task": "Prepare a task card for review before a call.",
    "update_task": "Update profile fields and pre-call choices on a task card.",
    "start_assisted_call": "Start a call only after the user confirms the current task card.",
    "get_call_status": "Read the current call phase, pending choices and handoff status.",
    "answer_pending_choice": "Record the user's explicit answer to a pending choice.",
    "join_call": "Join a ready identity handoff with AI media disconnected.",
    "return_to_agent": "Return an active handoff to the AI assistant.",
    "get_case_result": "Read confirmed results, follow-ups and the full call history.",
    "hangup_call": "Only on the user's explicit action: hang up the current call, including while waiting or joining. Idempotently return its call_id, status and remote_hangup_pending for retry.",
    "report_call_result": "Only on the user's explicit action: report confirmed, failed or needs_follow_up for the latest ended call the user actually joined, with an optional confirmation number. Return the stored result; identical retries are idempotent, different payloads return result_conflict and ineligible calls return result_not_allowed.",
}


class ToolHandlers:
    """§6.1 每个工具一个方法，参数与工具入参同名。"""

    def __init__(self, services: AppServices, parts: "AppParts") -> None:
        """parts：app.AppParts（tasks、calls、results、assistant、fetcher、trusted_sources；缺口 A-G-1，
        定稿 app_parts_dataclass）。测试可传自建的 AppParts，或继续用 ToolHandlers 子类做 MCP 层的替身。"""
        self.services = services
        self.parts = parts

    async def find_official_number(self, company: str, intent: str) -> OfficialNumberOut:
        from waitless.official_number import find_official_number
        return find_official_number(company, intent, self.parts.fetcher, trusted_sources=self.parts.trusted_sources)

    async def prepare_task(self, request: str, provider_hint: str | None = None) -> PrepareTaskOut:
        return await self.parts.tasks.prepare(request, provider_hint)

    async def update_task(
        self,
        task_id: str,
        fields: UpdateTaskFields | None = None,
        decisions: dict[str, str] | None = None,
    ) -> UpdateTaskOut:
        return await self.parts.tasks.update(task_id, fields=fields, decisions=decisions)

    async def start_assisted_call(self, task_id: str, confirmed: bool, card_version: int) -> StartCallOut:
        return await self.parts.calls.start(task_id, confirmed, card_version)

    async def get_call_status(self, task_id: str, debug: bool = False) -> CallStatusOut:
        return self.parts.calls.status(task_id, debug)

    async def answer_pending_choice(self, task_id: str, choice_id: str, option_id: str) -> AnswerChoiceOut:
        return await self.parts.calls.answer(task_id, choice_id, option_id)

    async def join_call(self, task_id: str) -> JoinCallOut:
        return await self.parts.calls.join(task_id)

    async def return_to_agent(self, task_id: str, handoff_id: str) -> ReturnOut:
        return await self.parts.calls.return_(task_id, handoff_id)

    async def get_case_result(self, task_id: str) -> CaseResultOut:
        return self.parts.results.case_result(task_id)

    async def hangup_call(self, task_id: str) -> HangupCallOut | dict[str, Any]:
        """A1 仅冻结 I/O；A2 接入 CallManager/LiveCall。"""
        return {"ok": False, "error": "not_implemented"}

    async def report_call_result(
        self, task_id: str, call_id: str, outcome: FinalOutcome,
        confirmation_number: str | None = None,
    ) -> ReportCallResultOut | dict[str, Any]:
        """A1 仅冻结 I/O；A2 核实接手/终态并原子存储用户结果。"""
        return {"ok": False, "error": "not_implemented"}


def build_mcp(services: AppServices, handlers: ToolHandlers | None = None) -> "MCPServer":
    """注册 11 个工具，全部 @mcp.tool(structured_output=True)，返回 models 里的输出模型（AC-19、AC-20）。"""
    from mcp.server import MCPServer
    if handlers is None:
        parts = getattr(services, "_parts", None)
        if parts is None:
            raise ValueError("Use build_services() or supply ToolHandlers with AppParts")
        handlers = ToolHandlers(services, parts)
    mcp = MCPServer("waitless")
    _register_tools(mcp, handlers)
    return mcp


def tool_error_result(failure: ToolFailure) -> Any:
    """返回 mcp_types.CallToolResult(is_error=True, content=[TextContent(text=json{"code","message"})])。"""
    import json
    from mcp_types import CallToolResult, TextContent
    return CallToolResult(is_error=True, content=[TextContent(type="text", text=json.dumps(
                          {"code": failure.code, "message": failure.message}))])


def _contract_tool_result(value: Any) -> Any:
    """占位错误以 isError 传输，不经成功 outputSchema 校验；保留成功响应模型。"""
    if isinstance(value, dict) and value == {"ok": False, "error": "not_implemented"}:
        import json
        from mcp_types import CallToolResult, TextContent
        return CallToolResult(is_error=True, content=[TextContent(type="text", text=json.dumps(value))])
    return value


def _register_tools(mcp, handlers):
    @mcp.tool(name="find_official_number", description=TOOL_DESCRIPTIONS["find_official_number"], structured_output=True)
    async def find_official_number(company: Annotated[str, Field(min_length=1, max_length=100)], intent: Annotated[str, Field(min_length=1, max_length=200)]) -> OfficialNumberOut:
        try:
            return await handlers.find_official_number(company=company, intent=intent)
        except ToolFailure as failure:
            return tool_error_result(failure)

    @mcp.tool(name="prepare_task", description=TOOL_DESCRIPTIONS["prepare_task"], structured_output=True)
    async def prepare_task(request: Annotated[str, Field(min_length=1, max_length=500)], provider_hint: str | None = None) -> PrepareTaskOut:
        try:
            return await handlers.prepare_task(request=request, provider_hint=provider_hint)
        except ToolFailure as failure:
            return tool_error_result(failure)

    @mcp.tool(name="update_task", description=TOOL_DESCRIPTIONS["update_task"], structured_output=True)
    async def update_task(task_id: Id, fields: UpdateTaskFields | None = None, decisions: dict[str, str] | None = None) -> UpdateTaskOut:
        try:
            return await handlers.update_task(task_id=task_id, fields=fields, decisions=decisions)
        except ToolFailure as failure:
            return tool_error_result(failure)

    @mcp.tool(name="start_assisted_call", description=TOOL_DESCRIPTIONS["start_assisted_call"], structured_output=True)
    async def start_assisted_call(task_id: Id, confirmed: StrictBool, card_version: StrictInt) -> StartCallOut:
        try:
            return await handlers.start_assisted_call(task_id=task_id, confirmed=confirmed, card_version=card_version)
        except ToolFailure as failure:
            return tool_error_result(failure)

    @mcp.tool(name="get_call_status", description=TOOL_DESCRIPTIONS["get_call_status"], structured_output=True)
    async def get_call_status(task_id: Id, debug: bool = False) -> CallStatusOut:
        try:
            return await handlers.get_call_status(task_id=task_id, debug=debug)
        except ToolFailure as failure:
            return tool_error_result(failure)

    @mcp.tool(name="answer_pending_choice", description=TOOL_DESCRIPTIONS["answer_pending_choice"], structured_output=True)
    async def answer_pending_choice(task_id: Id, choice_id: Id, option_id: Id) -> AnswerChoiceOut:
        try:
            return await handlers.answer_pending_choice(task_id=task_id, choice_id=choice_id, option_id=option_id)
        except ToolFailure as failure:
            return tool_error_result(failure)

    @mcp.tool(name="join_call", description=TOOL_DESCRIPTIONS["join_call"], structured_output=True)
    async def join_call(task_id: Id) -> JoinCallOut:
        try:
            return await handlers.join_call(task_id=task_id)
        except ToolFailure as failure:
            return tool_error_result(failure)

    @mcp.tool(name="return_to_agent", description=TOOL_DESCRIPTIONS["return_to_agent"], structured_output=True)
    async def return_to_agent(task_id: Id, handoff_id: Id) -> ReturnOut:
        try:
            return await handlers.return_to_agent(task_id=task_id, handoff_id=handoff_id)
        except ToolFailure as failure:
            return tool_error_result(failure)

    @mcp.tool(name="get_case_result", description=TOOL_DESCRIPTIONS["get_case_result"], structured_output=True)
    async def get_case_result(task_id: Id) -> CaseResultOut:
        try:
            return await handlers.get_case_result(task_id=task_id)
        except ToolFailure as failure:
            return tool_error_result(failure)

    @mcp.tool(name="hangup_call", description=TOOL_DESCRIPTIONS["hangup_call"], structured_output=True)
    async def hangup_call(task_id: Id) -> HangupCallOut:
        try:
            return _contract_tool_result(await handlers.hangup_call(task_id=task_id))
        except ToolFailure as failure:
            return tool_error_result(failure)

    @mcp.tool(name="report_call_result", description=TOOL_DESCRIPTIONS["report_call_result"], structured_output=True)
    async def report_call_result(task_id: Id, call_id: Id, outcome: FinalOutcome, confirmation_number: str | None = None) -> ReportCallResultOut:
        try:
            return _contract_tool_result(await handlers.report_call_result(
                task_id=task_id, call_id=call_id, outcome=outcome, confirmation_number=confirmation_number))
        except ToolFailure as failure:
            return tool_error_result(failure)

