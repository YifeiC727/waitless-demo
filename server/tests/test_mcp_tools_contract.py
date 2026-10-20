"""AC-20：11 个工具各有一个正常调用用例，structuredContent 都通过对应的 pydantic 输出模型校验。

机制（tech §7.1）：每个工具函数的返回类型注解为对应的 *Out 模型，SDK 在 tools/call 时按 outputSchema 校验 structured_content。
这里只测 MCP 注册与传输层：build_mcp(services, handlers=StubHandlers()) 的处理函数返回预制的合法输出，
官方 SDK 客户端逐个调用，断言 structuredContent 原样到达、能被同一个 *Out 模型校验，且入参原样交给处理函数。
各工具的业务逻辑另由 AC-24–AC-38 的测试覆盖。
"""

import pytest

from waitless import models as m
from waitless.mcp_server import build_mcp

from a_support import (
    TOOL_ARGS as LEGACY_TOOL_ARGS, StubHandlers as LegacyStubHandlers,
    make_settings, mcp_app_for, mcp_session, stub_services,
)


TOOL_ARGS = {
    **LEGACY_TOOL_ARGS,
    "hangup_call": {"task_id": "task-1"},
    "report_call_result": {"task_id": "task-1", "call_id": "call-1", "outcome": "confirmed",
                           "confirmation_number": None},
}


class StubHandlers(LegacyStubHandlers):
    """在本契约测试内扩展夹具，保持共享 a_support 与其他线不变。"""

    def __init__(self):
        super().__init__()
        self.outputs.update({
            "hangup_call": m.HangupCallOut(status="hung_up", call_id="call-1", remote_hangup_pending=False),
            "report_call_result": m.ReportCallResultOut(
                call_id="call-1", outcome="confirmed", confirmation_number=None,
                verified=[], follow_ups=[], summary="用户填写", result_source="user",
                confirmation_source=None, awaiting_user_result=False),
        })

    async def hangup_call(self, task_id: str) -> m.HangupCallOut:
        return await self._respond("hangup_call", task_id=task_id)

    async def report_call_result(self, task_id: str, call_id: str, outcome: m.FinalOutcome,
                                 confirmation_number: str | None = None) -> m.ReportCallResultOut:
        return await self._respond("report_call_result", task_id=task_id, call_id=call_id,
                                   outcome=outcome, confirmation_number=confirmation_number)


@pytest.mark.parametrize("tool", m.TOOL_NAMES)
async def test_ac20_tool_call_structured_content_matches_output_model(tool, tmp_path):
    handlers = StubHandlers()
    server = build_mcp(stub_services(make_settings(tmp_path)), handlers=handlers)
    async with mcp_session(mcp_app_for(server)) as client:
        result = await client.call_tool(tool, TOOL_ARGS[tool])
    assert result.is_error is not True, result
    out_model = m.TOOL_IO[tool][1]
    assert isinstance(result.structured_content, dict)
    validated = out_model.model_validate(result.structured_content)
    assert validated == handlers.outputs[tool]
    assert result.structured_content == handlers.outputs[tool].model_dump(mode="json")
    # 处理函数恰好被调用一次，收到客户端给的入参
    calls = handlers.calls_for(tool)
    assert len(calls) == 1
    got = calls[0]
    for key, value in TOOL_ARGS[tool].items():
        if key == "fields":
            assert isinstance(got["fields"], m.UpdateTaskFields)
            assert got["fields"].model_dump(exclude_none=True) == value
        else:
            assert got[key] == value, (tool, key)
