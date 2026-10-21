"""B 线 · 文字部分（10/14 晚前）：模拟运营商 HTTP API（tech §6.3；AC-84；A-10、A-11、B-G-3、B-G-4 定稿）。

经完整的公司进程 create_company_app(settings) 调用（回环、端口 0，_live_harness.running_company）：
未知场景按「该公司 list_scenarios() 里有没有这份文件」判断（models.CARRIER_ERROR_CODES 上方的说明），
所以要用带 company_dir 的完整应用；CarrierState 怎样拿到 company_dir 由 B 自定（B-G-1）。
company/ 复制到 tmp_path，再把 baseline 复制成第二个场景文件 menu-reworded，这样本组只依赖 baseline 的数据。

place_call 按 §7 契约「建 CarrierCall 并在后台连接引擎」：这里把模块里的公开名字 CarrierCall 换成一个只记录构造参数的
替身（契约签名：dial、call_sid、scenario_id、company），从外面看到「下一通电话用的是哪个场景」，不必真的去连引擎
（真连接见 test_audio_carrier_line.py）。
"""

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from waitless.company import carrier as carrier_mod
from waitless.company.app import create_company_app
from waitless.models import CARRIER_ERROR_CODES, CarrierDialIn

from ._live_harness import company_settings, loopback_host, no_proxy, patch_boto3_polly, running_company
from ._scenario_data import COMPANY_ID, copy_company_root, read_yaml, write_yaml

LINE_NUMBER = "(312) 555-0100"
OTHER_LINE = "(312) 555-0199"
SID_RE = re.compile(r"^CA[0-9a-fA-F]{32}$")


