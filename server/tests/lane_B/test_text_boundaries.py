"""Additional B text checks for transition inputs and line lifecycle."""

import asyncio
import json

import pytest

from waitless.call.line_protocol import StopMsg, parse_line_message
from waitless.company.carrier import CarrierState
from waitless.company.config import ScenarioError, load_company, load_scenario
from waitless.company.text import TextCompany
from waitless.models import CarrierDialIn, CompanyMenuConfig
from tests.company._live_harness import FakeEngineLine, loopback_host, no_proxy
from tests.company._scenario_data import drive


def test_correct_digits_reset_timeouts_and_invalid_account_does_not_advance(company_dir):
    company = load_company(company_dir, "lakeshore-energy")
    scenario = load_scenario(company_dir, company.company_id, "baseline")
    text = TextCompany(scenario, company)
    text.start()
    text.on_timeout()
    text.on_digits("1")
    assert all(ln.kind != "hangup" for ln in text.on_timeout())
    text.on_digits("1")
    text.on_speech("Service address")
    lines = text.on_speech("Start date")
    assert lines[-1].expect.intent == "press_field:account_number"
    assert text.on_speech("Not an account number") == []
    wrong = text.on_digits("0#")
    assert wrong[-1].expect.intent == "press_field:account_number"
    assert "rep" not in text.step_log()
    number = company.customers[0].account_number
    assert text.on_digits(number[:3]) == []
    assert text.on_digits(number[3:] + "#")[-1].role == "rep"


def test_altered_scenario_confirmation_format_is_used(company_dir):
    company = load_company(company_dir, "lakeshore-energy")
    scenario = load_scenario(company_dir, company.company_id, "baseline").model_copy(deep=True)
    scenario.scenario_id = "independent-scenario"
    confirm = next(a for a in scenario.steps["rep"].actions if a.do == "confirm")
    confirm.confirmation_format = "TEST-{8d}"
    run = drive(TextCompany(scenario, company), menu_digits=["1", "1"], customer=company.customers[0])
    assert run.problem is None and run.stalled_on is None
    assert run.confirmation.startswith("TEST-") and len(run.confirmation) == 13
    assert run.confirmation in run.confirm_line.text


def test_automatic_prompt_cycle_raises_instead_of_hanging(company_dir):
    company = load_company(company_dir, "lakeshore-energy")
    scenario = load_scenario(company_dir, company.company_id, "baseline").model_copy(deep=True)
    scenario.steps["greeting"].next = "greeting"
    with pytest.raises(ScenarioError, match="cycle"):
        TextCompany(scenario, company).start()


async def test_menu_timeout_sends_stop_before_close_and_stats_once(company_dir, tmp_path, mode_env, monkeypatch):
    env = mode_env("live-sim", company=True)
    host = loopback_host(env)
    no_proxy(monkeypatch, host)
    company = load_company(company_dir, "lakeshore-energy")
    company = company.model_copy(update={"menu": CompanyMenuConfig(timeout_sec=1, max_timeouts=2)})
    state = CarrierState([company])
    state.company_dir = company_dir
    state.company_stats_path = tmp_path / "stats.jsonl"
    async with FakeEngineLine(host) as engine:
        result = await state.place_call(CarrierDialIn(
            call_id="boundary-call", task_id="boundary-task", to_number=company.line_number,
            stream_url=engine.url, stream_token="boundary-token"))
        try:
            await asyncio.wait_for(state._tasks[result.call_sid], 5)
            assert len(engine.conns) == 1
            conn = engine.conns[0]
            assert conn.events() == ["connected", "start", "stop"]
            stop = parse_line_message(conn.messages[-1][1], direction="to_engine")
            assert isinstance(stop, StopMsg) and stop.stop.callSid == result.call_sid
            assert conn.closed_at is not None
            await state.hangup(result.call_sid)
            rows = [json.loads(line) for line in state.company_stats_path.read_text().splitlines()]
            assert rows == [{"call_sid": result.call_sid, "nonsilent_inbound_frames_during_handoff": 0}]
        finally:
            await state._shutdown()
