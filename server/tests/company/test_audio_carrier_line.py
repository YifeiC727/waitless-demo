"""B 线 · 音频部分（10/15）：运营商在电话线上的行为（tech §6.3；AC-83 线路侧、AC-79 线路侧、AC-61 的 B 侧、K18）。

create_company_app(settings) 跑在本测试事件循环里的 uvicorn 上（回环、端口 0）；假引擎 /ws/line 记录运营商发来的
每条消息。经 HTTP 拨号（POST /carrier/calls），然后只从线路上观察：connected → start → 20 ms μ-law media … → stop，
以及连接是否被运营商关闭。Polly 换成 FakePolly（_live_harness.patch_boto3_polly）。
菜单超时要等真实时间，所以把 company.yaml 复制到 tmp_path、把 timeout_sec 改成 1 秒（音频实时类例外，tech §12）。
"""

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from waitless.call.line_protocol import MULAW_FRAME_BYTES, SIM_ACCOUNT_SID, StartMsg, StopMsg
from waitless.company.app import create_company_app, run_company

from ._live_harness import (
    FakeEngineLine,
    LineConn,
    check_to_engine,
    company_settings,
    dial_body,
    loopback_host,
    no_proxy,
    patch_boto3_polly,
    running_company,
    wait_until,
)
from ._scenario_data import copy_company_root, edit_company_yaml, model_company
from ._signals import pipecat_dtmf_pcm, silence, ulaw_encode

LINE_NUMBER = "(312) 555-0100"
QUIET = {0xFF, 0x7F, 0xFE, 0x7E}  # μ-law 的零附近


@pytest.fixture
def fast_polly(monkeypatch: pytest.MonkeyPatch) -> Any:
    from fakes import FakePolly

    polly = FakePolly(ms_per_char=2, min_ms=40)  # 提示音短一些，测试跑得快
    patch_boto3_polly(monkeypatch, polly)
    return polly


@pytest.fixture
def host(mode_env, monkeypatch: pytest.MonkeyPatch) -> str:  # type: ignore[no-untyped-def]
    h = loopback_host(mode_env("live-sim", company=True))
    no_proxy(monkeypatch, h)
    return h


def quick_company_dir(company_dir: Path, tmp_path: Path) -> Path:
    root = copy_company_root(company_dir, tmp_path)
    edit_company_yaml(root, lambda d: d["menu"].update(timeout_sec=1, max_timeouts=2))
    return root


def settings_for(mode_env, engine_config, company_root: Path, tmp_path: Path):  # type: ignore[no-untyped-def]
    return company_settings(mode_env("live-sim", company=True), engine_config, company_root, tmp_path)


async def place_call(base: str, engine: FakeEngineLine, *, tag: str) -> tuple[str, LineConn]:
    async with httpx.AsyncClient(base_url=base, trust_env=False) as http:
        r = await http.post(
            "/carrier/calls",
            json=dial_body(
                call_id=f"call-{tag}", task_id=f"task-{tag}", to_number=LINE_NUMBER, stream_url=engine.url, stream_token=f"tok-{tag}"
            ),
        )
    assert r.status_code == 200, r.text
    sid = r.json()["call_sid"]
    await wait_until(lambda: bool(engine.conns) and engine.conns[-1].first("start") is not None, 10, "the carrier to send start")
    return sid, engine.conns[-1]


def nonsilent(payload: bytes) -> bool:
    return sum(1 for b in payload if b not in QUIET) > len(payload) // 4


async def wait_prompt_finished(conn: LineConn, *, quiet_s: float = 0.8, timeout_s: float = 15, after: float | None = None) -> None:
    """等运营商放完一段提示音：收到过有声帧（after 给出时须晚于它），且之后 quiet_s 秒内没有新的有声帧。"""

    def last_voiced() -> float | None:
        ts = [
            t
            for t, m in conn.messages
            if m.get("event") == "media" and (after is None or t > after) and nonsilent(base64.b64decode(m["media"]["payload"]))
        ]
        return ts[-1] if ts else None

    loop = asyncio.get_running_loop()
    deadline = time.monotonic() + timeout_s
    while True:
        lv = last_voiced()
        if lv is not None and loop.time() - lv >= quiet_s:
            return
        if time.monotonic() > deadline:
            raise AssertionError("the carrier never played a prompt and went quiet")
        await asyncio.sleep(0.02)


