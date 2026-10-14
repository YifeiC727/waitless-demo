"""I2-1: inspect real MCP request timeouts through an in-process ASGI transport."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx2
import uvicorn

from a_support import (
    LAKESHORE_REQUEST, LOOPBACK, StubHandlers, build_skills, canned_outputs,
    make_settings, mcp_app_for,
)
from waitless import app as app_module
from waitless.call import live
from waitless.cli import check_live
from waitless.mcp_server import build_mcp
from .h3_support import holder_company


async def test_check_live_mcp_requests_have_no_read_timeout(
    tmp_path, monkeypatch, company_dir, skills_dir, unused_tcp_port_factory,
):
    company_dir, _, _, _ = holder_company(tmp_path, company_dir, monkeypatch)
    settings = make_settings(
        tmp_path, mode="live-sim", port=unused_tcp_port_factory(),
        company_host=LOOPBACK, company_port=unused_tcp_port_factory(),
        company_dir=company_dir, live_check_request=LAKESHORE_REQUEST,
    )
    skills = build_skills(settings.skills_dir)
    skills["decide-option"] = SimpleNamespace(path=skills_dir / "decide-option")
    trace = SimpleNamespace(
        events=Mock(return_value=[]), menu_to_dtmf_ms=Mock(return_value=[100]),
        check_call=Mock(return_value=[]),
    )
    services = SimpleNamespace(store=Mock(), trace=trace)
    parts = SimpleNamespace(tasks=SimpleNamespace(skills=skills))
    outputs = canned_outputs()
    outputs["get_call_status"] = outputs["get_call_status"].model_copy(
        update={"ended": True, "pending_choice": None, "handoff": None},
    )
    handlers = StubHandlers(outputs)
    app = mcp_app_for(build_mcp(services, handlers))
    monkeypatch.setattr(app_module, "build_services", lambda settings: (services, parts))
    monkeypatch.setattr(app_module, "create_app", lambda *args, **kwargs: app)

    class InProcessServer:
        def __init__(self, config):
            self.started = False
            self.should_exit = False

        async def serve(self):
            async with app.router.lifespan_context(app):
                self.started = True
                while not self.should_exit:
                    await asyncio.sleep(0.001)

    monkeypatch.setattr(uvicorn, "Server", InProcessServer)
    company = Mock(returncode=None, wait=AsyncMock(return_value=0))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=company))
    writer = Mock(wait_closed=AsyncMock())
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(return_value=(None, writer)))
    carrier = Mock(set_scenario=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr(live, "CarrierClient", lambda **kwargs: carrier)

    timeouts = {}

    class RecordingTransport(httpx2.ASGITransport):
        async def handle_async_request(self, request):
            if request.method == "POST":
                payload = json.loads(request.content)
                if payload["method"] == "tools/call":
                    name = payload["params"]["name"]
                    timeouts[name] = dict(request.extensions["timeout"])
            return await super().handle_async_request(request)

    real_client = httpx2.AsyncClient

    def local_client(*args, **kwargs):
        return real_client(*args, transport=RecordingTransport(app=app), **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", local_client)
    report = await check_live("baseline", settings)

    assert report.outcome == "confirmed"
    assert report.confirmation_matches
    assert [name for name, _ in handlers.calls] == [
        "prepare_task", "update_task", "start_assisted_call", "get_call_status", "get_case_result",
    ]
    assert timeouts["prepare_task"]["read"] is None, timeouts
    assert all(timeout["read"] is None for timeout in timeouts.values()), timeouts
    carrier.aclose.assert_awaited_once()
    company.terminate.assert_called_once()
    company.wait.assert_awaited_once()
    services.store.close.assert_called_once()
