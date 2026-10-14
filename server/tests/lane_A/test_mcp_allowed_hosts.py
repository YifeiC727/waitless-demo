from urllib.parse import urlunsplit

import httpx
import pytest

from a_support import INIT_PARAMS, LOOPBACK, MCP_HEADERS, build_app, jsonrpc


@pytest.mark.parametrize("allowed_host", [LOOPBACK, "localhost", "fixture-tunnel.example", "[::1]"])
@pytest.mark.parametrize("host_has_port", [False, True])
@pytest.mark.parametrize("origin_scheme", [None, "http", "https"])
@pytest.mark.parametrize("origin_has_port", [False, True])
async def test_allowed_hostname_accepts_host_and_origin_with_any_port(
    tmp_path, unused_tcp_port_factory, allowed_host, host_has_port, origin_scheme, origin_has_port
):
    app, settings, store = build_app(tmp_path, allowed_hosts=[allowed_host])
    host = f"{allowed_host}:{unused_tcp_port_factory()}" if host_has_port else allowed_host
    headers = {**MCP_HEADERS, "Host": host}
    if origin_scheme:
        origin_host = f"{allowed_host}:{unused_tcp_port_factory()}" if origin_has_port else allowed_host
        headers["Origin"] = urlunsplit((origin_scheme, origin_host, "", "", ""))
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=urlunsplit(("http", LOOPBACK, "", "", ""))) as client:
                response = await client.post("/mcp", headers=headers, json=jsonrpc("initialize", INIT_PARAMS))
        assert response.status_code == 200, response.text
        assert response.json()["result"]["protocolVersion"] == "2025-11-25"
        assert settings.allowed_hosts == (allowed_host,)
    finally:
        store.close()


@pytest.mark.parametrize("rejected_header,status", [("Host", 421), ("Origin", 403)])
@pytest.mark.parametrize("untrusted_host", ["unlisted.example", "fixture-tunnel.example.attacker.example", "prefix-fixture-tunnel.example", "localhost", LOOPBACK])
@pytest.mark.parametrize("has_port", [False, True])
async def test_unlisted_hostname_stays_rejected(tmp_path, unused_tcp_port, rejected_header, status, untrusted_host, has_port):
    allowed_host = "fixture-tunnel.example"
    app, _settings, store = build_app(tmp_path, allowed_hosts=[allowed_host])
    authority = f"{untrusted_host}:{unused_tcp_port}" if has_port else untrusted_host
    headers = {**MCP_HEADERS, "Host": allowed_host}
    headers[rejected_header] = authority if rejected_header == "Host" else urlunsplit(("https", authority, "", "", ""))
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=urlunsplit(("http", LOOPBACK, "", "", ""))) as client:
                response = await client.post("/mcp", headers=headers, json=jsonrpc("initialize", INIT_PARAMS))
        assert response.status_code == status
    finally:
        store.close()