async def send_keys(conn: LineConn, keys: str) -> None:
    """像引擎一样在线路上按键：Pipecat 的按键音 → μ-law → 每 20 ms 一条 to_carrier media，后面补 200 ms 静音。"""
    from websockets.exceptions import ConnectionClosed

    ulaw = ulaw_encode(await pipecat_dtmf_pcm(keys) + silence(200))
    sid = conn.stream_sid
    for i in range(0, len(ulaw), MULAW_FRAME_BYTES):
        if conn.closed_at is not None:
            return  # 公司方已经挂断（例如按到 dead_end），剩下的帧不用再送
        frame = ulaw[i : i + MULAW_FRAME_BYTES]
        try:
            await conn.send({"event": "media", "streamSid": sid, "media": {"payload": base64.b64encode(frame).decode("ascii")}})
        except ConnectionClosed:
            return
        await asyncio.sleep(0.02)


def assert_stopped_and_closed(conn: LineConn, call_sid: str) -> tuple[float, dict[str, Any]]:
    stop = conn.first("stop")
    assert stop is not None, f"the carrier closed without a stop message; events={conn.events()[-5:]}"
    assert conn.events()[-1] == "stop", "nothing may follow stop"
    msg = StopMsg.model_validate(stop[1])
    assert msg.stop.callSid == call_sid
    assert msg.streamSid == conn.stream_sid
    assert conn.closed_at is not None
    return stop


# ---------------------------------------------------------------------------
# AC-83：菜单连续两次超时 → 发 stop 并关闭连接
# ---------------------------------------------------------------------------


async def test_ac83_menu_timeout_carrier_sends_stop_and_closes(
    host, fast_polly, mode_env, engine_config, company_dir, tmp_path  # type: ignore[no-untyped-def]
) -> None:
    root = quick_company_dir(company_dir, tmp_path)
    app = create_company_app(settings_for(mode_env, engine_config, root, tmp_path))
    async with FakeEngineLine(host) as engine, running_company(app, host) as base:
        sid, conn = await place_call(base, engine, tag="ac83")
        t_start = conn.first("start")[0]  # type: ignore[index]
        await wait_until(lambda: conn.closed_at is not None, 30, "the carrier to hang up after two menu timeouts")
    stop_t, _ = assert_stopped_and_closed(conn, sid)
    assert stop_t - t_start >= 2 * 1 - 0.05, "hung up before two 1-second menu timeouts could have passed"
    assert stop_t - t_start <= 2 * 1 + 6, "the menu timeout must come from company.yaml (timeout_sec=1 here)"
    assert any(nonsilent(p) for p in conn.media_payloads()), "the greeting and menu were never played"


async def test_ac83_hangup_api_sends_stop_and_closes(
    host, fast_polly, mode_env, engine_config, company_dir, tmp_path  # type: ignore[no-untyped-def]
) -> None:
    app = create_company_app(settings_for(mode_env, engine_config, company_dir, tmp_path))
    async with FakeEngineLine(host) as engine, running_company(app, host) as base:
        sid, conn = await place_call(base, engine, tag="hang")
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        async with httpx.AsyncClient(base_url=base, trust_env=False) as http:
            r = await http.post(f"/carrier/calls/{sid}/hangup")
        assert r.status_code == 200 and r.json() == {"call_sid": sid, "status": "completed"}
        await wait_until(lambda: conn.closed_at is not None, 5, "the carrier to close the line after hangup")
    stop_t, _ = assert_stopped_and_closed(conn, sid)
    assert stop_t - t0 < 5


# ---------------------------------------------------------------------------
# AC-79 线路侧：引擎在线路上按键，模拟公司的 Goertzel 识别后交给场景（按 2 → dead_end → 挂断，A-17）
# ---------------------------------------------------------------------------


