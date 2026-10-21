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
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, PrivateAttr

from waitless.config import Settings
from waitless.live_check import read_company_stats, evaluate_identity, result_check, AnswerGate

__all__ = ["EXIT_CHECK_FAILED", "EXIT_CONFIG", "EXIT_OK", "LiveCheckReport", "check_live", "main"]

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_CONFIG = 2


@dataclass(frozen=True)
class _HolderPlan:
    frames: list[bytes]
    question_wait_ms: int
    return_after_ms: int
    choice_min_voice_ms: int
    then_keys: str | None = None
    live: bool = True
    birthday_keys: str | None = None
    bank: bool = False
    first_answer_silence_ms: int | None = None
    first_answer_no_voice_ms: int | None = None
    holder_audio_tail_ms: int | None = None
    key_ms: int | None = None
    key_tail_ms: int | None = None
    holder_windows: list[tuple[float, float]] = field(default_factory=list)


def _create_holder_polly_client(settings: Settings):
    import boto3
    return boto3.session.Session(region_name=settings.aws_region).client("polly")


def _holder_audio_frames(pcm: bytes) -> list[bytes]:
    from waitless.models import HANDOFF_FRAME_BYTES
    return [pcm[i:i + HANDOFF_FRAME_BYTES].ljust(HANDOFF_FRAME_BYTES, b"\x00")
            for i in range(0, len(pcm), HANDOFF_FRAME_BYTES)]


async def _prepare_holder_plan(scenario_data: dict, settings: Settings) -> _HolderPlan | None:
    import asyncio
    import re
    from waitless.config import ConfigError
    from waitless.company.ivr_audio import synthesize_prompt
    from waitless.models import HANDOFF_SAMPLE_RATE

    for name, step in scenario_data["steps"].items():
        if step.get("kind") == "live_rep" or any(line.get("expect", {}).get("intent") == "request_handoff"
               or line.get("expect", {}).get("action") == "handoff"
               for line in step.get("text_script", [])):
            break
    else:
        return None
    prefix = f"steps.{name}.scripted"
    scripted = step.get("scripted")
    if not isinstance(scripted, dict):
        raise ConfigError([prefix])
    bank = not any(a.get("verification") == "date_of_birth" for a in step.get("actions", []))
    # Generic live representatives (including the bank) ask two spoken questions.
    # Electricity's keypad verification keeps its strict holder configuration.
    if bank:
        scripted = dict(scripted)
        scripted.setdefault("holder_prompt_ms", settings.engine.turns.turn_end_silence_ms)
        scripted.setdefault("holder_max_asks", 4)
        scripted.setdefault("holder_reply", {"say": "Yes, I am the account holder. Please use the phone number on the account.",
                                             "polly_voice": scripted.get("polly_voice")})
    for field in ("reply_end_silence_ms", "holder_prompt_ms", "holder_max_asks"):
        value = scripted.get(field)
        if type(value) is not int or value <= 0:
            raise ConfigError([prefix + "." + field])
    reply = scripted.get("holder_reply")
    if not isinstance(reply, dict):
        raise ConfigError([prefix + ".holder_reply"])
    for field in ("say", "polly_voice"):
        value = reply.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError([prefix + ".holder_reply." + field])
    then_keys = reply.get("then_keys")
    if "then_keys" in reply and (not isinstance(then_keys, str)
                                 or re.fullmatch(r"[0-9*#]{1,16}", then_keys) is None):
        raise ConfigError([prefix + ".holder_reply.then_keys"])

    birthday_keys = None if bank else reply.get("birthday_keys", "01011990#")
    if birthday_keys is not None and (not isinstance(birthday_keys, str) or re.fullmatch(r"[0-9]{8}#", birthday_keys) is None):
        raise ConfigError([prefix + ".holder_reply.birthday_keys"])

    def synthesize():
        return synthesize_prompt(reply["say"].strip(), voice=reply["polly_voice"],
                                 polly_client=_create_holder_polly_client(settings),
                                 sample_rate=HANDOFF_SAMPLE_RATE)

    pcm = await asyncio.to_thread(synthesize)
    if not pcm:
        raise RuntimeError("Holder reply synthesis returned empty audio")
    return _HolderPlan(frames=_holder_audio_frames(pcm),
                       return_after_ms=scripted["reply_end_silence_ms"],
                       question_wait_ms=(scenario_data["max_call_seconds"] * 1000 if bank else
                           scripted["holder_prompt_ms"] * scripted["holder_max_asks"]
                           + settings.engine.turns.turn_end_silence_ms),
                       choice_min_voice_ms=scripted["holder_prompt_ms"], then_keys=then_keys,
                       birthday_keys=birthday_keys, bank=bank, live=settings.mode == "live-sim",
                       first_answer_silence_ms=settings.engine.speaker.first_answer_silence_ms,
                       first_answer_no_voice_ms=settings.engine.speaker.first_answer_no_voice_ms,
                       holder_audio_tail_ms=settings.engine.speaker.holder_audio_tail_ms,
                       key_ms=settings.engine.dtmf.user_mute_ms_per_key,
                       key_tail_ms=settings.engine.dtmf.tail_silence_ms)


