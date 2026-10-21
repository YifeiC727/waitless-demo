"""U92-A1：旁听票据的 HTTP 边界与仅 live-sim 的只读音频路由。"""

from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest

from a_support import BASE_URL, NOW, AEnv, build_app, mcp_session
from waitless import models
from waitless.call.listen import ListenTicketOut
from waitless.interfaces import ToolFailure


@pytest.fixture
def app(tmp_path):
    env = AEnv(tmp_path, mode="live-sim")
    application = env.app()
    env.add_task("task-listen")
    yield application
    env.close()


def _session(**overrides):
    data = dict(task_id="task-listen", call_id="call-listen", ended=False)
    data.update(overrides)
    return SimpleNamespace(**data)


def _ticket():
    return ListenTicketOut(task_id="session-task", call_id="call-listen",
                           ws_path="/ws/listen/call-listen", token="fake-listen-ticket", expires_at=NOW)


async def _post(app, *, headers=None, **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as client:
        return await client.post("/listen", headers={"Origin": BASE_URL} if headers is None else headers, **kwargs)


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("with_port", [False, True])
async def test_u92_post_listen_returns_ticket(app, unused_tcp_port, scheme, with_port):
    # Missing forwarding, missing task_id normalization, or broken :* matching must fail here.
    app.state.parts.calls._sessions["call-listen"] = _session(listen=_ticket)
    host = urlsplit(BASE_URL).netloc
    if with_port:
        host = f"{host}:{unused_tcp_port}"
    origin = urlunsplit((scheme, host, "", "", ""))
    response = await _post(app, headers={"Host": host, "Origin": origin}, json={"task_id": "task-listen"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"task_id", "call_id", "ws_path", "token", "expires_at"}
    assert body["task_id"] == "task-listen"
    assert body["call_id"] == "call-listen"
    assert body["ws_path"] == "/ws/listen/" + body["call_id"]
    assert body["token"] == "fake-listen-ticket"
    assert body["expires_at"] == NOW.isoformat().replace("+00:00", "Z")


async def test_u92_post_listen_unknown_task_404(app):
    response = await _post(app, json={"task_id": "unknown-task"})
    assert response.status_code == 404
    assert models.ApiError.model_validate(response.json()).code == "unknown_task"


def _unavailable():
    raise ToolFailure("listen_unavailable")


@pytest.mark.parametrize("session", [None, _session(), _session(ended=True, listen=_ticket),
                                     _session(listen=_unavailable)])
async def test_u92_post_listen_text_session_409(app, session):
    if session is not None:
        app.state.parts.calls._sessions["call-listen"] = session
    response = await _post(app, json={"task_id": "task-listen"})
    assert response.status_code == 409
    assert models.ApiError.model_validate(response.json()).code == "listen_unavailable"


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.example"},
                                     {"Origin": BASE_URL, "Host": "unlisted.example"}])
async def test_u92_post_listen_origin_checked(app, headers):
    response = await _post(app, headers=headers, json={"task_id": "task-listen"})
    assert response.status_code == 403
    assert models.ApiError.model_validate(response.json()).code == "forbidden_origin"


@pytest.mark.parametrize("payload", [
    {"content": "not-json", "headers": {"Origin": BASE_URL, "Content-Type": "application/json"}},
    {"content": '{"task_id":"task-listen"}', "headers": {"Origin": BASE_URL, "Content-Type": "text/plain"}},
    {"json": {}}, {"json": []}, {"json": {"task_id": 123}},
])
async def test_u92_post_listen_requires_json_task_id(app, payload):
    response = await _post(app, **payload)
    assert 400 <= response.status_code < 500
    models.ApiError.model_validate(response.json())


async def test_u92_ws_listen_route_only_in_live_sim(tmp_path):
    for mode in ("text-sim", "live-sim"):
        application, _, store = build_app(tmp_path / mode, mode=mode)
        try:
            paths = {route.path for route in application.routes}
            assert ("/ws/listen/{call_id}" in paths) == (mode == "live-sim")
        finally:
            store.close()


async def test_u92_mcp_tools_unchanged(app):
    async with mcp_session(app) as client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == set(models.TOOL_NAMES)
