"""AC-94：MCP 端到端（replay 模式，官方 MCP SDK 客户端）。

第 3 步写（冻结）；tech §6.1、§6.2、§6.12、§7.1 AC-94、§8、§12。

做法：
- load_settings(mode_env("replay", WAITLESS_REPLAY_FILE="baseline.jsonl")) → create_app(settings)，用 uvicorn 在回环地址的临时端口上真正起一个 HTTP 服务；
- 用官方 SDK 的 `mcp.Client("<base>/mcp")` 按主线依次调用
  prepare_task → update_task → start_assisted_call → get_call_status → join_call → return_to_agent → get_case_result，
  最终 outcome=confirmed；
- 回放文件是 replays/baseline.jsonl（tech §7.1 AC-88：I 线用 text-sim 录制）。文件缺失时本测试失败，不跳过；
  按缺口定稿 X-G5，replays/ 里有两份录制：baseline.jsonl（MCP 调用序列，本测试用）与 baseline-web.jsonl（网页流程，
  replay 模式的默认值），所以本测试显式设 WAITLESS_REPLAY_FILE=baseline.jsonl；
- 按 X-G3，回放文件头带 request_text（录制时 prepare_task 用的原话），/app-config 的 replay_request_text 必须就是它；
- 客户档案按 X-G9（暂定）：在 company.yaml 的 customers 里按 conftest.TEST_PROFILE 的姓名找，不依赖排第几；
  账户号取 conftest.TEST_ACCOUNT（B-G-9）；
- 场景按定稿 A-11：回放文件头 ReplayHeader.scenario_id 记录录制时的场景（baseline），replay 模式强制用它；
  录制时 text-sim 缺省场景取 engine.yaml 的 textsim.default_scenario（baseline）；
- 节奏按定稿 A-20：测试不设 WAITLESS_TEXTSIM_PACE_MS，textsim.pace_ms 保持 0；
- 录制用同一套调用顺序：`test_ac94_record_baseline_replay`（needs_keys，只由 I-3b 显式选择运行），
  这样回放里 jev / converse 各通道的请求顺序与本测试一致。

断言都落在外面看得到的结果上：tools/list、每个工具的 structuredContent（按 models 里的输出模型校验）、
/app-config 的回应。等待通话推进用真实时间轮询（服务端用 SystemClock，replay 模式下 textsim.pace_ms=0），
每次间隔 POLL_INTERVAL_S，总时限 POLL_LIMIT_S，超时即失败。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from waitless.models import (
    TOOL_NAMES,
    AppConfigOut,
    CallStatusOut,
    CaseResultOut,
    CompanyConfig,
    CompanyCustomer,
    JoinCallOut,
    PrepareTaskOut,
    ReplayHeader,
    ReplayRecord,
    ReturnOut,
    StartCallOut,
    UpdateTaskOut,
)

BASELINE_REPLAY = "baseline.jsonl"  # X-G5：MCP 序列那份；网页那份是 baseline-web.jsonl（replay 模式的默认）
COMPANY_ID = "lakeshore-energy"  # tech §6.10：company/lakeshore-energy/
MAINLINE: tuple[str, ...] = (
    "prepare_task",
    "update_task",
    "start_assisted_call",
    "get_call_status",
    "join_call",
    "return_to_agent",
    "get_case_result",
)
CONFIRMATION_RE = re.compile(r"^LE-\d{5}$")  # PRD AC-88：baseline 的确认号格式
# 录制时用的用户请求（只在 text-sim 录制时使用；回放时以 /app-config 的 replay_request_text 为准）。
BASELINE_REQUEST = "Please start electric service with Lakeshore Energy at my address, effective November 1."
POLL_INTERVAL_S = 0.05
POLL_LIMIT_S = 60.0
SERVER_START_LIMIT_S = 20.0


# ---------------------------------------------------------------------------
# 服务与客户端
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def serve_app(app: Any, host: str, port: int) -> AsyncIterator[str]:
    """在 host:port（port=0 为临时端口）起 uvicorn，产出 "http://<host>:<实际端口>"，退出时关停。"""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    deadline = time.monotonic() + SERVER_START_LIMIT_S
    while not server.started:
        if task.done():
            task.result()  # 启动失败（例如 lifespan 抛错）就把异常抛出来
            raise AssertionError("uvicorn exited before it started")
        if time.monotonic() > deadline:
            server.should_exit = True
            raise AssertionError("uvicorn did not start in time")
        await asyncio.sleep(0.01)
    bound_host, bound_port = server.servers[0].sockets[0].getsockname()[:2]
    try:
        yield f"http://{bound_host}:{bound_port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=SERVER_START_LIMIT_S)


def _text_of(result: Any) -> str:
    parts = [getattr(c, "text", "") for c in (result.content or [])]
    return " ".join(p for p in parts if p)


@dataclass
class ToolCaller:
    """包住官方 SDK 的 Client：记录调用顺序，按输出模型校验 structuredContent。

    connect 为 None 时用同一个已连接的 client；否则每次调用都新建一个连接（隐藏测试用）。
    """

    client: Any = None
    connect: Callable[[], Any] | None = None
    calls: list[str] = field(default_factory=list)

    async def call(self, name: str, arguments: Mapping[str, Any], out_model: type[BaseModel]) -> Any:
        if self.connect is None:
            result = await self.client.call_tool(name, dict(arguments))
        else:
            async with self.connect() as fresh:
                result = await fresh.call_tool(name, dict(arguments))
        self.calls.append(name)
        assert not result.is_error, f"{name} returned isError=true: {_text_of(result)}"
        assert result.structured_content is not None, f"{name} returned no structuredContent"
        return out_model.model_validate(result.structured_content)


def collapse_repeats(names: list[str]) -> list[str]:
    out: list[str] = []
    for n in names:
        if not out or out[-1] != n:
            out.append(n)
    return out


@dataclass
class MainlineRun:
    prepare: PrepareTaskOut
    update: UpdateTaskOut
    start: StartCallOut
    statuses: list[CallStatusOut]
    join: JoinCallOut
    returned: ReturnOut
    results: list[CaseResultOut]
    calls: list[str]

    @property
    def final(self) -> CaseResultOut:
        return self.results[-1]


def load_lakeshore_customer(
    company_dir: Path, profile: Mapping[str, str], accounts: Mapping[str, str]
) -> CompanyCustomer:
    """测试身份（conftest.TEST_PROFILE、TEST_ACCOUNT）在模拟公司里的客户档案，用来补齐任务卡缺的可代说字段与账户号。

    缺口 X-G9（定稿 match_by_value，暂定）：按姓名在 customers 里找同名客户，不依赖它排第几；
    账户号取 TEST_ACCOUNT[company_id]（B-G-9：company.yaml 照抄这两份）。
    """
    path = company_dir / COMPANY_ID / "company.yaml"
    assert path.is_file(), f"{path} is missing (B lane, tech §6.10)"
    config = CompanyConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    same_name = [c for c in config.customers if c.name == profile["name"]]
    assert len(same_name) == 1, f"company.yaml must list exactly one customer named {profile['name']!r}"
    account = accounts[COMPANY_ID]
    assert same_name[0].account_number == account, "company.yaml and conftest.TEST_ACCOUNT disagree (B-G-9)"
    return same_name[0].model_copy(update={"account_number": account})


async def drive_mainline(
    caller: ToolCaller,
    request_text: str,
    customer: Any,
    *,
    debug_status: bool = False,
    probe_result_at_handoff: bool = False,
) -> MainlineRun:
    """按 AC-94 的顺序调用 7 个工具，返回每一步的输出。任何一步不符合主线就断言失败。"""
    prepare = await caller.call("prepare_task", {"request": request_text}, PrepareTaskOut)
    assert prepare.status in ("ready", "setup_required"), f"prepare_task: {prepare.status} {prepare.message}"
    assert prepare.task_card is not None, "prepare_task returned no task card for the baseline request"
    assert prepare.task_card.company.company_id == COMPANY_ID
    task_id = prepare.task_id

    fields: dict[str, str] = {"account_number": customer.account_number}
    for item in prepare.missing:
        if item.key in ("name", "phone_number", "address"):
            fields[item.key] = getattr(customer, item.key)
    decisions = {
        q.question_id: (q.selected or q.default or q.options[0].id)
        for q in prepare.task_card.precall_decisions
        if q.options
    }
    update_args: dict[str, Any] = {"task_id": task_id, "fields": fields}
    if decisions:
        update_args["decisions"] = decisions
    update = await caller.call("update_task", update_args, UpdateTaskOut)
    assert update.task_id == task_id
    assert update.status == "ready", f"update_task left the task in {update.status}, missing={update.missing}"
    assert update.missing == []
    assert update.task_card is not None  # 定稿 A-19：task_card 可空，ready 时必须有

    start = await caller.call(
        "start_assisted_call",
        {"task_id": task_id, "confirmed": True, "card_version": update.task_card.card_version},
        StartCallOut,
    )
    assert start.task_id == task_id and start.status == "dialing"

    statuses: list[CallStatusOut] = []
    results: list[CaseResultOut] = []
    deadline = time.monotonic() + POLL_LIMIT_S
    while True:
        args: dict[str, Any] = {"task_id": task_id}
        if debug_status:
            args["debug"] = True
        st = await caller.call("get_call_status", args, CallStatusOut)
        statuses.append(st)
        assert st.call_id == start.call_id
        assert st.pending_choice is None, (
            "the baseline mainline must not raise a pending choice (deposit is decided on the task card, tech §8 step 10)"
        )
        assert not st.ended, f"call ended before the handoff (phase={st.phase}, status_line={st.status_line!r})"
        if debug_status:
            assert st.debug is not None and st.debug.mode == "replay"
        if st.handoff is not None and st.handoff.state == "ready":
            break
        assert time.monotonic() < deadline, f"no handoff within {POLL_LIMIT_S}s; last phase={st.phase}"
        await asyncio.sleep(POLL_INTERVAL_S)
    assert any(s.transcript_tail for s in statuses), "no transcript was shown before the handoff"

    if probe_result_at_handoff:
        mid = await caller.call("get_case_result", {"task_id": task_id}, CaseResultOut)
        assert mid.outcome == "in_progress", f"case result during the handoff wait: {mid.outcome}"
        results.append(mid)

    join = await caller.call("join_call", {"task_id": task_id}, JoinCallOut)
    assert join.task_id == task_id and join.call_id == start.call_id
    assert join.ws_path == f"/ws/handoff/{join.handoff_id}"
    assert (join.media.ai_media, join.media.recording, join.media.transcription) == ("disconnected", False, False)

    returned = await caller.call("return_to_agent", {"task_id": task_id, "handoff_id": join.handoff_id}, ReturnOut)
    assert returned.call_id == start.call_id and returned.status == "resuming"
    assert (returned.media.ai_media, returned.media.recording, returned.media.transcription) == (
        "connected",
        False,
        True,
    )

    deadline = time.monotonic() + POLL_LIMIT_S
    while True:
        res = await caller.call("get_case_result", {"task_id": task_id}, CaseResultOut)
        results.append(res)
        if res.outcome != "in_progress":
            break
        assert time.monotonic() < deadline, f"case still in_progress after {POLL_LIMIT_S}s"
        await asyncio.sleep(POLL_INTERVAL_S)

    return MainlineRun(prepare, update, start, statuses, join, returned, results, list(caller.calls))


def assert_confirmed(run: MainlineRun) -> None:
    final = run.final
    assert final.task_id == run.prepare.task_id
    assert final.outcome == "confirmed", f"outcome={final.outcome}, headline={final.headline!r}"
    assert final.confirmation_number is not None and CONFIRMATION_RE.fullmatch(final.confirmation_number), (
        f"confirmation number {final.confirmation_number!r} does not match {CONFIRMATION_RE.pattern}"
    )
    mine = [c for c in final.calls if c.call_id == run.start.call_id]
    assert len(mine) == 1, f"get_case_result.calls does not list call {run.start.call_id}"
    assert mine[0].outcome == "confirmed"
    assert mine[0].ended_at is not None
    assert mine[0].scenario_id == "baseline"  # 定稿 A-11：replay 用回放文件头记录的场景


async def fetch_app_config(base_url: str) -> AppConfigOut:
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.get(f"{base_url}/app-config")
    assert r.status_code == 200, r.text
    return AppConfigOut.model_validate(r.json())


async def run_mainline_against(
    settings: Any,
    customer: Any,
    *,
    request_text: str | None = None,
    expect_request_text: str | None = None,
    fresh_connection_per_call: bool = False,
    debug_status: bool = False,
    probe_result_at_handoff: bool = False,
) -> MainlineRun:
    """起服务 → tools/list 校验 → 按主线驱动。request_text 为 None 时取 /app-config 的 replay_request_text；
    expect_request_text 给出时，replay_request_text 必须等于它（X-G3：回放头的 request_text）。"""
    from mcp import Client

    from waitless.app import create_app

    app = create_app(settings)
    async with serve_app(app, settings.host, settings.port) as base:
        cfg = await fetch_app_config(base)
        assert cfg.mode == settings.mode
        if request_text is None:
            assert cfg.replay_request_text, "/app-config must give replay_request_text in replay mode (tech §6.2)"
            request_text = cfg.replay_request_text
        if expect_request_text is not None:
            assert cfg.replay_request_text == expect_request_text, (
                f"replay_request_text {cfg.replay_request_text!r} is not the replay header's request_text"
            )
        mcp_url = f"{base}/mcp"
        async with Client(mcp_url) as client:
            listed = await client.list_tools()
            assert [t.name for t in listed.tools] == list(TOOL_NAMES)
            caller = ToolCaller(client=client)
            if fresh_connection_per_call:
                caller = ToolCaller(connect=lambda: Client(mcp_url))
            return await drive_mainline(
                caller,
                request_text,
                customer,
                debug_status=debug_status,
                probe_result_at_handoff=probe_result_at_handoff,
            )


def read_replay(path: Path) -> tuple[ReplayHeader, list[ReplayRecord]]:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, f"{path} is empty"
    header = ReplayHeader.model_validate(json.loads(lines[0]))
    records = [ReplayRecord.model_validate(json.loads(ln)) for ln in lines[1:]]
    return header, records


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


def test_ac94_baseline_replay_file_present(replays_dir: Path) -> None:
    """回放文件由 I-3b 录制并入库；缺失就失败（不是跳过）。"""
    path = replays_dir / BASELINE_REPLAY
    assert path.is_file(), (
        f"{path} is missing: record it in I-3b with "
        "`scripts/load-keys-macos.sh uv run pytest -m needs_keys tests/test_e2e_mcp_replay.py::test_ac94_record_baseline_replay`"
    )
    header, records = read_replay(path)
    assert header.source_mode == "text-sim"
    assert header.scenario_id == "baseline"  # 定稿 A-11
    assert header.request_text.strip(), "X-G3: the header must carry the request text used when recording"
    channels = {r.channel for r in records}
    assert "jev" in channels, "a mainline replay must contain Jev decisions"
    for channel in channels:
        seqs = [r.seq for r in records if r.channel == channel]
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), f"{channel}: seq must increase"


async def test_ac94_mcp_mainline_replay_confirmed(
    mode_env: Callable[..., dict[str, str]],
    replays_dir: Path,
    company_dir: Path,
    test_profile: dict[str, str],
    test_account: dict[str, str],
) -> None:
    from waitless.config import load_settings

    path = replays_dir / BASELINE_REPLAY
    assert path.is_file(), f"{path} is missing (record it in I-3b)"
    header, _ = read_replay(path)
    settings = load_settings(mode_env("replay", WAITLESS_REPLAY_FILE=BASELINE_REPLAY))  # X-G5
    assert settings.mode == "replay"
    run = await run_mainline_against(
        settings,
        load_lakeshore_customer(company_dir, test_profile, test_account),
        expect_request_text=header.request_text,
    )
    assert collapse_repeats(run.calls) == list(MAINLINE)
    assert_confirmed(run)


@pytest.mark.needs_keys
async def test_ac94_record_baseline_replay(
    tmp_path: Path, company_dir: Path, test_profile: dict[str, str], test_account: dict[str, str]
) -> None:
    """I-3b 录制 replays/baseline.jsonl：text-sim、真实 Jev 与 Nova 2 Lite，调用顺序与回放测试相同。

    运行：scripts/load-keys-macos.sh env WAITLESS_E2E_REPLAY_OUT="$PWD/../replays/baseline.jsonl" \
          uv run pytest -m needs_keys tests/test_e2e_mcp_replay.py::test_ac94_record_baseline_replay
    需要 .env 里 text-sim 的全部变量（tech §5.2）；缺密钥或缺 WAITLESS_E2E_REPLAY_OUT 都失败。
    """
    from waitless.config import load_settings

    out = os.environ.get("WAITLESS_E2E_REPLAY_OUT")
    assert out, "set WAITLESS_E2E_REPLAY_OUT to the replay file to write (normally replays/baseline.jsonl)"
    record_dir = tmp_path / "record"
    record_dir.mkdir()
    env = dict(os.environ)
    env.pop("WAITLESS_TEXTSIM_PACE_MS", None)  # 定稿 A-20：录制不放慢
    env.update(
        WAITLESS_MODE="text-sim",
        WAITLESS_PORT="0",
        WAITLESS_DB_PATH=str(tmp_path / "record.db"),
        WAITLESS_RECORD_DIR=str(record_dir),
    )
    settings = load_settings(env)
    run = await run_mainline_against(
        settings, load_lakeshore_customer(company_dir, test_profile, test_account), request_text=BASELINE_REQUEST
    )
    assert collapse_repeats(run.calls) == list(MAINLINE)
    assert_confirmed(run)
    recorded = record_dir / BASELINE_REPLAY  # X-G4：录制产物名是 <scenario_id>.jsonl
    assert sorted(p.name for p in record_dir.glob("*.jsonl")) == [BASELINE_REPLAY], list(record_dir.iterdir())
    header, records = read_replay(recorded)
    assert header.source_mode == "text-sim" and records
    assert header.scenario_id == "baseline"  # 定稿 A-11：text-sim 缺省场景 textsim.default_scenario
    assert header.request_text == BASELINE_REQUEST  # X-G3
    raw = recorded.read_text(encoding="utf-8")
    assert run.prepare.task_id not in raw and run.start.call_id not in raw, "ids must be replaced by placeholders"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(recorded, out)