async def test_ac79_carrier_recognises_keypad_tones_on_the_line(
    host, fast_polly, mode_env, engine_config, company_dir, tmp_path  # type: ignore[no-untyped-def]
) -> None:
    company = model_company(company_dir)
    dead_end_text = company.dead_end_text
    app = create_company_app(settings_for(mode_env, engine_config, company_dir, tmp_path))
    async with FakeEngineLine(host) as engine, running_company(app, host) as base:
        sid, conn = await place_call(base, engine, tag="keys")
        await wait_prompt_finished(conn)  # 开场白与主菜单放完（timeout_sec=8，离第一次超时还早）
        loop = asyncio.get_running_loop()
        await send_keys(conn, "2")
        t_sent = loop.time()
        await wait_until(lambda: conn.closed_at is not None, 6, "the company to take key 2 (dead_end) and hang up")
    stop_t, _ = assert_stopped_and_closed(conn, sid)
    assert stop_t - t_sent < 6
    assert any(dead_end_text in c.get("Text", "") for c in fast_polly.calls), "dead_end_text must be spoken before the hangup"


# ---------------------------------------------------------------------------
# AC-61 的 B 侧：运营商发出的消息合乎 §6.3（模型冻结在 line_protocol.py）
# ---------------------------------------------------------------------------


async def test_ac61_carrier_line_messages_follow_protocol(
    host, fast_polly, mode_env, engine_config, company_dir, tmp_path  # type: ignore[no-untyped-def]
) -> None:
    app = create_company_app(settings_for(mode_env, engine_config, company_dir, tmp_path))
    async with FakeEngineLine(host) as engine, running_company(app, host) as base:
        sid, conn = await place_call(base, engine, tag="proto")
        await conn.send({"event": "mark", "streamSid": conn.stream_sid, "mark": {"name": "engine-mark-1"}})
        await wait_until(lambda: sum(1 for e in conn.events() if e == "media") >= 10, 10, "10 media frames")
        async with httpx.AsyncClient(base_url=base, trust_env=False) as http:
            await http.post(f"/carrier/calls/{sid}/hangup")
        await wait_until(lambda: conn.closed_at is not None, 5, "close")
    assert conn.path == engine.path
    assert conn.query.get("token") == ["tok-proto"], "the carrier connects to stream_url?token=<stream_token> (§6.3)"
    assert conn.raw_errors == []
    events = conn.events()
    assert events[:2] == ["connected", "start"]
    assert "mark" not in events, "the carrier records the engine's marks but never echoes them (B-G-10)"
    models = [check_to_engine(m) for _, m in conn.messages]
    start = models[1]
    assert isinstance(start, StartMsg)
    assert start.start.callSid == sid
    assert start.start.accountSid == SIM_ACCOUNT_SID
    assert start.start.customParameters.model_dump() == {"call_id": "call-proto", "task_id": "task-proto"}
    seqs = [int(m["sequenceNumber"]) for _, m in conn.messages if "sequenceNumber" in m]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "sequenceNumber strictly increases"
    assert {m["streamSid"] for _, m in conn.messages if "streamSid" in m} == {start.streamSid}
    media = [m for _, m in conn.messages if m["event"] == "media"]
    chunks = [int(m["media"]["chunk"]) for m in media]
    assert chunks == sorted(chunks) and len(set(chunks)) == len(chunks)
    sizes = [len(base64.b64decode(m["media"]["payload"])) for m in media]
    assert all(0 < s <= MULAW_FRAME_BYTES for s in sizes), "a media frame carries at most 20 ms of μ-law (160 bytes)"
    assert sizes.count(MULAW_FRAME_BYTES) >= 0.8 * len(sizes), "frames are 20 ms each; only a prompt's last frame may be shorter"


async def test_ac95_carrier_ivr_prompts_use_company_polly_voice(
    host, fast_polly, mode_env, engine_config, company_dir, tmp_path  # type: ignore[no-untyped-def]
) -> None:
    company = model_company(company_dir)
    app = create_company_app(settings_for(mode_env, engine_config, company_dir, tmp_path))
    async with FakeEngineLine(host) as engine, running_company(app, host) as base:
        sid, conn = await place_call(base, engine, tag="voice")
        await wait_until(lambda: len(fast_polly.calls) > 0 and any(e == "media" for e in conn.events()), 10, "a prompt")
        async with httpx.AsyncClient(base_url=base, trust_env=False) as http:
            await http.post(f"/carrier/calls/{sid}/hangup")
        await wait_until(lambda: conn.closed_at is not None, 5, "close")
    assert fast_polly.calls
    for c in fast_polly.calls:
        assert c["VoiceId"] == company.voices.ivr_polly
        assert c["Engine"] == "neural" and c["OutputFormat"] == "pcm"
        assert c["SampleRate"] == str(engine_config.audio.wire_sample_rate)