async def _simulate_legacy_account_holder(ws, plan: _HolderPlan, *, threshold_dbfs: float,
                                   question_end_silence_ms: int, frame_ms: int) -> str:
    """Drain the handoff throughout a bounded listen, reply and return wait."""
    import asyncio
    import json
    import sys
    from websockets.exceptions import ConnectionClosed
    from waitless.audio.codec import pcm16_rms_dbfs
    from waitless.models import HANDOFF_FRAME_BYTES, HANDOFF_SAMPLE_RATE

    loop = asyncio.get_running_loop()
    started = loop.time()
    poll_seconds = frame_ms / 1000
    frame_seconds = HANDOFF_FRAME_BYTES / 2 / HANDOFF_SAMPLE_RATE
    question_deadline = started + plan.question_wait_ms / 1000
    then_keys = getattr(plan, "then_keys", None)
    choice_budget = plan.question_wait_ms / 1000 + frame_seconds if then_keys is not None else 0
    total_deadline = question_deadline + len(plan.frames) * frame_seconds + plan.return_after_ms / 1000
    total_deadline += choice_budget
    last_voice = None
    choice_started = None
    choice_first_voice = None
    choice_last_voice = None
    ended = False

    async def receive():
        nonlocal last_voice, choice_first_voice, choice_last_voice, ended
        try:
            while True:
                frame = await ws.recv()
                if isinstance(frame, bytes):
                    if pcm16_rms_dbfs(frame) > threshold_dbfs:
                        last_voice = loop.time()
                        # Enable this only after the entire DOB reply was sent;
                        # neither old audio nor audio during that reply counts.
                        if choice_started is not None:
                            if (choice_last_voice is None
                                    or last_voice - choice_last_voice > question_end_silence_ms / 1000):
                                choice_first_voice = last_voice
                            choice_last_voice = last_voice
                else:
                    try:
                        message = json.loads(frame)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(message, dict) and message.get("type") == "ended":
                        ended = True
                        return
        except ConnectionClosed:
            ended = True

    reader = asyncio.create_task(receive())
    sender = None

    async def pause_until(deadline):
        await asyncio.wait({reader}, timeout=max(0, deadline - loop.time()))
        if reader.done():
            await reader

    try:
        async with asyncio.timeout_at(total_deadline) as timeout:
            while True:
                if reader.done():
                    await reader
                if ended:
                    return "ended"
                now = loop.time()
                if now >= question_deadline:
                    print("check live: holder question not heard", file=sys.stderr)
                    return "no_question"
                if last_voice is not None and now - last_voice >= question_end_silence_ms / 1000:
                    break
                await pause_until(min(question_deadline, now + poll_seconds))

            answer_started = loop.time()
            # Give late answers their full budget without shortening the
            # existing bound for early answers with slow sends.
            timeout.reschedule(max(total_deadline, answer_started + len(plan.frames) * frame_seconds
                                   + plan.return_after_ms / 1000 + frame_seconds + choice_budget))
            for i, frame in enumerate(plan.frames):
                target = answer_started + i * frame_seconds
                await pause_until(target)
                if ended:
                    return "ended"
                now = loop.time()
                # Re-anchor only after missing a whole frame; small wake-up
                # errors must not accumulate, and overdue frames must not burst.
                if now - target > frame_seconds:
                    answer_started = now - i * frame_seconds
                sender = asyncio.create_task(ws.send(frame))
                # A closed peer must also interrupt a send blocked by backpressure.
                await asyncio.wait({reader, sender}, return_when=asyncio.FIRST_COMPLETED)
                if reader.done():
                    await reader
                if ended:
                    return "ended"
                await sender
                sender = None

            if then_keys is not None:
                choice_started = loop.time()
                choice_deadline = choice_started + plan.question_wait_ms / 1000
                # Slow DOB sends must leave a complete second listen and return
                # budget, while the original first-stage bound is retained.
                timeout.reschedule(max(timeout.when(), choice_deadline
                                       + plan.return_after_ms / 1000 + frame_seconds))
                while True:
                    if reader.done():
                        await reader
                    if ended:
                        return "ended"
                    now = loop.time()
                    if now >= choice_deadline:
                        print("check live: holder choice question not heard", file=sys.stderr)
                        return "no_choice_question"
                    if (choice_first_voice is not None
                            and choice_last_voice - choice_first_voice >= plan.choice_min_voice_ms / 1000
                            and now - choice_last_voice >= question_end_silence_ms / 1000):
                        break
                    await pause_until(min(choice_deadline, now + poll_seconds))

                sender = asyncio.create_task(ws.send(json.dumps({"type": "dtmf", "keys": then_keys})))
                await asyncio.wait({reader, sender}, return_when=asyncio.FIRST_COMPLETED)
                if reader.done():
                    await reader
                if ended:
                    return "ended"
                await sender
                sender = None
                # An ACK only confirms transmission. Keep draining for the full
                # configured return wait, regardless of any control frames.
                timeout.reschedule(max(timeout.when(), loop.time()
                                       + plan.return_after_ms / 1000 + frame_seconds))
            else:
                timeout.reschedule(max(timeout.when(), loop.time()
                                       + plan.return_after_ms / 1000 + frame_seconds))

            await pause_until(loop.time() + plan.return_after_ms / 1000)
            return "ended" if ended else "answered"
    except ConnectionClosed:
        return "ended"
    finally:
        tasks = {reader}
        if sender is not None:
            tasks.add(sender)
        for task in tasks:
            task.cancel()
        done, pending = await asyncio.wait(tasks, timeout=poll_seconds)
        for task in done:
            if not task.cancelled():
                # Errors are handled in the main path; reap without replacing
                # an already-observed close or the caller's cancellation.
                task.exception()
        if pending:
            raise RuntimeError("Holder WebSocket task did not stop")


