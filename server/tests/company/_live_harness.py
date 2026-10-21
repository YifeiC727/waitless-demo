"""B 线测试用的公司进程测试台（第 3 步写，冻结）。只在测试里用。

把 create_company_app(settings) 放进本测试事件循环里的 uvicorn（只绑回环、端口 0 由系统分配），
再起一个假的引擎 /ws/line（websockets 服务端，同样回环、端口 0），运营商回连时记录收到的每条消息。
Polly 用 tests/fakes 的 FakePolly：在 boto3 的 Session.client 处替换（所有 boto3.client(...) 都经过它），
服务名不是 polly 的客户端返回一个一用就报错的对象（默认测试不出网，AC-31 只允许 polly / bedrock-runtime）。

主机与端口取自 conftest 的 mode_env("live-sim", company=True)（测试值：WAITLESS_COMPANY_HOST 是回环、PORT 是 0；A-13），
不读任何密钥。
这些测试按真实时间送 20 ms 音频帧，所以用 asyncio.sleep 轮询（tech §12 允许的「音频实时送帧类」例外）。
"""

import asyncio
import base64
import contextlib
import json
import socket
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr, TypeAdapter

from waitless.call.line_protocol import (
    MULAW_FRAME_BYTES,
    ConnectedMsg,
    MarkMsg,
    MediaMsg,
    StartMsg,
    StopMsg,
    ToEngineMessage,
)
from waitless.config import Settings  # A-27 定稿：Settings 在 config.py
from waitless.models import EngineConfig

PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "WS_PROXY", "WSS_PROXY")
_to_engine = TypeAdapter(ToEngineMessage)


def no_proxy(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    """回环连接不许走代理（本机若设了代理变量，httpx / websockets 会读取）。"""
    for name in PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", f"{host},localhost")
    monkeypatch.setenv("no_proxy", f"{host},localhost")


def loopback_host(env: Mapping[str, str]) -> str:
    """env 来自 mode_env("live-sim", company=True)。"""
    return env["WAITLESS_COMPANY_HOST"]


def company_settings(env: Mapping[str, str], engine: EngineConfig, company_dir: Path, tmp_path: Path, **over: Any) -> Settings:
    """`waitless company` 用的 Settings（全是 conftest mode_env 的测试值）；company_dir 可指向 tmp 副本。

    env 要用 mode_env("live-sim", company=True)，公司进程的监听地址取其中的 WAITLESS_COMPANY_HOST/PORT。
    """
    data: dict[str, Any] = {
        "mode": "live-sim",
        "host": env["WAITLESS_HOST"],
        "port": int(env["WAITLESS_PORT"]),
        "allowed_hosts": tuple(h for h in env["WAITLESS_ALLOWED_HOSTS"].split(",") if h),
        "db_path": tmp_path / "waitless.db",
        "web_dir": Path(env["WAITLESS_WEB_DIR"]),
        "skills_dir": Path(env["WAITLESS_SKILLS_DIR"]),
        "company_dir": company_dir,
        "engine_config_path": Path(env["WAITLESS_ENGINE_CONFIG"]),
        "carrier_url": env["WAITLESS_CARRIER_URL"],
        "line_ws_url": env["WAITLESS_LINE_WS_URL"],
        "company_host": env["WAITLESS_COMPANY_HOST"],
        "company_port": int(env["WAITLESS_COMPANY_PORT"]),
        "aws_region": env["AWS_REGION"],
        "aws_access_key_id": SecretStr(env["AWS_ACCESS_KEY_ID"]),
        "aws_secret_access_key": SecretStr(env["AWS_SECRET_ACCESS_KEY"]),
        "sonic_model_id": env["WAITLESS_SONIC_MODEL_ID"],
        "sonic_voice": env["WAITLESS_SONIC_VOICE"],
        "planner_model_id": env["WAITLESS_PLANNER_MODEL_ID"],
        "jev_url": env["WAITLESS_JEV_URL"],
        "jev_model": env["WAITLESS_JEV_MODEL"],
        "jev_api_key": SecretStr(env["WAITLESS_JEV_API_KEY"]),
        "engine": engine,
    }
    data.update(over)
    return Settings(**data)


# ---------------------------------------------------------------------------
# boto3 → FakePolly
# ---------------------------------------------------------------------------


class _OfflineClient:
    def __init__(self, service: str) -> None:
        self._service = service

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"default tests must not use boto3 client {self._service!r}.{name}")


@dataclass
class PollyPatch:
    polly: Any
    services: list[str] = field(default_factory=list)


