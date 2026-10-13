"""AC-19：官方 MCP Python SDK 客户端（不经网页）连 /mcp，tools/list 恰好返回 9 个工具（名单见 tech §6.1），
每个都有 inputSchema 和 outputSchema。

机制（tech §7.1）：create_app() 调 build_mcp(services)，用 @mcp.tool(structured_output=True) 注册 9 个函数，
SDK 按参数类型和返回的 *Out 模型生成两个 schema。测试用官方 SDK 客户端（mcp==2.3.0，Streamable HTTP）连 create_app 的应用。
"""

import pytest

from waitless import models as m

from a_support import build_app, mcp_session


@pytest.fixture
def app(tmp_path):
    app, _settings, store = build_app(tmp_path)
    yield app
    store.close()


async def _list_tools(app):
    async with mcp_session(app) as client:
        return (await client.list_tools()).tools


async def test_ac19_official_client_lists_exactly_the_nine_tools(app):
    tools = await _list_tools(app)
    names = [t.name for t in tools]
    assert len(names) == 9
    assert set(names) == set(m.TOOL_NAMES)


async def test_ac19_every_tool_has_input_and_output_schema(app):
    tools = await _list_tools(app)
    assert tools, "tools/list returned nothing"
    for tool in tools:
        in_model, out_model = m.TOOL_IO[tool.name]
        assert isinstance(tool.input_schema, dict) and tool.input_schema.get("type") == "object", tool.name
        assert isinstance(tool.output_schema, dict) and tool.output_schema.get("type") == "object", tool.name
        # 两个 schema 的字段与 §6.1（models 里的 *In / *Out 模型）一致
        assert set(tool.input_schema.get("properties", {})) == set(in_model.model_fields), tool.name
        assert set(tool.output_schema.get("properties", {})) == set(out_model.model_fields), tool.name

