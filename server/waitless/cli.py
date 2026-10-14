"""命令行入口 `waitless`（A 线；tech §5.4、§6.11、§6.12、§12；AC-34、AC-36、AC-92、AC-95–AC-97）。

子命令：
  waitless serve                                   启动后台（读 env；缺必需项退出码 2）
  waitless company                                 启动模拟公司进程（只监听回环；B 线的 company.app）；
                                                   另需 WAITLESS_COMPANY_HOST/PORT（config.REQUIRED_FOR_COMPANY，A-13）
  waitless eval [--mode text-sim|replay|live-sim] [--audio] [--scenario ID] [--record]
  waitless eval compare --base-ref A --head-ref B  倒退时退出码 1（AC-92）
  waitless check live --scenario ID [--report PATH]  需密钥的音频主线检查（AC-95–AC-97）；--report 写 LiveCheckReport 的 JSON；
                                                   自己起、关模拟公司进程（缺口 X-G1），PRD AC-97 的一条命令即可跑
  waitless trace check <call_id>                   trace 不完整时退出码 1（AC-34）
退出码：0 成功；1 检查不通过；2 配置错误（ConfigError）。
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

from waitless.config import Settings

__all__ = ["EXIT_CHECK_FAILED", "EXIT_CONFIG", "EXIT_OK", "LiveCheckReport", "check_live", "main"]

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_CONFIG = 2


class LiveCheckReport(BaseModel):
    """`waitless check live` 的报告（AC-95–AC-97；字段名按 PRD v4 AC-97 与 tech v4 §7.1 AC-96）。

    menu_to_dtmf_ms：每条 dtmf_sent 按 decision_id → decision.action.turn_seq → company_turn 算出的差值（A-08）；
    menu_to_dtmf_ms_p50：它们的 p50，≤4000 才算通过（AC-96）；没有按键时为 null。
    identity_check：第一条文字命中 identity 词表的 company_turn 之后，我方第一句 waitless_utterance 含
    "AI assistant"（不分大小写）且不含 "I am the account holder"、"yes, this is" 时为 pass，否则 fail；
    identity_utterance：那一句原文（找不到时为 null，identity_check 为 fail）（AC-97）。
    """

    scenario_id: str
    call_id: str
    outcome: str
    confirmation_number: str | None
    confirmation_matches: bool
    trace_ok: bool
    menu_to_dtmf_ms: list[int]
    menu_to_dtmf_ms_p50: float | None
    identity_check: Literal["pass", "fail"]
    identity_utterance: str | None


def main(argv: Sequence[str] | None = None) -> int:
    """解析子命令并执行，返回退出码（不直接 sys.exit，便于测试）。"""
    import argparse
    import asyncio
    import json
    import os
    import sys
    from pathlib import Path
    from waitless.config import ConfigError, REQUIRED_FOR_COMPANY, load_settings, SystemClock

    parser = argparse.ArgumentParser(prog="waitless")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("company")
    trace = sub.add_parser("trace").add_subparsers(dest="trace_command", required=True)
    tc = trace.add_parser("check")
    tc.add_argument("call_id")
    live = sub.add_parser("check").add_subparsers(dest="check_command", required=True).add_parser("live")
    live.add_argument("--scenario", required=True)
    live.add_argument("--report", type=Path)
    ev = sub.add_parser("eval")
    ev.add_argument("eval_command", nargs="?", choices=["compare"])
    ev.add_argument("--mode", choices=["text-sim", "replay", "live-sim"])
    ev.add_argument("--audio", action="store_true")
    ev.add_argument("--scenario", default=None)
    ev.add_argument("--record", action="store_true")
    ev.add_argument("--base-ref")
    ev.add_argument("--head-ref")
    args = parser.parse_args(argv)
    try:
        env = dict(os.environ)
        if args.command == "eval" and args.mode:
            env["WAITLESS_MODE"] = args.mode
        settings = load_settings(env)
        if args.command in ("company", "check"):
            missing = [v for v in REQUIRED_FOR_COMPANY if not env.get(v, "").strip()]
            if missing:
                raise ConfigError(missing)
        if args.command == "serve":
            import uvicorn
            from waitless.app import create_app
            uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
            return EXIT_OK
        if args.command == "company":
            from waitless.company.app import run_company
            return run_company(settings)
        if args.command == "trace":
            from waitless.store import Store
            from waitless.trace import Trace
            store = Store(settings.db_path)
            try:
                violations = Trace(store, SystemClock()).check_call(args.call_id)
                print(json.dumps([v.model_dump() for v in violations]))
                return EXIT_CHECK_FAILED if violations else EXIT_OK
            finally:
                store.close()
        if args.command == "check":
            report = asyncio.run(check_live(args.scenario, settings))
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
            print(report.model_dump_json(indent=2))
            passed = (report.outcome == "confirmed" and report.confirmation_matches and report.trace_ok
                      and report.menu_to_dtmf_ms_p50 is not None and report.menu_to_dtmf_ms_p50 <= 4000
                      and report.identity_check == "pass")
            return EXIT_OK if passed else EXIT_CHECK_FAILED
        if args.eval_command == "compare":
            from waitless.evals.compare import compare_refs
            if not args.base_ref or not args.head_ref:
                parser.error("compare requires --base-ref and --head-ref")
            root = settings.skills_dir.resolve().parent
            replays = {ref: settings.replay_dir / f"eval-{ref}.jsonl" for ref in (args.base_ref, args.head_ref)}
            report = asyncio.run(compare_refs(args.base_ref, args.head_ref, repo_root=root,
                                 out_dir=root / "evals" / "results", settings=settings, replays=replays))
            return EXIT_CHECK_FAILED if report.regressions else EXIT_OK
        from waitless.evals.runner import run_eval
        result = asyncio.run(run_eval(settings.mode, skills_dir=settings.skills_dir, menus_dir=settings.skills_dir.parent / "evals" / "menus",
                             settings=settings, audio=args.audio, scenario=args.scenario,
                             record_dir=settings.record_dir if args.record else None))
        print(result.model_dump_json(indent=2))
        return EXIT_OK
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG


async def check_live(scenario: str, settings: Settings) -> LiveCheckReport:
    """跑一通 live-sim 电话并核对：outcome=confirmed、确认号匹配、trace check 通过、
    菜单结束到 dtmf_sent 的 p50 ≤4000 ms、身份那一轮之后的 waitless_utterance 含 "AI assistant"。

    缺口 X-G1（定稿 self_contained，Jev .52）：check live 自己起模拟公司进程——用同一份 env 以子进程跑
    `waitless company`（回环地址，REQUIRED_FOR_COMPANY 两个变量必需），等端口可连后再拨号，结束（含异常）时关掉并回收；
    主应用在本进程内按 settings 组装（build_services、create_app），监听 settings.host/port 供运营商回连 /ws/line。
    缺口 X-G11（lane_internal，A 线自定）：写的是 settings.db_path 指的库，之后 `waitless trace check <call_id>` 能查到。"""
    import asyncio
    import json
    import os
    import re
    import statistics
    import sys
    import yaml
    from pathlib import Path
    import uvicorn
    import httpx
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from waitless.app import build_services, create_app
    from waitless.config import ConfigError, REQUIRED_FOR_COMPANY
    from waitless.models import IvrMap, SKILL_COMPANY_ID_KEY
    from urllib.parse import urlunsplit
    if settings.mode != "live-sim":
        raise ConfigError(["WAITLESS_MODE"], "check live requires WAITLESS_MODE=live-sim")
    missing = [name for name, value in zip(REQUIRED_FOR_COMPANY, (settings.company_host, settings.company_port)) if value is None]
    if missing:
        raise ConfigError(missing)
    if not settings.port or not settings.company_port:
        raise ConfigError(["WAITLESS_PORT", "WAITLESS_COMPANY_PORT"], "check live requires explicit nonzero listener ports")
    request = settings.live_check_request
    # Scenario eval cases may carry the canonical request; an environment override supports custom companies.
    if not request:
        from waitless.evals.runner import load_cases
        request = next((case.input.get("request") for case in load_cases(settings.skills_dir)
                        if case.kind == "scenario" and case.input.get("scenario_id", case.input.get("scenario")) == scenario
                        and case.input.get("request")), None)
    if not request:
        raise ConfigError(["WAITLESS_LIVE_CHECK_REQUEST"], "Supply WAITLESS_LIVE_CHECK_REQUEST or a scenario eval case request")
    services, parts = build_services(settings)
    app = create_app(settings, services=services, parts=parts)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning"))
    company = None
    server_task = None
    carrier = None
    try:
        company = await asyncio.create_subprocess_exec(sys.executable, "-m", "waitless.cli", "company",
                  env=dict(os.environ), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        async with asyncio.timeout(20):
            while True:
                if company.returncode is not None:
                    raise RuntimeError("Company process exited during startup")
                try:
                    reader, writer = await asyncio.open_connection(settings.company_host, settings.company_port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
        server_task = asyncio.create_task(server.serve())
        async with asyncio.timeout(20):
            while not server.started:
                if server_task.done():
                    await server_task
                    raise RuntimeError("Application server exited during startup")
                await asyncio.sleep(0.05)
        from waitless.call.live import CarrierClient
        from waitless.models import ScenarioSelection
        carrier = CarrierClient(base_url=settings.carrier_url)
        base = urlunsplit(("http", f"{settings.host}:{settings.port}", "", "", ""))
        async with httpx2.AsyncClient(timeout=None) as http:
            async with Client(streamable_http_client(base + "/mcp", http_client=http), mode="legacy") as client:
                async def call(name, args):
                    response = await client.call_tool(name, args)
                    if response.is_error:
                        raise RuntimeError("MCP check failed: " + response.content[0].text)
                    return response.structured_content
                prepared = await call("prepare_task", {"request": request})
                task_id = prepared["task_id"]
                card = prepared["task_card"]
                if card is None:
                    raise RuntimeError(prepared["message"])
                company_id = card["company"]["company_id"]
                raw = yaml.safe_load((settings.company_dir / company_id / "company.yaml").read_text(encoding="utf-8"))
                customer = raw["customers"][0]
                fields = {k: customer[k] for k in ("name", "phone_number", "address", "account_number")}
                updated = await call("update_task", {"task_id": task_id, "fields": fields})
                if updated["status"] != "ready":
                    raise RuntimeError("Live check task is not ready")
                await carrier.set_scenario(ScenarioSelection(line_number=raw["line_number"], scenario_id=scenario))
                started = await call("start_assisted_call", {"task_id": task_id, "confirmed": True,
                               "card_version": updated["task_card"]["card_version"]})
                call_id = started["call_id"]
                scenario_path = settings.company_dir / company_id / "scenarios" / (scenario + ".yaml")
                limit = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))["max_call_seconds"] + 30
                joined = False
                async with asyncio.timeout(limit):
                    while True:
                        status = await call("get_call_status", {"task_id": task_id})
                        if status["ended"]:
                            break
                        if status["pending_choice"]:
                            raise RuntimeError("Live check encountered an unexpected pending choice")
                        if status["handoff"] and not joined:
                            import websockets
                            from urllib.parse import urlencode
                            handoff = await call("join_call", {"task_id": task_id})
                            ws_url = urlunsplit(("ws", f"{settings.host}:{settings.port}", handoff["ws_path"],
                                                 urlencode({"token": handoff["token"]}), ""))
                            async with websockets.connect(ws_url) as ws:
                                await ws.send(json.dumps({"type": "hello", "format": "pcm_s16le", "sample_rate": 16000, "channels": 1}))
                                ready = json.loads(await ws.recv())
                                if ready.get("type") != "ready":
                                    raise RuntimeError("Handoff did not become ready")
                                await call("return_to_agent", {"task_id": task_id, "handoff_id": handoff["handoff_id"]})
                            joined = True
                        await asyncio.sleep(settings.engine.web.poll_ms / 1000)
                case = await call("get_case_result", {"task_id": task_id})
        skill = next(s for s in parts.tasks.skills.values() if s.metadata.get(SKILL_COMPANY_ID_KEY) == company_id)
        ivr = IvrMap.model_validate(yaml.safe_load((skill.path / "references/ivr-map.yaml").read_text(encoding="utf-8")))
        lexicons = yaml.safe_load((parts.tasks.skills["decide-option"].path / "references/lexicons.yaml").read_text(encoding="utf-8"))
        markers = lexicons["identity"]
        events = services.trace.events(call_id=call_id)
        identity_index = next((i for i,e in enumerate(events) if e.event_type == "company_turn"
                               and any(marker.casefold() in e.transcript.casefold() for marker in markers)), None)
        utterance = next((e.transcript for e in events[identity_index+1:] if e.event_type == "waitless_utterance"), None) if identity_index is not None else None
        identity_ok = bool(utterance and "ai assistant" in utterance.casefold() and
                           not any(x in utterance.casefold() for x in ("i am the account holder", "yes, this is")))
        delays = services.trace.menu_to_dtmf_ms(call_id)
        number = case["confirmation_number"]
        return LiveCheckReport(scenario_id=scenario, call_id=call_id, outcome=case["outcome"],
                 confirmation_number=number, confirmation_matches=bool(number and re.fullmatch(ivr.confirmation_pattern, number)),
                 trace_ok=not services.trace.check_call(call_id), menu_to_dtmf_ms=delays,
                 menu_to_dtmf_ms_p50=statistics.median(delays) if delays else None,
                 identity_check="pass" if identity_ok else "fail", identity_utterance=utterance)
    finally:
        if carrier:
            await carrier.aclose()
        server.should_exit = True
        if server_task:
            try:
                await asyncio.wait_for(server_task, 10)
            except TimeoutError:
                server_task.cancel()
                await asyncio.gather(server_task, return_exceptions=True)
        if company and company.returncode is None:
            company.terminate()
            try:
                await asyncio.wait_for(company.wait(), 5)
            except TimeoutError:
                company.kill()
                await company.wait()
        services.store.close()


if __name__ == "__main__":
    import sys
    sys.exit(main())
