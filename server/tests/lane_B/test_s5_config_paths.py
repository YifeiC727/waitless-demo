"""B-s5 K2: unresolvable configuration paths are safe configuration errors."""

import httpx
import pytest
from fastapi import FastAPI

from waitless.company.carrier import CarrierState, build_carrier_router
from waitless.company.config import ScenarioError, list_scenarios, load_company, load_scenario


@pytest.fixture
def cyclic_root(tmp_path):
    root = tmp_path / "company"
    root.mkdir()
    # A corrupt local configuration directory; it cannot be resolved or read.
    (root / "lakeshore-energy").symlink_to("lakeshore-energy", target_is_directory=True)
    return root


@pytest.mark.parametrize("loader", [load_company, load_scenario, list_scenarios])
def test_path_resolution_failure_is_scenario_error(cyclic_root, loader):
    args = (cyclic_root, "lakeshore-energy")
    if loader is load_scenario:
        args += ("baseline",)
    with pytest.raises(ScenarioError):
        loader(*args)


@pytest.mark.parametrize("operation", ["put", "dial"])
async def test_unresolvable_configuration_returns_safe_500(
    company_dir, cyclic_root, mode_env, operation,
):
    company = load_company(company_dir, "lakeshore-energy")
    state = CarrierState([company])
    state.company_dir = cyclic_root
    before = state.get_scenario(company.line_number)
    app = FastAPI()
    app.include_router(build_carrier_router(state))
    env = mode_env("live-sim", company=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, raise_app_exceptions=False),
        base_url=env["WAITLESS_CARRIER_URL"],
    ) as client:
        if operation == "put":
            response = await client.put("/sim/scenario", json=before.model_dump())
        else:
            response = await client.post("/carrier/calls", json={
                "call_id": "path-error-call", "task_id": "path-error-task",
                "to_number": company.line_number, "stream_url": env["WAITLESS_LINE_WS_URL"],
                "stream_token": "test-path-error-token",
            })
    assert response.status_code == 500
    assert response.json() == {
        "code": "invalid_configuration", "message": "The company configuration is invalid.",
    }
    assert str(cyclic_root) not in response.text
    assert "test-path-error-token" not in response.text
    assert state.get_scenario(company.line_number) == before
    assert not state._calls and not state._tasks
