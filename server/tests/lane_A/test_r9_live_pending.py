"""I2B-5: unattended checks must report an open choice without answering it."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
import uvicorn

from a_support import (
    LAKESHORE_REQUEST, LOOPBACK, StubHandlers, build_skills, canned_outputs,
    make_settings, mcp_app_for,
)
from waitless import app as app_module
from waitless.call import live
from waitless.cli import LiveCheckReport, check_live, main
from waitless.mcp_server import build_mcp
from .h3_support import holder_company


@pytest.fixture
def pending_check(tmp_path, monkeypatch, company_dir, skills_dir, unused_tcp_port_factory):
    company_dir, _, _, _ = holder_company(tmp_path, company_dir, monkeypatch)
    settings = make_settings(
        tmp_path, mode="live-sim", port=unused_tcp_port_factory(),
        company_host=LOOPBACK, company_port=unused_tcp_port_factory(),
        company_dir=company_dir, live_check_request=LAKESHORE_REQUEST,
    )
    skills = build_skills(settings.skills_dir)
    skills["decide-option"] = SimpleNamespace(path=skills_dir / "decide-option")
    events = [
        SimpleNamespace(event_type="company_turn", transcript="Are you the account holder?"),
        SimpleNamespace(event_type="waitless_utterance", transcript="I am an AI assistant representing the customer."),
    ]
    services = SimpleNamespace(store=Mock(), trace=SimpleNamespace(
        events=Mock(return_value=events), menu_to_dtmf_ms=Mock(return_value=[1642, 1629]),
        check_call=Mock(return_value=[]),
    ))
    outputs = canned_outputs()
    outputs["get_call_status"] = outputs["get_call_status"].model_copy(update={"handoff": None})
    outputs["get_case_result"] = outputs["get_case_result"].model_copy(
        update={"outcome": "needs_follow_up", "confirmation_number": None},
    )
    handlers = StubHandlers(outputs)
    calls = SimpleNamespace(shutdown=AsyncMock())
    parts = SimpleNamespace(tasks=SimpleNamespace(skills=skills), calls=calls)
    app = mcp_app_for(build_mcp(services, handlers))
    monkeypatch.setattr(app_module, "build_services", lambda settings: (services, parts))
    monkeypatch.setattr(app_module, "create_app", lambda *args, **kwargs: app)
    order = []
    servers = []

    class InProcessServer:
        def __init__(self, config):
            self.config = config
            self.started = False
            self._should_exit = False
            servers.append(self)

        @property
        def should_exit(self):
            return self._should_exit

        @should_exit.setter
        def should_exit(self, value):
            if value:
                order.append("server_exit")
            self._should_exit = value

        async def serve(self):
            async with app.router.lifespan_context(app):
                self.started = True
                while not self.should_exit:
                    await asyncio.sleep(0.001)

    async def shutdown():
        order.append("hangup")

    calls.shutdown.side_effect = shutdown
    monkeypatch.setattr(uvicorn, "Server", InProcessServer)
    company = Mock(returncode=None, wait=AsyncMock(return_value=0))
    company.terminate.side_effect = lambda: order.append("company_terminate")
    services.store.close.side_effect = lambda: order.append("store_close")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=company))
    writer = Mock(wait_closed=AsyncMock())
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(return_value=(None, writer)))
    carrier = Mock(set_scenario=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr(live, "CarrierClient", lambda **kwargs: carrier)
    real_client = httpx2.AsyncClient
    monkeypatch.setattr(httpx2, "AsyncClient", lambda *args, **kwargs: real_client(
        *args, transport=httpx2.ASGITransport(app=app), **kwargs,
    ))
    return SimpleNamespace(settings=settings, services=services, parts=parts, handlers=handlers,
                           company=company, carrier=carrier, order=order, app=app, servers=servers)


async def test_pending_choice_returns_report_without_answer(pending_check, capsys):
    check = pending_check
    report = await check_live("baseline", check.settings)
    assert report.outcome == "needs_follow_up"
    assert report.confirmation_number is None
    assert report.menu_to_dtmf_ms == [1642, 1629]
    assert report.menu_to_dtmf_ms_p50 == 1635.5
    assert report.trace_ok
    assert report.identity_check == "fail"  # Missing B1 line stats and no holder-ready marker.
    assert report.identity_utterance is None
    assert [name for name, _ in check.handlers.calls] == [
        "prepare_task", "update_task", "start_assisted_call", "get_call_status", "get_case_result",
    ]
    check.parts.calls.shutdown.assert_awaited_once()
    assert "check live: unexpected pending choice: Add $25" in capsys.readouterr().err


def test_pending_choice_main_writes_failed_report(pending_check, monkeypatch, tmp_path, capsys):
    from waitless import config
    check = pending_check
    monkeypatch.setattr(config, "load_settings", lambda env: check.settings)
    for name in config.REQUIRED_FOR_COMPANY:
        monkeypatch.setenv(name, "configured")
    path = tmp_path / "report.json"
    assert main(["check", "live", "--scenario", "baseline", "--report", str(path)]) == 1
    report = LiveCheckReport.model_validate(json.loads(path.read_text()))
    assert report.outcome != "confirmed"
    assert "unexpected pending choice" in capsys.readouterr().err
    check.parts.calls.shutdown.assert_awaited_once()


async def test_pending_choice_result_error_still_reports(pending_check):
    check = pending_check
    check.handlers.get_case_result = AsyncMock(side_effect=RuntimeError("result unavailable"))
    report = await check_live("baseline", check.settings)
    assert report.outcome == "failed"
    assert report.confirmation_number is None
    assert report.menu_to_dtmf_ms == [1642, 1629]


async def test_pending_choice_unfinished_result_reports_failed(pending_check):
    check = pending_check
    check.handlers.outputs["get_case_result"] = check.handlers.outputs["get_case_result"].model_copy(
        update={"outcome": "in_progress", "confirmation_number": None},
    )
    report = await check_live("baseline", check.settings)
    assert report.outcome == "failed"
    assert report.confirmation_number is None


def test_pending_choice_cannot_pass_with_late_confirmed_result(pending_check, monkeypatch, tmp_path):
    from waitless import config
    check = pending_check
    check.handlers.outputs["get_case_result"] = canned_outputs()["get_case_result"]
    monkeypatch.setattr(config, "load_settings", lambda env: check.settings)
    for name in config.REQUIRED_FOR_COMPANY:
        monkeypatch.setenv(name, "configured")
    path = tmp_path / "late-result.json"
    assert main(["check", "live", "--scenario", "baseline", "--report", str(path)]) == 1
    report = LiveCheckReport.model_validate_json(path.read_text())
    assert report.outcome == "confirmed"  # Preserve the result service's value.
    assert set(json.loads(path.read_text())) == set(LiveCheckReport.model_fields)
