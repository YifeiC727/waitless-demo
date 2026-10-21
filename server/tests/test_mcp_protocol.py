"""AC-18：initialize 响应的 protocolVersion 是 2025-11-25；/mcp 走 Streamable HTTP，POST 的 JSON-RPC 返回 application/json。

机制（tech §7.1）：MCPServer + streamable_http_app(stateless_http=True, json_response=True)，挂在 create_app 的 "/"，
内部路径 /mcp；session_manager.run() 在父 lifespan 里跑（inv-1 §2.1）。
"""

import httpx
import pytest

from a_support import BASE_URL, INIT_PARAMS, MCP_HEADERS, build_app, jsonrpc, mcp_session


@pytest.fixture
def app(tmp_path):
    app, _settings, store = build_app(tmp_path)
    yield app
    store.close()


async def _post(app, body, *, headers=None) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as client:
            return await client.post("/mcp", headers={**MCP_HEADERS, **(headers or {})}, json=body)


async def test_ac18_initialize_negotiates_2025_11_25_as_json(app):
    r = await _post(app, jsonrpc("initialize", INIT_PARAMS, rid=7))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 7
    assert body["result"]["protocolVersion"] == "2025-11-25"


async def test_ac18_tools_list_post_returns_json_not_sse(app):
    r = await _post(app, jsonrpc("tools/list", {}, rid=8), headers={"MCP-Protocol-Version": "2025-11-25"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert isinstance(r.json()["result"]["tools"], list)


async def test_ac18_official_sdk_handshake_is_2025_11_25(app):
    async with mcp_session(app, mode="legacy") as client:
        assert client.protocol_version == "2025-11-25"