async def _simulate_account_holder(ws, plan: _HolderPlan, *, threshold_dbfs: float,
                                   question_end_silence_ms: int, frame_ms: int) -> str:
    """Live: listen until the first company phrase ends, answer, then private questions and end.

    Legacy plans remain available for text-era timing tests. Live plans never return AI media.
    """
    if not getattr(plan, "live", False):
        return await _simulate_legacy_account_holder(ws, plan, threshold_dbfs=threshold_dbfs,
                    question_end_silence_ms=question_end_silence_ms, frame_ms=frame_ms)
    import asyncio
    import json
    import sys
    from websockets.exceptions import ConnectionClosed
    from waitless.audio.codec import pcm16_rms_dbfs
    from waitless.models import HANDOFF_FRAME_BYTES, HANDOFF_SAMPLE_RATE
    loop = asyncio.get_running_loop()
    tick = frame_ms / 1000
    frame_s = HANDOFF_FRAME_BYTES / 2 / HANDOFF_SAMPLE_RATE
    silence_s = question_end_silence_ms / 1000
    first_answer = AnswerGate(ready_ms=loop.time()*1000,
        silence_ms=getattr(plan, 'first_answer_silence_ms', None),
        no_voice_ms=getattr(plan, 'first_answer_no_voice_ms', None))
    for name in ('holder_audio_tail_ms', 'key_ms', 'key_tail_ms'):
        value = getattr(plan, name, None)
        if type(value) is not int or value < 0:
            raise ValueError('missing holder configuration: ' + name)
    windows = plan.holder_windows

    def sent_window(start, end):
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(end, windows[-1][1]))
        else:
            windows.append((start, end))
    first_voice = last_voice = None
    listen_after = float("inf")
    ended = False
    sender = None

    async def receive():
        nonlocal first_voice, last_voice, ended
        try:
            while True:
                value = await ws.recv()
                if isinstance(value, bytes):
                    now = loop.time()
                    voiced = pcm16_rms_dbfs(value) > threshold_dbfs
                    first_answer.observe(now*1000, voiced=voiced)
                    if now >= listen_after and voiced:
                        if last_voice is None or now - last_voice > silence_s:
                            first_voice = now
                        last_voice = now
                else:
                    try:
                        message = json.loads(value)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(message, dict) and message.get("type") == "ended":
                        ended = True
                        return
        except ConnectionClosed:
            ended = True

    reader = asyncio.create_task(receive())

    async def pause(seconds):
        await asyncio.wait({reader}, timeout=max(0, seconds))
        if reader.done():
            await reader

    async def send(value):
        nonlocal sender
        send_start = loop.time()*1000
        sender = asyncio.create_task(ws.send(value))
        await asyncio.wait({reader, sender}, return_when=asyncio.FIRST_COMPLETED)
        if reader.done():
            await reader
        if ended:
            return False
        await sender
        sender = None
        if isinstance(value, str):
            message = json.loads(value)
            if message.get('type') == 'dtmf':
                sent_window(send_start, loop.time()*1000 + len(message['keys'])*plan.key_ms
                            + plan.key_tail_ms + plan.holder_audio_tail_ms)
        else:
            sent_window(send_start, loop.time()*1000 + frame_s*1000 + plan.holder_audio_tail_ms)
        return True

    async def speak():
        start = loop.time()
        for index, frame in enumerate(plan.frames):
            target = start + index * frame_s
            await pause(target - loop.time())
            if ended:
                return False
            now = loop.time()
            if now - target > frame_s:
                start = now - index * frame_s
            if not await send(frame):
                return False
        return True

    def listen():
        nonlocal listen_after, first_voice, last_voice
        first_voice = last_voice = None
        listen_after = loop.time()

    async def question(min_voice_ms=0):
        deadline = loop.time() + plan.question_wait_ms / 1000
        while not ended:
            now = loop.time()
            if (last_voice is not None and last_voice - first_voice + frame_s >= min_voice_ms / 1000
                    and now - last_voice >= silence_s):
                return True
            if now >= deadline:
                return False
            await pause(min(tick, deadline - now))
        return False

    # One bound also covers stuck sends. Every full question receives a fresh bounded stage.
    reply_s = len(plan.frames) * frame_s
    budget_s = 3 * (plan.question_wait_ms / 1000 + reply_s + plan.return_after_ms / 1000 + frame_s)
    budget_s += (plan.first_answer_no_voice_ms + plan.first_answer_silence_ms) / 1000
    try:
        async with asyncio.timeout(budget_s):
            while not ended and not first_answer.due(loop.time()*1000):
                await pause(tick)
            if ended:
                return "ended"
            if not await speak():
                return "ended"
            listen()
            if not await question():
                if ended: return "ended"
                print("check live: holder question not heard", file=sys.stderr)
                return "no_question"
            listen_after = float("inf")
            if plan.bank:
                if not await speak(): return "ended"
            else:
                if not await send(json.dumps({"type": "dtmf", "keys": plan.birthday_keys})): return "ended"
                listen()
                if not await question(plan.choice_min_voice_ms):
                    if ended: return "ended"
                    print("check live: holder choice question not heard", file=sys.stderr)
                    return "no_choice_question"
                listen_after = float("inf")
                if plan.then_keys is None:
                    raise RuntimeError("Live keypad holder requires a deposit choice")
                if not await send(json.dumps({"type": "dtmf", "keys": plan.then_keys})): return "ended"
            # ACKs do not mean the call ended. Drain company audio until its hangup.
            deadline = loop.time() + plan.question_wait_ms / 1000 + plan.return_after_ms / 1000
            while not ended and loop.time() < deadline:
                await pause(min(tick, deadline - loop.time()))
            return "ended" if ended else "not_ended"
    except ConnectionClosed:
        return "ended"
    finally:
        tasks = {reader} | ({sender} if sender else set())
        for task in tasks: task.cancel()
        done, pending = await asyncio.wait(tasks, timeout=tick)
        for task in done:
            if not task.cancelled(): task.exception()
        if pending: raise RuntimeError("Holder WebSocket task did not stop")