# ---------------------------------------------------------------------------
# 统计文件（X-G6 定稿 stats_jsonl，暂定；AC-98 真机检查单读它）
# ---------------------------------------------------------------------------


async def test_ac98_company_stats_line_per_finished_call(
    host, fast_polly, mode_env, engine_config, company_dir, tmp_path  # type: ignore[no-untyped-def]
) -> None:
    stats = tmp_path / "company-stats.jsonl"
    settings = company_settings(mode_env("live-sim", company=True), engine_config, company_dir, tmp_path, company_stats_path=stats)
    app = create_company_app(settings)
    async with FakeEngineLine(host) as engine, running_company(app, host) as base:
        sid, conn = await place_call(base, engine, tag="stats")
        async with httpx.AsyncClient(base_url=base, trust_env=False) as http:
            await http.post(f"/carrier/calls/{sid}/hangup")
        await wait_until(lambda: conn.closed_at is not None, 5, "close")
        await wait_until(lambda: stats.is_file() and stats.read_text(encoding="utf-8").strip() != "", 5, "the stats line")
    rows = [json.loads(line) for line in stats.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows == [{"call_id": "call-stats", "call_sid": sid, "nonsilent_inbound_frames_during_handoff": 0}], "one JSON line per call; no handoff → 0"


async def test_ac98_company_stats_not_written_when_unset(
    host, fast_polly, mode_env, engine_config, company_dir, tmp_path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    app = create_company_app(settings_for(mode_env, engine_config, company_dir, tmp_path))
    async with FakeEngineLine(host) as engine, running_company(app, host) as base:
        sid, conn = await place_call(base, engine, tag="nostats")
        async with httpx.AsyncClient(base_url=base, trust_env=False) as http:
            await http.post(f"/carrier/calls/{sid}/hangup")
        await wait_until(lambda: conn.closed_at is not None, 5, "close")
        await asyncio.sleep(0.2)
    assert [p for p in tmp_path.rglob("*") if p != cwd] == [], "without WAITLESS_COMPANY_STATS nothing is written"


# ---------------------------------------------------------------------------
# K18、A-13：公司进程只听回环；COMPANY_HOST/PORT 缺项时退出码 2
# ---------------------------------------------------------------------------


@pytest.fixture
def uvicorn_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    import uvicorn

    calls: list[dict[str, Any]] = []

    def fake_run(app: Any, **kw: Any) -> None:
        calls.append(kw)

    def fake_server_run(self: Any, *a: Any, **kw: Any) -> None:
        calls.append({"host": self.config.host, "port": self.config.port})

    async def fake_server_serve(self: Any, *a: Any, **kw: Any) -> None:
        calls.append({"host": self.config.host, "port": self.config.port})

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(uvicorn.Server, "run", fake_server_run)
    monkeypatch.setattr(uvicorn.Server, "serve", fake_server_serve)
    return calls


def test_ac84_company_process_listens_on_loopback_only(
    uvicorn_calls, fast_polly, mode_env, engine_config, company_dir, tmp_path  # type: ignore[no-untyped-def]
) -> None:
    ok = settings_for(mode_env, engine_config, company_dir, tmp_path)
    for bad_host in ("0.0.0.0", "::"):
        rc = run_company(ok.model_copy(update={"company_host": bad_host}))
        assert isinstance(rc, int) and rc != 0, f"must refuse to listen on {bad_host}"
    assert uvicorn_calls == []
    rc = run_company(ok)
    assert rc == 0
    assert uvicorn_calls == [{**uvicorn_calls[0], "host": ok.company_host, "port": ok.company_port}]


@pytest.mark.parametrize("missing", ["company_host", "company_port"])
def test_ac84_company_process_missing_address_exits_2(
    uvicorn_calls, fast_polly, mode_env, engine_config, company_dir, tmp_path, missing: str  # type: ignore[no-untyped-def]
) -> None:
    ok = settings_for(mode_env, engine_config, company_dir, tmp_path)
    assert run_company(ok.model_copy(update={missing: None})) == 2  # A-13 定稿
    assert uvicorn_calls == []