def patch_boto3_polly(monkeypatch: pytest.MonkeyPatch, polly: Any) -> PollyPatch:
    import boto3.session

    rec = PollyPatch(polly=polly)

    def fake_client(self: Any, service_name: str, *args: Any, **kwargs: Any) -> Any:
        rec.services.append(service_name)
        if service_name == "polly":
            return polly
        return _OfflineClient(service_name)

    monkeypatch.setattr(boto3.session.Session, "client", fake_client)
    return rec


# ---------------------------------------------------------------------------
# 等待
# ---------------------------------------------------------------------------


async def wait_until(pred: Callable[[], bool], timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# 公司进程（uvicorn，回环，端口 0）
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def running_company(app: Any, host: str) -> AsyncIterator[str]:
    import uvicorn

    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="auto"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await wait_until(lambda: server.started or task.done(), 15, "company app to start")
        if task.done():
            task.result()  # 启动失败时把异常抛出来
        netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        yield f"http://{netloc}"
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 15)
        sock.close()


# ---------------------------------------------------------------------------
# 假引擎 /ws/line
# ---------------------------------------------------------------------------


@dataclass
class LineConn:
    path: str
    query: dict[str, list[str]]
    ws: Any
    opened_at: float
    messages: list[tuple[float, dict[str, Any]]] = field(default_factory=list)
    raw_errors: list[str] = field(default_factory=list)
    closed_at: float | None = None
    close_code: int | None = None

    def events(self) -> list[str]:
        return [m.get("event", "?") for _, m in self.messages]

    def first(self, event: str) -> tuple[float, dict[str, Any]] | None:
        for t, m in self.messages:
            if m.get("event") == event:
                return t, m
        return None

    def media_payloads(self) -> list[bytes]:
        return [base64.b64decode(m["media"]["payload"]) for _, m in self.messages if m.get("event") == "media"]

    @property
    def stream_sid(self) -> str | None:
        got = self.first("start")
        return got[1]["streamSid"] if got else None

    async def send(self, msg: Mapping[str, Any]) -> None:
        await self.ws.send(json.dumps(msg))


class FakeEngineLine:
    """引擎一侧的 /ws/line：只收不发（需要时测试经 conn.send 发 to_carrier 消息）。"""

    def __init__(self, host: str, path: str = "/ws/line") -> None:
        self.host = host
        self.path = path
        self.conns: list[LineConn] = []
        self._server: Any = None
        self.url = ""

    async def __aenter__(self) -> "FakeEngineLine":
        from websockets.asyncio.server import serve

        self._server = await serve(self._handler, self.host, 0)
        port = self._server.sockets[0].getsockname()[1]
        netloc = f"[{self.host}]:{port}" if ":" in self.host else f"{self.host}:{port}"
        self.url = f"ws://{netloc}{self.path}"
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), 5)

    async def _handler(self, ws: Any) -> None:
        loop = asyncio.get_running_loop()
        parts = urlsplit(ws.request.path)
        conn = LineConn(path=parts.path, query=parse_qs(parts.query), ws=ws, opened_at=loop.time())
        self.conns.append(conn)
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    conn.raw_errors.append("binary frame on /ws/line")
                    continue
                try:
                    conn.messages.append((loop.time(), json.loads(raw)))
                except json.JSONDecodeError:
                    conn.raw_errors.append(f"not JSON: {raw[:80]!r}")
        except Exception:  # noqa: BLE001 — 对端异常断开也算「关闭」，记下来即可
            pass
        conn.closed_at = loop.time()
        conn.close_code = ws.close_code


def check_to_engine(msg: Mapping[str, Any]) -> Any:
    """按 §6.3 to_engine 方向校验一条运营商消息（不依赖 D 线的 parse_line_message）。"""
    model = _to_engine.validate_python(msg)
    if isinstance(model, MediaMsg):
        assert model.sequenceNumber is not None, "to_engine media needs sequenceNumber"
        assert model.media.track == "inbound" and model.media.chunk is not None and model.media.timestamp is not None, (
            "to_engine media needs media.track/chunk/timestamp"
        )
    if isinstance(model, MarkMsg):
        assert model.sequenceNumber is not None, "to_engine mark needs sequenceNumber"
    return model


def dial_body(*, call_id: str, task_id: str, to_number: str, stream_url: str, stream_token: str) -> dict[str, str]:
    return {
        "call_id": call_id,
        "task_id": task_id,
        "to_number": to_number,
        "stream_url": stream_url,
        "stream_token": stream_token,
    }


__all__ = [
    "MULAW_FRAME_BYTES",
    "ConnectedMsg",
    "FakeEngineLine",
    "LineConn",
    "PollyPatch",
    "StartMsg",
    "StopMsg",
    "check_to_engine",
    "company_settings",
    "dial_body",
    "loopback_host",
    "no_proxy",
    "patch_boto3_polly",
    "running_company",
    "wait_until",
]
