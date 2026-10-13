"""MCP server：9 个工具（A 线；tech §6.1、inv-1 §2；AC-18、AC-19、AC-20、AC-24、AC-32）。

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

from typing import TYPE_CHECKING, Any

from waitless.interfaces import AppServices, ToolFailure
from waitless.models import (
    AnswerChoiceOut,
    CallStatusOut,
    CaseResultOut,
    JoinCallOut,
    OfficialNumberOut,
    PrepareTaskOut,
    ReturnOut,
    StartCallOut,
    UpdateTaskFields,
    UpdateTaskOut,
)

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from waitless.app import AppParts

__all__ = ["TOOL_DESCRIPTIONS", "ToolHandlers", "build_mcp", "tool_error_result"]

# todo(A): 9 个工具的 description（给任何 MCP 客户端 agent 看；DP-A1 的 criteria 也取自这里，§6.6）。
#          键必须是 models.TOOL_NAMES；不写第三方商标。
TOOL_DESCRIPTIONS: dict[str, str] = {}


class ToolHandlers:
    """§6.1 每个工具一个方法，参数与工具入参同名。"""

    def __init__(self, services: AppServices, parts: "AppParts") -> None:
        """parts：app.AppParts（tasks、calls、results、assistant、fetcher、trusted_sources；缺口 A-G-1，
        定稿 app_parts_dataclass）。测试可传自建的 AppParts，或继续用 ToolHandlers 子类做 MCP 层的替身。"""
        raise NotImplementedError

    async def find_official_number(self, company: str, intent: str) -> OfficialNumberOut:
        raise NotImplementedError

    async def prepare_task(self, request: str, provider_hint: str | None = None) -> PrepareTaskOut:
        raise NotImplementedError

    async def update_task(
        self,
        task_id: str,
        fields: UpdateTaskFields | None = None,
        decisions: dict[str, str] | None = None,
    ) -> UpdateTaskOut:
        # todo(A): fields 里的 forbidden 键与其他未知键 → forbidden_field（AC-32、A-05），见 UpdateTaskFields 的 model_extra；
        #          fields 的 inputSchema 是 additionalProperties: true。工具顶层（与 task_id 同级）的未知键由 SDK 参数模型处理，
        #          A 线第一个测试先用官方客户端核实 mcp==2.3.0 的行为（丢弃还是报错），结果写进报告。
        # todo(A): 返回 UpdateTaskOut：status 取 3 个值、task_card 可空；锁定状态 → task_locked（A-19）。
        raise NotImplementedError

    async def start_assisted_call(self, task_id: str, confirmed: bool, card_version: int) -> StartCallOut:
        # todo(A): 服务端守门（AC-24）：交给 CallManager.start，先验后写。未知 task_id → unknown_task（缺口 R2-1）。
        raise NotImplementedError

    async def get_call_status(self, task_id: str, debug: bool = False) -> CallStatusOut:
        raise NotImplementedError

    async def answer_pending_choice(self, task_id: str, choice_id: str, option_id: str) -> AnswerChoiceOut:
        # todo(A): 未知 task_id → unknown_task（A-18，models.TOOL_ERROR_CODES）。
        raise NotImplementedError

    async def join_call(self, task_id: str) -> JoinCallOut:
        # todo(A): 未知 task_id → unknown_task（A-18）。
        raise NotImplementedError

    async def return_to_agent(self, task_id: str, handoff_id: str) -> ReturnOut:
        # todo(A): 未知 task_id → unknown_task（A-18）。
        raise NotImplementedError

    async def get_case_result(self, task_id: str) -> CaseResultOut:
        raise NotImplementedError


def build_mcp(services: AppServices, handlers: ToolHandlers | None = None) -> "MCPServer":
    """注册 9 个工具，全部 @mcp.tool(structured_output=True)，返回 models 里的输出模型（AC-19、AC-20）。"""
    # todo(A): from mcp.server import MCPServer（mcp==2.3.0；不是 FastMCP，inv-1 §2.1）。
    # todo(A): tools/list 恰好 9 个，名字与顺序同 models.TOOL_NAMES，每个都有 inputSchema 与 outputSchema。
    # todo(A): ToolFailure → tool_error_result(...)；其他异常不要吞掉成功结果。
    # todo(A): DNS-rebinding 白名单用 services.settings.allowed_hosts（K9）。
    raise NotImplementedError


def tool_error_result(failure: ToolFailure) -> Any:
    """返回 mcp_types.CallToolResult(is_error=True, content=[TextContent(text=json{"code","message"})])。"""
    # todo(A): inv-1 §2.3：mcp_types 的 CallToolResult、TextContent。
    raise NotImplementedError
