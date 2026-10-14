"""B-r2: carrier errors follow ApiError without disclosing request data."""

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from waitless.company.carrier import CarrierState, build_carrier_router
from waitless.company.config import load_company
from waitless.models import ApiError, CarrierDialIn, ScenarioSelection


@pytest.fixture
def state(company_dir):
    state = CarrierState([load_company(company_dir, "lakeshore-energy")])
    state.company_dir = company_dir
    return state


def request_data(state, mode_env):
    line = next(iter(state._companies))
    env = mode_env("live-sim", company=True)
    dial = CarrierDialIn(
        call_id="error-call", task_id="error-task", to_number=line,
        stream_url=env["WAITLESS_LINE_WS_URL"], stream_token="test-carrier-token",
    )
    return line, dial


def assert_error(body, code, token):
    assert set(body) == {"code", "message"}
    error = ApiError.model_validate(body)
    assert error.code == code
    assert error.message.strip()
    assert token not in error.message


ERROR_CASES = [
    ("put_line", "unknown_line"),
    ("get_line", "unknown_line"),
    ("put_scenario", "unknown_scenario"),
    ("dial", "unknown_number"),
    ("hangup", "unknown_call"),
]


@pytest.mark.parametrize("operation,code", ERROR_CASES)
async def test_state_404_has_message_and_preserves_selection(state, mode_env, operation, code):
    line, dial = request_data(state, mode_env)
    before = state.get_scenario(line)
    with pytest.raises(HTTPException) as caught:
        if operation == "put_line":
            state.set_scenario(ScenarioSelection(line_number=line + "-unknown", scenario_id="baseline"))
        elif operation == "get_line":
            state.get_scenario(line + "-unknown")
        elif operation == "put_scenario":
            state.set_scenario(ScenarioSelection(line_number=line, scenario_id="missing-scenario"))
        elif operation == "dial":
            await state.place_call(dial.model_copy(update={"to_number": line + "-unknown"}))
        else:
            await state.hangup("missing-call")
    assert caught.value.status_code == 404
    assert_error(caught.value.detail, code, dial.stream_token)
    assert state.get_scenario(line) == before
    assert not state._calls and not state._tasks


@pytest.mark.parametrize("operation,code", ERROR_CASES)
async def test_http_404_has_api_error_shape(state, mode_env, operation, code):
    line, dial = request_data(state, mode_env)
    before = state.get_scenario(line)
    app = FastAPI()
    app.include_router(build_carrier_router(state))
    env = mode_env("live-sim", company=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=env["WAITLESS_CARRIER_URL"]) as client:
        if operation.startswith("put"):
            body = {"line_number": line, "scenario_id": "baseline"}
            body["line_number" if operation == "put_line" else "scenario_id"] += "-unknown"
            response = await client.put("/sim/scenario", json=body)
        elif operation == "get_line":
            response = await client.get("/sim/scenario", params={"line_number": line + "-unknown"})
        elif operation == "dial":
            body = dial.model_dump()
            body["to_number"] += "-unknown"
            response = await client.post("/carrier/calls", json=body)
        else:
            response = await client.post("/carrier/calls/missing-call/hangup")
    assert response.status_code == 404
    assert_error(response.json(), code, dial.stream_token)
    assert state.get_scenario(line) == before


@pytest.mark.parametrize("operation", ["put", "dial"])
async def test_corrupt_configuration_returns_safe_500(state, mode_env, tmp_path, operation):
    line, dial = request_data(state, mode_env)
    before = state.get_scenario(line)
    directory = tmp_path / "lakeshore-energy" / "scenarios"
    directory.mkdir(parents=True)
    # Include a synthetic token in malformed YAML to catch exception-text leaks.
    (directory / "baseline.yaml").write_text("broken: [" + dial.stream_token, encoding="utf-8")
    state.company_dir = tmp_path
    app = FastAPI()
    app.include_router(build_carrier_router(state))
    env = mode_env("live-sim", company=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=env["WAITLESS_CARRIER_URL"]) as client:
        if operation == "put":
            response = await client.put("/sim/scenario", json=before.model_dump())
        else:
            response = await client.post("/carrier/calls", json=dial.model_dump())
    assert response.status_code == 500
    assert_error(response.json(), "invalid_configuration", dial.stream_token)
    assert state.get_scenario(line) == before
    assert not state._calls and not state._tasks
