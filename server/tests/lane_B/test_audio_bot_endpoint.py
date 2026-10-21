"""Explicit company-bot endpoint validation, without Sonic or credentials."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from waitless.company.app import create_company_app
from tests.company._live_harness import company_settings


@pytest.mark.parametrize('query', [
    '', 'role=boss&call_sid=CA' + '0' * 32,
    'role=agent', 'role=rep&call_sid=CA' + '0' * 32,
])
def test_bot_parameters_are_rejected_by_registered_endpoint(
    mode_env, engine_config, company_dir, tmp_path, query,
):
    settings = company_settings(mode_env('live-sim', company=True), engine_config, company_dir, tmp_path)
    app = create_company_app(settings)
    assert any(getattr(route, 'path', None) == '/ws/company-bot' for route in app.routes)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect('/ws/company-bot?' + query):
                pass
        assert exc.value.code == 4401