def _live_check_timeout(scenario_data, settings):
    queue_s = sum(step.get("seconds", 0) for step in scenario_data["steps"].values() if step.get("kind") == "hold")
    return max(scenario_data["max_call_seconds"], queue_s + settings.engine.handoff.join_timeout_s
               + settings.engine.handoff.rejoin_grace_s) + 30


def _read_company_stats(path, call_id, offset=0):
    """Read the unique new row for this call; preserve seconds and normalize once."""
    return read_company_stats(path, call_id, offset)


def _identity_check(events, stats, *, holder_ready_ms, rep_final_ms=None, config,
                    holder_windows=(), company_id=None, scenario_id=None, dtmf_key_ms=None):
    """E183c uses gate facts and full wire spans; FINAL is observational only."""
    return evaluate_identity(events, stats, holder_ready_ms=holder_ready_ms, config=config,
        holder_windows=holder_windows, company_id=company_id, scenario_id=scenario_id, dtmf_key_ms=dtmf_key_ms).ok


class LiveCheckReport(BaseModel):
    """`waitless check live` 的报告（AC-95–AC-97；字段名按 PRD v4 AC-97 与 tech v4 §7.1 AC-96）。

    menu_to_dtmf_ms：每条 dtmf_sent 按 decision_id → decision.action.turn_seq → company_turn 算出的差值（A-08）；
    menu_to_dtmf_ms_p50：它们的 p50，≤4000 才算通过（AC-96）；没有按键时为 null。
    identity_check：E183c 门事实、关联清理、完整有声跨度与接手后静音。
    handoff_latency_ms：通知减线路首句内容尾，先比较原值，可以为负。
    两个 holder_join 字段及 FINAL 均仅作观察；identity_utterance 在 live 固定为 null。
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
    result_source: Literal["user", "transcript"] = "transcript"
    expected_outcome: str = "confirmed"
    handoff_latency_ms: float | None = None
    handoff_latency_limit_ms: float | None = None
    holder_join_after_notice_ms: float | None = None
    holder_join_after_line_end_ms: float | None = None
    last_voiced_before_ready_minus_clear_ms: float | None = None
    voiced_beyond_transit: int = 0
    ready_tone_digits: int = 0
    identity_failure_reason: str | None = None
    user_result_check: bool = False
    rep_pre_final_voiced_frames: int | None = None
    _unexpected_choice: bool = PrivateAttr(default=False)


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
        if args.command == "eval" and settings.lookup != "off":
            raise ConfigError(("WAITLESS_LOOKUP",), "eval runs without live lookup")
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
            number_ok = report.confirmation_matches or (report.result_source == "user" and report.confirmation_number is None)
            passed = (not report._unexpected_choice and report.outcome == report.expected_outcome
                      and (report.outcome != "confirmed" or number_ok) and report.trace_ok
                      and report.menu_to_dtmf_ms_p50 is not None and report.menu_to_dtmf_ms_p50 <= 4000
                      and report.identity_check == "pass" and report.user_result_check)
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
    菜单结束到 dtmf_sent 的 p50 ≤4000 ms、坐席首句 FINAL 后到本人 ready 前线路有声帧为零。

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
    from waitless.models import HANDOFF_SAMPLE_RATE, IvrMap, SKILL_COMPANY_ID_KEY
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
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning",
                                         timeout_graceful_shutdown=2))
    company = None
    server_task = None
    carrier = None
    cleanup = _LiveCheckCleanup(parts, app)
    unexpected_choice = False
    holder_ready_ms = None
    user_result_ok = False
    try:
        company = await asyncio.create_subprocess_exec(sys.executable, "-m", "waitless.cli", "company",
                  env={**os.environ, 'WAITLESS_COMPANY_AGENT_MODE': 'scripted'},
                  stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
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
                scenario_path = settings.company_dir / company_id / "scenarios" / (scenario + ".yaml")
                scenario_data = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
                holder_plan = await _prepare_holder_plan(scenario_data, settings)
                limit = _live_check_timeout(scenario_data, settings)
                await carrier.set_scenario(ScenarioSelection(line_number=raw["line_number"], scenario_id=scenario))
                stats_offset = (settings.company_stats_path.stat().st_size
                    if settings.company_stats_path is not None and settings.company_stats_path.exists() else 0)
                started = await call("start_assisted_call", {"task_id": task_id, "confirmed": True,
                               "card_version": updated["task_card"]["card_version"]})
                call_id = started["call_id"]
                joined = False
                async with asyncio.timeout(limit):
                    while True:
                        status = await call("get_call_status", {"task_id": task_id})
                        if status["ended"]:
                            break
                        choice = status["pending_choice"]
                        if choice and choice["status"] == "open":
                            unexpected_choice = True
                            print("check live: unexpected pending choice: " + choice["question_text"], file=sys.stderr)
                            break
                        if status["handoff"] and not joined:
                            import websockets
                            from websockets.exceptions import ConnectionClosed
                            from urllib.parse import urlencode
                            handoff = await call("join_call", {"task_id": task_id})
                            ws_url = urlunsplit(("ws", f"{settings.host}:{settings.port}", handoff["ws_path"],
                                                 urlencode({"token": handoff["token"]}), ""))
                            received_ready = False
                            try:
                                async with websockets.connect(ws_url) as ws:
                                    await ws.send(json.dumps({"type": "hello", "format": "pcm_s16le",
                                                             "sample_rate": HANDOFF_SAMPLE_RATE, "channels": 1}))
                                    ready = json.loads(await ws.recv())
                                    received_ready = ready.get("type") == "ready"
                                    if received_ready:
                                        import time
                                        holder_ready_ms = time.monotonic() * 1000
                                    if ready.get("type") == "ended":
                                        result = "ended"
                                    elif ready.get("type") != "ready":
                                        raise RuntimeError("Handoff did not become ready")
                                    elif holder_plan is None:
                                        result = "no_question"
                                    else:
                                        result = await _simulate_account_holder(
                                            ws, holder_plan, threshold_dbfs=settings.engine.vad.threshold_dbfs,
                                            question_end_silence_ms=settings.engine.turns.turn_end_silence_ms,
                                            frame_ms=settings.engine.vad.frame_ms,
                                        )
                                    if result in ("no_question", "no_choice_question"):
                                        unexpected_choice = True
                                    if result == "no_choice_question":
                                        break
                                    if result != "ended":
                                        unexpected_choice = True
                            except ConnectionClosed as exc:
                                if not received_ready:
                                    code = exc.rcvd.code if exc.rcvd is not None else "none"
                                    print(f"check live: handoff closed before ready (code={code})", file=sys.stderr)
                            joined = True
                        await asyncio.sleep(settings.engine.web.poll_ms / 1000)
                if unexpected_choice:
                    # Leave the polling timeout before teardown, so a near-end
                    # choice cannot cancel shutdown and strand its pipeline.
                    await cleanup.stop_calls()
                    try:
                        case = await cleanup.wait(call("get_case_result", {"task_id": task_id}), 3)
                        if case["outcome"] == "in_progress":
                            case = {"outcome": "failed", "confirmation_number": None}
                    except Exception as exc:
                        print("check live: result unavailable: " + type(exc).__name__, file=sys.stderr)
                        case = {"outcome": "failed", "confirmation_number": None}
                else:
                    case = await call("get_case_result", {"task_id": task_id})
                    if case.get("awaiting_user_result") and holder_ready_ms is not None:
                        # An explicit simulator input supplies the user's result; never infer it from L4/Jev.
                        expected = scenario_data["expected"]["outcome"]
                        before = dict(case)
                        reported = await call("report_call_result", {"task_id": task_id, "call_id": call_id,
                                                          "outcome": expected})
                        case = await call("get_case_result", {"task_id": task_id})
                        user_result_ok = result_check(before, reported, case, call_id=call_id, expected_outcome=expected)
        skill = next(s for s in parts.tasks.skills.values() if s.metadata.get(SKILL_COMPANY_ID_KEY) == company_id)
        ivr = IvrMap.model_validate(yaml.safe_load((skill.path / "references/ivr-map.yaml").read_text(encoding="utf-8")))
        events = services.trace.events(call_id=call_id)
        stats = _read_company_stats(settings.company_stats_path, call_id, stats_offset)
        rep_final_ms = next((getattr(e, "t_ms", None) for e in events
                            if e.event_type == "company_turn" and getattr(e, "t_ms", None) is not None
                            and stats.get("rep_first_audio_ms") is not None
                            and e.t_ms >= stats["rep_first_audio_ms"]), None)
        evidence = evaluate_identity(events, stats, holder_ready_ms=holder_ready_ms,
            config=settings.engine.speaker, holder_windows=holder_plan.holder_windows if holder_plan else (),
            company_id=company_id, scenario_id=scenario, dtmf_key_ms=settings.engine.dtmf.user_mute_ms_per_key)
        identity_ok = evidence.ok
        handoff_latency = evidence.notice_latency_ms
        pre_final_frames = (sum(t < rep_final_ms for t in stats["rep_nonsilent_inbound_frame_ms"])
                            if rep_final_ms is not None and isinstance(stats.get("rep_nonsilent_inbound_frame_ms"), list)
                            and all(type(t) in (int,float) for t in stats["rep_nonsilent_inbound_frame_ms"])
                            else None)
        utterance = None
        delays = services.trace.menu_to_dtmf_ms(call_id)
        number = case["confirmation_number"]
        report = LiveCheckReport(scenario_id=scenario, call_id=call_id, outcome=case["outcome"],
                 confirmation_number=number, confirmation_matches=bool(number and re.fullmatch(ivr.confirmation_pattern, number)),
                 trace_ok=not services.trace.check_call(call_id), menu_to_dtmf_ms=delays,
                 menu_to_dtmf_ms_p50=statistics.median(delays) if delays else None,
                 identity_check="pass" if identity_ok else "fail", identity_utterance=utterance,
                 result_source=case.get("result_source", "transcript"),
                 expected_outcome=scenario_data.get("expected", {}).get("outcome", "confirmed"),
                 handoff_latency_ms=handoff_latency, rep_pre_final_voiced_frames=pre_final_frames,
                 handoff_latency_limit_ms=settings.engine.speaker.handoff_latency_max_ms,
                 holder_join_after_notice_ms=evidence.join_after_notice_ms,
                 holder_join_after_line_end_ms=evidence.join_after_line_end_ms,
                 last_voiced_before_ready_minus_clear_ms=evidence.last_voiced_before_ready_minus_clear_ms,
                 voiced_beyond_transit=evidence.voiced_beyond_transit,
                 ready_tone_digits=evidence.ready_tone_digits,
                 identity_failure_reason=evidence.reason, user_result_check=user_result_ok)
        report._unexpected_choice = unexpected_choice
        return report
    except BaseExceptionGroup as exc:
        # MCP's task groups wrap exceptions raised inside the client context.
        # Restore a sole configuration failure so main's existing handler sees it.
        cause = exc
        while isinstance(cause, BaseExceptionGroup) and len(cause.exceptions) == 1:
            cause = cause.exceptions[0]
        if isinstance(cause, ConfigError):
            raise cause from None
        raise
    finally:
        await cleanup.stop_calls()
        if carrier:
            await cleanup.attempt("carrier close", carrier.aclose(), 1)
        server.should_exit = True
        if server_task:
            await cleanup.attempt("server stop", server_task, 4)
        # Uvicorn may cancel connection handlers during its graceful shutdown.
        # Reap those handlers before closing the company or the database, too.
        await cleanup.finish_lines()
        if company:
            if company.returncode is None:
                try:
                    company.terminate()
                except Exception as exc:
                    cleanup.error("company terminate", exc)
            try:
                await cleanup.wait(company.wait(), 5)
            except Exception as exc:
                cleanup.error("company wait", exc)
                if company.returncode is None:
                    try:
                        company.kill()
                    except Exception as kill_exc:
                        cleanup.error("company kill", kill_exc)
                    await cleanup.attempt("company reap", company.wait(), 1)
        for resource in getattr(services, "_resources", []):
            try:
                if hasattr(resource, "aclose"):
                    await cleanup.attempt("resource close", resource.aclose(), 1)
                elif hasattr(resource, "close"):
                    resource.close()
            except Exception as exc:
                cleanup.error("resource close", exc)
        try:
            services.store.close()
        except Exception as exc:
            cleanup.error("store close", exc)


class _LiveCheckCleanup:
    """Bound check-only teardown; never cancel all pipecat workers together.

    A shared 28-second deadline starts at the first hangup (also on the pending
    choice path). Each timed-out operation gets at most one second to unwind.
    Only /ws/line handlers are cancelled to unblock a still-running pipeline.
    """

    def __init__(self, parts, app):
        self.parts, self.app = parts, app
        self.deadline = None
        self.calls_stopped = False

    def remaining(self, limit):
        import asyncio
        now = asyncio.get_running_loop().time()
        if self.deadline is None:
            self.deadline = now + 28
        return max(0, min(limit, self.deadline - now))

    @staticmethod
    def error(step, exc):
        import sys
        # Keep arbitrary exception payloads (which can contain credentials) out
        # of diagnostics. The open choice itself is logged separately as data.
        print("check live: " + step + " failed: " + type(exc).__name__, file=sys.stderr)

    async def wait(self, awaitable, limit):
        import asyncio
        task = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.remaining(limit))
        except asyncio.CancelledError:
            await self.cancel_wait(task)
            raise
        if done:
            if task.cancelled():
                raise RuntimeError("Cleanup operation was cancelled")
            return task.result()
        await self.cancel_wait(task)
        raise TimeoutError()

    async def cancel_wait(self, task):
        import asyncio
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=self.remaining(1))
        if done:
            if not task.cancelled():
                task.exception()
        else:
            task.add_done_callback(lambda t: None if t.cancelled() else t.exception())

    async def attempt(self, step, awaitable, limit):
        try:
            return await self.wait(awaitable, limit)
        except Exception as exc:
            self.error(step, exc)

    async def stop_calls(self):
        if not self.calls_stopped:
            self.calls_stopped = True
            # Production AppParts always has calls; older injected MCP-only
            # fakes have no call manager and no running line handlers.
            calls = getattr(self.parts, "calls", None)
            if calls is not None:
                await self.attempt("call shutdown", calls.shutdown(), 3)
        await self.finish_lines()

    async def finish_lines(self):
        import asyncio
        tasks = {task for task in getattr(self.app.state, "_line_tasks", ()) if not task.done()}
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=self.remaining(2))
        for task in pending:
            task.cancel()
        if pending:
            cancelled, pending = await asyncio.wait(pending, timeout=self.remaining(3))
            done |= cancelled
        for task in done:
            if not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    self.error("line handler", exc)
        if pending:
            self.error("line handler stop", TimeoutError())


if __name__ == "__main__":
    import sys
    sys.exit(main())