class RecordingCall:
    """CarrierCall 的替身：记下构造参数，run() / hangup() 只做标记。"""

    instances: list["RecordingCall"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.ran = False
        self.hung_up = False
        self.hangup_sources = []
        RecordingCall.instances.append(self)

    async def run(self) -> None:
        self.ran = True

    async def hangup(self, *, source: str = 'internal') -> None:
        self.hangup_sources.append(source)
        self.hung_up = True

    @property
    def nonsilent_inbound_frames_during_handoff(self) -> int:
        return 0


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[RecordingCall]:
    RecordingCall.instances = []
    monkeypatch.setattr(carrier_mod, "CarrierCall", RecordingCall)
    return RecordingCall.instances


def company_copy_with(company_dir: Path, tmp_path: Path, *scenario_ids: str) -> Path:
    """company/ 的 tmp 副本；scenarios/ 里只留 baseline，另把 baseline 复制成 scenario_ids 这几个文件。"""
    root = copy_company_root(company_dir, tmp_path)
    scen = root / COMPANY_ID / "scenarios"
    base = read_yaml(scen / "baseline.yaml")
    for p in scen.glob("*.yaml"):
        if p.stem != "baseline":
            p.unlink()
    for sid in scenario_ids:
        write_yaml(scen / f"{sid}.yaml", {**base, "scenario_id": sid})
    return root


@contextlib.asynccontextmanager
async def carrier_http(
    root: Path, tmp_path: Path, mode_env: Any, engine_config: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    from fakes import FakePolly

    patch_boto3_polly(monkeypatch, FakePolly())
    env = mode_env("live-sim", company=True)
    host = loopback_host(env)
    no_proxy(monkeypatch, host)
    app = create_company_app(company_settings(env, engine_config, root, tmp_path))
    async with running_company(app, host) as base, httpx.AsyncClient(base_url=base, trust_env=False) as http:
        yield http


def dial(**over: str) -> dict[str, str]:
    body = {
        "call_id": "call-1",
        "task_id": "task-1",
        "to_number": LINE_NUMBER,
        "stream_url": "ws://engine.invalid/ws/line",  # 替身不会去连；真实连接见音频组
        "stream_token": "tok-1",
    }
    body.update(over)
    return body


async def settle(calls: list[RecordingCall], n: int) -> None:
    """等 place_call 放进后台的任务跑起来（同一事件循环，最多约 1 s）。"""
    for _ in range(100):
        if len(calls) >= n and all(c.ran for c in calls[:n]):
            return
        await asyncio.sleep(0.01)


def error_code(r: httpx.Response) -> str | None:
    try:
        return r.json().get("code")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# AC-84：PUT /sim/scenario 之后，下一通电话走该场景
# ---------------------------------------------------------------------------


async def test_ac84_scenario_switch_default_is_baseline(company_dir, tmp_path, mode_env, engine_config, monkeypatch, calls) -> None:  # type: ignore[no-untyped-def]
    root = company_copy_with(company_dir, tmp_path, "menu-reworded")
    async with carrier_http(root, tmp_path, mode_env, engine_config, monkeypatch) as http:
        r = await http.get("/sim/scenario", params={"line_number": LINE_NUMBER})
    assert r.status_code == 200
    assert r.json() == {"line_number": LINE_NUMBER, "scenario_id": "baseline"}  # A-11 定稿：默认 baseline


async def test_ac84_scenario_switch_next_call_uses_selected_scenario(company_dir, tmp_path, mode_env, engine_config, monkeypatch, calls) -> None:  # type: ignore[no-untyped-def]
    root = company_copy_with(company_dir, tmp_path, "menu-reworded")
    async with carrier_http(root, tmp_path, mode_env, engine_config, monkeypatch) as http:
        put = await http.put("/sim/scenario", json={"line_number": LINE_NUMBER, "scenario_id": "menu-reworded"})
        assert put.status_code == 200
        assert put.json() == {"line_number": LINE_NUMBER, "scenario_id": "menu-reworded"}
        got = await http.get("/sim/scenario", params={"line_number": LINE_NUMBER})
        assert got.json() == {"line_number": LINE_NUMBER, "scenario_id": "menu-reworded"}

        first = await http.post("/carrier/calls", json=dial(call_id="call-a"))
        assert first.status_code == 200
        body = first.json()
        assert body["status"] == "queued" and SID_RE.match(body["call_sid"])
        await settle(calls, 1)
        assert len(calls) == 1
        call_a = calls[0]
        assert call_a.kwargs["scenario_id"] == "menu-reworded"
        assert call_a.kwargs["call_sid"] == body["call_sid"]
        assert call_a.kwargs["dial"] == CarrierDialIn(**dial(call_id="call-a"))
        assert call_a.kwargs["company"].company_id == COMPANY_ID
        assert call_a.ran, "place_call must start the call in the background"

        await http.put("/sim/scenario", json={"line_number": LINE_NUMBER, "scenario_id": "baseline"})
        second = await http.post("/carrier/calls", json=dial(call_id="call-b"))
        await settle(calls, 2)
    assert second.status_code == 200
    assert len(calls) == 2
    assert calls[1].kwargs["scenario_id"] == "baseline"
    assert call_a.kwargs["scenario_id"] == "menu-reworded", "switching must not touch a call already placed"
    assert second.json()["call_sid"] != body["call_sid"]


async def test_ac84_scenario_switch_rejects_unknown_scenario(company_dir, tmp_path, mode_env, engine_config, monkeypatch, calls) -> None:  # type: ignore[no-untyped-def]
    # B-G-3 定稿：不在该公司 list_scenarios() 里 → 404 unknown_scenario，当前选择不变
    root = company_copy_with(company_dir, tmp_path, "menu-reworded")
    async with carrier_http(root, tmp_path, mode_env, engine_config, monkeypatch) as http:
        await http.put("/sim/scenario", json={"line_number": LINE_NUMBER, "scenario_id": "menu-reworded"})
        bad = await http.put("/sim/scenario", json={"line_number": LINE_NUMBER, "scenario_id": "no-such-scenario"})
        got = await http.get("/sim/scenario", params={"line_number": LINE_NUMBER})
    assert bad.status_code == 404 and error_code(bad) == "unknown_scenario"
    assert got.json()["scenario_id"] == "menu-reworded"


async def test_ac84_scenario_switch_rejects_unknown_line(company_dir, tmp_path, mode_env, engine_config, monkeypatch, calls) -> None:  # type: ignore[no-untyped-def]
    # B-G-3 定稿：线路未知 → PUT 与 GET 都是 404 unknown_line，已知线路的选择不变
    root = company_copy_with(company_dir, tmp_path, "menu-reworded")
    async with carrier_http(root, tmp_path, mode_env, engine_config, monkeypatch) as http:
        await http.put("/sim/scenario", json={"line_number": LINE_NUMBER, "scenario_id": "menu-reworded"})
        put = await http.put("/sim/scenario", json={"line_number": OTHER_LINE, "scenario_id": "baseline"})
        get_other = await http.get("/sim/scenario", params={"line_number": OTHER_LINE})
        got = await http.get("/sim/scenario", params={"line_number": LINE_NUMBER})
    assert put.status_code == 404 and error_code(put) == "unknown_line"
    assert get_other.status_code == 404 and error_code(get_other) == "unknown_line"
    assert got.json()["scenario_id"] == "menu-reworded"


# ---------------------------------------------------------------------------
# 同一张 §6.3 表的其余接口（AC-84 的前提：拨号、挂断；B-G-4 定稿）
# ---------------------------------------------------------------------------


async def test_ac84_dial_unknown_number_returns_404(company_dir, tmp_path, mode_env, engine_config, monkeypatch, calls) -> None:  # type: ignore[no-untyped-def]
    root = company_copy_with(company_dir, tmp_path)
    async with carrier_http(root, tmp_path, mode_env, engine_config, monkeypatch) as http:
        r = await http.post("/carrier/calls", json=dial(to_number=OTHER_LINE))
        await asyncio.sleep(0.05)
    assert r.status_code == 404 and error_code(r) == "unknown_number"
    assert calls == []


async def test_ac84_dial_rejects_malformed_body(company_dir, tmp_path, mode_env, engine_config, monkeypatch, calls) -> None:  # type: ignore[no-untyped-def]
    body = dial()
    del body["stream_token"]
    root = company_copy_with(company_dir, tmp_path)
    async with carrier_http(root, tmp_path, mode_env, engine_config, monkeypatch) as http:
        r = await http.post("/carrier/calls", json=body)
        await asyncio.sleep(0.05)
    assert 400 <= r.status_code < 500
    assert calls == []


async def test_ac84_hangup_endpoint_completes_call(company_dir, tmp_path, mode_env, engine_config, monkeypatch, calls) -> None:  # type: ignore[no-untyped-def]
    root = company_copy_with(company_dir, tmp_path)
    async with carrier_http(root, tmp_path, mode_env, engine_config, monkeypatch) as http:
        sid = (await http.post("/carrier/calls", json=dial())).json()["call_sid"]
        await settle(calls, 1)
        r = await http.post(f"/carrier/calls/{sid}/hangup")
        unknown = await http.post(f"/carrier/calls/CA{'0' * 32}/hangup")
    assert r.status_code == 200
    assert r.json() == {"call_sid": sid, "status": "completed"}
    assert calls[0].hung_up, "the hangup endpoint must hang up that CarrierCall (stop + close, AC-83)"
    assert unknown.status_code == 404 and error_code(unknown) == "unknown_call"
    assert set(CARRIER_ERROR_CODES) == {"unknown_number", "unknown_call", "unknown_line", "unknown_scenario"}
