"""D 线（通话引擎）测试的共用支撑（第 3 步写；属于可见组，冻结，Codex 不改）。

被测代码只经骨架与 tech §6/§7 列出的公开名字访问：
- waitless.models、waitless.interfaces 的模型与接口；
- waitless.call.* 与 waitless.audio.vad 在骨架里列出的类、函数与方法；
- Pipecat 1.12.0 处理器的公开方法 setup()、link()、process_frame()、push_frame()
  （tech §12：Pipecat 处理器直接用 process_frame 驱动）。

这里的替身只代替「别的线」或「外部」：决策门与待决登记（G）、大模型任务、存储与 trace（A）、
运营商 HTTP（fakes/carrier.py）、Sonic（fakes/sonic.py）、线路与浏览器的 WebSocket（ASGI 消息）。
D 线自己的组件一律用真实实现。

依赖 Jev 定稿（RUN/step3/ambiguity-decisions.md）与骨架 r2 的地方：
- A-02：DP-C2 的结果编码（models.SAID_OK / SAID_REINJECT / SAID_SAFE_DEFAULT，safe_default 的 reason_code=SAY_MISMATCH），
  DecisionRequest.attempt 与 template_vars[SAID_SUMMARY_VAR]，见 said_* 与 FakeGate；
- A-08：call_round 的 decision 事件 action={TURN_SEQ_KEY: n}，见 FakeGate；
- A-15：OutputGuard.on_turn(turn) 登记轮次，回应按开始时刻归属，见 register_turn()；
- A-25：trace 用共享的 fakes.FakeTrace（G-G-01）：写入时检查有后果事件、decision 事件与 call_round 的 turn_seq；
- A-27：Settings 在 waitless.config，见 make_settings()。
骨架 r3/r4 的缺口定稿（gap-decisions-r3.md、gap-decisions-r4.md）：
- E-G-4、G-G-03：TurnDetector 按 parse_menu(text, compiled.press_patterns, digit_words=...) 判菜单，见 make_compiled、patch_menu_parser；
- E-G-6：line_endpoint、handoff_endpoint 先 accept 再核对令牌，session 不再 accept，见 FakeHandoffSession；
- D-G-1：LiveActionBackend 必填 call_sid，见 TEST_CALL_SID；D-G-2：call_round 的 DecisionResult.turn_seq，见 FakeGate、record_decision；
- D-G-4：转接事件固定顺序 handoff_announced → handoff_ready → handoff_started → handoff_ended；
- D-G-11：phase 按 models.CALL_PHASE_FROM_JEV。
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import math
import re
import struct
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode, urlparse

from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    OutputDTMFFrame,
    OutputDTMFUrgentFrame,
    StartFrame,
    TTSAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor, FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager
from starlette.exceptions import WebSocketException
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState
from fastapi import WebSocket as FastAPIWebSocket  # 模块级导入：本文件启用了延迟注解，FastAPI 要在模块命名空间里解析路由参数的 WebSocket 类型

from fakes import FakeTrace
from waitless import models as m
from waitless.call.line_protocol import LINE_FRAME_MS, StartMsg
from waitless.interfaces import AppServices, CallDeps, ToolFailure

DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM
STEP_MS = LINE_FRAME_MS  # 线路每帧 20 ms（§6.3 协议常量）

# ---------------------------------------------------------------------------
# 音频
# ---------------------------------------------------------------------------


def tone_pcm16(ms: int, rate: int, *, amplitude: float = 0.1, freq: float = 440.0) -> bytes:
    """幅度 amplitude 的正弦音（0.1 ≈ −23 dBFS，高于 engine.vad.threshold_dbfs）。"""
    n = int(rate * ms / 1000)
    peak = int(32767 * amplitude)
    return b"".join(struct.pack("<h", int(peak * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))


def zero_pcm16(ms: int, rate: int) -> bytes:
    return bytes(2 * int(rate * ms / 1000))


def is_all_zero(data: bytes) -> bool:
    return not any(data)


def in_frame(pcm: bytes, rate: int) -> InputAudioRawFrame:
    return InputAudioRawFrame(audio=pcm, sample_rate=rate, num_channels=1)


def audio_ms(frames: Iterable[OutputAudioRawFrame]) -> float:
    return sum(len(f.audio) / (2 * f.num_channels * f.sample_rate) * 1000 for f in frames)


# ---------------------------------------------------------------------------
# trace：用共享的 fakes.FakeTrace（G-G-01；seq 从 0 起，写入检查与 interfaces.Trace.event 一致）
# ---------------------------------------------------------------------------


def injection_texts(trace: FakeTrace, call_id: str) -> list[str]:
    """injection_sent 的 action.text（A-08 约定），去掉 SONIC_INSTRUCTION_PREFIX。"""
    out = []
    for e in trace.of_type("injection_sent", call_id=call_id):
        text = str((e.action or {}).get("text", ""))
        out.append(text.removeprefix(m.SONIC_INSTRUCTION_PREFIX))
    return out


# ---------------------------------------------------------------------------
# 帧处理器的直连驱动（tech §12：直接调用 process_frame）
# ---------------------------------------------------------------------------


class Capture(FrameProcessor):
    """直通模式的记录器：记下收到的每一帧和方向。"""

    def __init__(self, name: str = "capture") -> None:
        super().__init__(name=name, enable_direct_mode=True)
        self.frames: list[tuple[Frame, FrameDirection]] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        self.frames.append((frame, direction))

    def down(self, cls: type | tuple[type, ...] = Frame) -> list[Any]:
        return [f for f, d in self.frames if d == DOWN and isinstance(f, cls)]

    def up(self, cls: type | tuple[type, ...] = Frame) -> list[Any]:
        return [f for f, d in self.frames if d == UP and isinstance(f, cls)]

    def tts_audio(self) -> list[TTSAudioRawFrame]:
        return self.down(TTSAudioRawFrame)

    def dtmf(self) -> list[Any]:
        return self.down((OutputDTMFFrame, OutputDTMFUrgentFrame))

    def clear(self) -> None:
        self.frames.clear()


class Relay(Capture):
    """直连：收到的帧直接交给相邻真实处理器的 process_frame（下行给后一个，上行给前一个），同时记录。"""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        target = self.next if direction == DOWN else self.previous
        if target is not None:
            await target.process_frame(frame, direction)


class Chain:
    """head(记录上行) → p1 → 直连 → p2 → … → tail(记录下行)，全部在测试的事件循环里同步处理。

    start() 给每个处理器 setup（真实 TaskManager 与 SystemClock），再从头部送一个 StartFrame。
    """

    def __init__(self, *procs: FrameProcessor) -> None:
        if not procs:
            raise ValueError("Chain needs at least one processor")
        self.procs = list(procs)
        self.head = Capture("chain-head")
        self.tail = Capture("chain-tail")
        self.relays: list[Relay] = []
        self.head.link(self.procs[0])
        for a, b in zip(self.procs, self.procs[1:]):
            r = Relay(f"relay:{a.name}->{b.name}")
            a.link(r)
            r.link(b)
            self.relays.append(r)
        self.procs[-1].link(self.tail)

    def relay_after(self, proc: FrameProcessor) -> Relay:
        return self.relays[self.procs.index(proc)]

    async def start(self) -> "Chain":
        tm = TaskManager(loop=asyncio.get_running_loop())
        clock = SystemClock()
        clock.start()
        setup = FrameProcessorSetup(clock=clock, task_manager=tm, pipeline_worker=None)
        for p in (self.head, *self.relays, self.tail, *self.procs):
            await p.setup(setup)
        await self.push(StartFrame())
        return self

    async def push(self, frame: Frame, direction: FrameDirection = DOWN) -> None:
        if direction == DOWN:
            await self.procs[0].process_frame(frame, DOWN)
        else:
            await self.procs[-1].process_frame(frame, UP)

    async def push_at(self, proc: FrameProcessor, frame: Frame, direction: FrameDirection = DOWN) -> None:
        """从链中间某个处理器送入（例如 Sonic 之后、MediaGateOut 之前产生的帧）。"""
        await proc.process_frame(frame, direction)

    async def stop(self) -> None:
        for p in (*self.procs, *self.relays, self.head, self.tail):
            try:
                await p.cleanup()
            except Exception:  # noqa: BLE001 - 清理阶段不掩盖测试本身的结论
                pass

    async def __aenter__(self) -> "Chain":
        return await self.start()

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()


async def settle(rounds: int = 60) -> None:
    """让排队的 asyncio 任务跑完（不推进任何时钟，不 sleep 真实时间）。"""
    for _ in range(rounds):
        await asyncio.sleep(0)


async def wait_until(pred: Callable[[], Any], *, rounds: int = 4000, what: str = "condition") -> None:
    for _ in range(rounds):
        if pred():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {what}")


# ---------------------------------------------------------------------------
# 共享模型的构造（测试值，不是密钥）
# ---------------------------------------------------------------------------

COMPANY_ID = "lakeshore-energy"
COMPANY_NAME = "Lakeshore Energy"
STALL_PHRASES = ("One moment please.", "Let me check on that for you.")
SAY_TEMPLATES: dict[str, str] = {
    "handoff.announce": "I'm going to bring the account holder onto the line now.",
    "handoff.resume": "Thanks, the verification is complete. Let's continue with the request.",
    "handoff.ask_user": "Please hold for a moment while I check with the account holder.",
    "pending.answer": "The account holder chose {option_label}.",
    "pending.safe_default": "I'm not able to decide that now; we'll follow up later.",
    "end.goodbye": "Thank you for your help. Goodbye.",
    "ask_repeat": "Sorry, could you repeat that?",
}
HANDOFF_HELLO = {"type": "hello", "format": "pcm_s16le", "sample_rate": m.HANDOFF_SAMPLE_RATE, "channels": 1}
# navigate-ivr 的 press_patterns 与 digit_words 在测试里的样子（E-G-4、G-G-03；真正的值归 G 的 skill 文件）
MENU_PATTERNS = (r"(?i)press (?P<digits>[0-9*#])",)
DIGIT_WORDS = {"one": "1", "two": "2", "nine": "9", "pound": "#"}
# 本通在运营商那边的 callSid（D-G-1：LiveActionBackend 必填 call_sid）
TEST_CALL_SID = "CA" + "0d" * 16


def make_task_card(*, official_number: str, company_name: str = COMPANY_NAME) -> m.TaskCard:
    return m.TaskCard(
        card_version=1,
        company=m.TaskCompany(
            company_id=COMPANY_ID,
            name=company_name,
            official_number=official_number,
            sources=[m.Source(url="https://lakeshore-energy.example/contact", title="Contact us")],
        ),
        goal="Start electric service at the new address",
        task_summary="Start service on 11/1 at the new address",
        effective_date="11/1",
        shared_fields=[],
        precall_decisions=[],
        boundary="Waitless never shares SSN, card numbers or date of birth.",
    )


def make_compiled(
    *,
    allowed_fields: Mapping[str, str],
    official_number: str,
    stall_phrases: Sequence[str] = STALL_PHRASES,
    say_templates: Mapping[str, str] | None = None,
    press_patterns: Sequence[str] | None = None,
    digit_words: Mapping[str, str] | None = None,
) -> m.CompiledCall:
    return m.CompiledCall(
        sonic_prompt="You are an AI assistant calling Lakeshore Energy on behalf of the account holder.",
        jev_templates={},
        policy={},
        ivr_map={},
        intents=[],
        lexicons={},
        stall_phrases=list(stall_phrases),
        say_templates=dict(say_templates or SAY_TEMPLATES),
        allowed_fields=dict(allowed_fields),
        press_patterns=list(MENU_PATTERNS if press_patterns is None else press_patterns),  # E-G-4
        digit_words=dict(DIGIT_WORDS if digit_words is None else digit_words),  # G-G-03
        task_card=make_task_card(official_number=official_number),
        skill_versions=[m.SkillRef(name="decide-option", commit="unknown", content_sha256="0" * 64)],
    )


def make_spec(
    compiled: m.CompiledCall,
    *,
    to_number: str,
    call_id: str = "call-d-1",
    task_id: str = "task-d-1",
    company_name: str = COMPANY_NAME,
) -> m.CallSpec:
    return m.CallSpec(
        call_id=call_id,
        task_id=task_id,
        mode="live-sim",
        company_name=company_name,
        to_number=to_number,
        compiled=compiled,
    )


def make_settings(env: Mapping[str, str], engine: m.EngineConfig) -> Any:
    """live-sim 的 Settings，值全部取自 conftest 的 mode_env（测试值）。不调 A 线的 load_settings。

    A-27：Settings 归 config.py（骨架 r1 里 config.py 再导出 models.Settings，r2 搬家后同一个导入仍可用）。
    只填 Settings 里存在的字段，A 线增补的可选字段不影响这里。"""
    from waitless.config import Settings

    data: dict[str, Any] = {
        "mode": env["WAITLESS_MODE"],
        "host": env["WAITLESS_HOST"],
        "port": int(env["WAITLESS_PORT"]),
        "allowed_hosts": tuple(env["WAITLESS_ALLOWED_HOSTS"].split(",")),
        "db_path": env["WAITLESS_DB_PATH"],
        "web_dir": env["WAITLESS_WEB_DIR"],
        "skills_dir": env["WAITLESS_SKILLS_DIR"],
        "company_dir": env["WAITLESS_COMPANY_DIR"],
        "engine_config_path": env["WAITLESS_ENGINE_CONFIG"],
        "carrier_url": env["WAITLESS_CARRIER_URL"],
        "line_ws_url": env["WAITLESS_LINE_WS_URL"],
        "aws_region": env["AWS_REGION"],
        "aws_access_key_id": env["AWS_ACCESS_KEY_ID"],
        "aws_secret_access_key": env["AWS_SECRET_ACCESS_KEY"],
        "sonic_model_id": env["WAITLESS_SONIC_MODEL_ID"],
        "sonic_voice": env["WAITLESS_SONIC_VOICE"],
        "planner_model_id": env["WAITLESS_PLANNER_MODEL_ID"],
        "jev_url": env["WAITLESS_JEV_URL"],
        "jev_model": env["WAITLESS_JEV_MODEL"],
        "jev_api_key": env["WAITLESS_JEV_API_KEY"],
        "engine": engine,
    }
    fields = set(Settings.model_fields)
    return Settings.model_validate({k: v for k, v in data.items() if k in fields})


def line_ws_path(settings: Any) -> str:
    """/ws/line 的路径取自 WAITLESS_LINE_WS_URL，不在测试里另写。"""
    return urlparse(settings.line_ws_url).path


HANDOFF_ROUTE = "/ws/handoff/{handoff_id}"  # §6.4 协议路径（与 models.JoinCallOut 的 ws_path 校验同一写法）


def handoff_path(handoff_id: str) -> str:
    return HANDOFF_ROUTE.format(handoff_id=handoff_id)


# ---------------------------------------------------------------------------
# 决策结果（G 线产出的 DecisionResult 的样子）
# ---------------------------------------------------------------------------

_ids = itertools.count(1)


def new_decision_id(prefix: str = "dec-d") -> str:
    return f"{prefix}-{next(_ids)}"


def jev_exchange(point: str) -> m.JevExchange:
    state = m.JevState(
        task_goal="Start electric service",
        user_profile={"name": "Alex Example"},
        standing_rules=[],
        prior_choices=[],
        transcript=m.JevTranscript(
            note=m.JEV_TRANSCRIPT_NOTE, lines=[m.TranscriptLine(speaker="company", text="(test line)")]
        ),
    )
    if point == "said_as_decided":
        questions = {"said": m.JevQuestion(type="noul", instructions="Did Waitless convey the decision?")}
        answers = {"said": m.JevAnswer(type="noul", noul=0.9)}
    else:
        questions = {
            "menu_choice": m.JevQuestion(type="choice", instructions="Pick the option.", criteria={"1": "one"})
        }
        answers = {"menu_choice": m.JevAnswer(type="choice", choice="1", confidence=0.9, probabilities={"1": 0.9})}
    return m.JevExchange(
        request=m.JevRequest(model="fake-jev", state=state, questions=questions),
        response=m.JevResponse(answers=answers),
        latency_ms=5,
    )


def make_result(
    action: str,
    *,
    point: str = "call_round",
    intent: str | None = None,
    digits: str | None = None,
    say_text: str | None = None,
    call_phase: str | None = None,
    decision_id: str | None = None,
    reason_code: str = "test_rule",
    policy_rule: str = "test",
    turn_seq: int | None = None,
) -> m.DecisionResult:
    return m.DecisionResult(
        decision_id=decision_id or new_decision_id(),
        point=point,
        intent=intent or action,
        action=action,
        digits=digits,
        say_text=say_text,
        call_phase=call_phase,
        reason_code=reason_code,
        policy_rule=policy_rule,
        jev=jev_exchange(point),
        turn_seq=turn_seq,
    )


def decision_event(
    result: m.DecisionResult, *, task_id: str, call_id: str | None, turn_seq: int | None = None
) -> m.TraceEvent:
    return m.TraceEvent(
        task_id=task_id,
        call_id=call_id,
        event_type="decision",
        decision_id=result.decision_id,
        decision_point=result.point,
        jev_request=result.jev.request.model_dump(mode="json"),
        jev_response=result.jev.response.model_dump(mode="json"),
        policy_decision={"intent": result.intent, "action": result.action, "reason_code": result.reason_code},
        latency_ms=result.jev.latency_ms,
        action={m.TURN_SEQ_KEY: turn_seq} if turn_seq is not None else None,  # A-08
    )


def record_decision(
    trace: FakeTrace, result: m.DecisionResult, *, task_id: str, call_id: str | None, turn_seq: int | None = None
) -> m.DecisionResult:
    """像 G 的 gate 那样写 decision 事件后返回结果；call_round 的结果同时带上 turn_seq（A-08 事件、D-G-2 结果同值）。"""
    if result.point == "call_round":
        turn_seq = result.turn_seq if turn_seq is None else turn_seq
        if result.turn_seq != turn_seq:
            result = result.model_copy(update={"turn_seq": turn_seq})
    else:
        turn_seq = None
    trace.event(decision_event(result, task_id=task_id, call_id=call_id, turn_seq=turn_seq))
    return result


# A-02 定稿：DP-C2 said_as_decided 的三种结论（models 的常量）


def said_ok() -> m.DecisionResult:
    return make_result("wait", point="said_as_decided", intent=m.SAID_OK, reason_code=m.SAID_OK)


def said_reinject(original_text: str) -> m.DecisionResult:
    return make_result(
        "say", point="said_as_decided", intent=m.SAID_REINJECT, say_text=original_text, reason_code="said_below_threshold"
    )


def said_safe_default(action: str = "end", *, say_text: str | None = None) -> m.DecisionResult:
    """第二次仍未过：按 policy.yaml 的 said_as_decided.safe_default 出动作（定稿建议 end），reason_code=say_mismatch。"""
    return make_result(
        action, point="said_as_decided", intent=m.SAID_SAFE_DEFAULT, say_text=say_text, reason_code=m.SAY_MISMATCH
    )


# ---------------------------------------------------------------------------
# 别的线的替身
# ---------------------------------------------------------------------------


class FakeGate:
    """interfaces.DecisionGate 的替身（G 线）。返回前写 decision 事件；call_round 的结果与事件都带这一轮的
    turn_seq（A-08、D-G-2）。

    round_script：每项是 DecisionResult，或 f(turn, ctx) -> DecisionResult；用完后返回 wait。
    said：DP-C2 每次核对的结论 "pass" / "fail"；用完后一律 pass。按 A-02 编码：pass → said_ok；
    attempt=0 时 fail → reinject（say_text = template_vars[SAID_SUMMARY_VAR]，即本次注入的原文）；
    attempt≥1 时 fail → said_safe_default(safe_action)。用 request.attempt 判断第几次（与 G 的策略同口径）。
    """

    def __init__(
        self,
        trace: FakeTrace,
        *,
        round_script: Iterable[Any] = (),
        said: Iterable[str] = (),
        safe_action: str = "end",
        safe_say_text: str | None = None,
    ) -> None:
        self.trace = trace
        self.round_script: deque[Any] = deque(round_script)
        self.said: deque[str] = deque(said)
        self.safe_action = safe_action
        self.safe_say_text = safe_say_text
        self.round_calls: list[tuple[m.CompanyTurn, m.RoundContext]] = []
        self.point_calls: list[tuple[str, m.DecisionRequest]] = []
        self.results: list[m.DecisionResult] = []

    async def decide_round(self, turn: m.CompanyTurn, ctx: m.RoundContext) -> m.DecisionResult:
        self.round_calls.append((turn, ctx))
        entry = self.round_script.popleft() if self.round_script else make_result("wait", intent="wait")
        result = entry(turn, ctx) if callable(entry) else entry
        result = record_decision(self.trace, result, task_id=ctx.task_id, call_id=ctx.call_id, turn_seq=turn.seq)
        self.results.append(result)
        return result

    async def decide(self, point: str, request: m.DecisionRequest) -> m.DecisionResult:
        self.point_calls.append((point, request))
        if point == "said_as_decided":
            verdict = self.said.popleft() if self.said else "pass"
            if verdict == "pass":
                result = said_ok()
            elif request.attempt == 0:
                result = said_reinject(request.template_vars[m.SAID_SUMMARY_VAR])
            else:
                result = said_safe_default(self.safe_action, say_text=self.safe_say_text)
        else:
            result = make_result("wait", point=point, intent="ok")
        result = record_decision(self.trace, result, task_id=request.task_id, call_id=request.call_id)
        self.results.append(result)
        return result

    def said_requests(self) -> list[m.DecisionRequest]:
        return [r for p, r in self.point_calls if p == "said_as_decided"]


class FakePending:
    """interfaces.PendingRegistry 的最小替身（G 线）。"""

    def __init__(self, clock: Any) -> None:
        self.clock = clock
        self.created: list[tuple[m.PendingChoice, str, str]] = []
        self._by_id: dict[str, tuple[m.PendingChoice, str, str]] = {}
        self._n = itertools.count(1)

    def create(self, *, call_id: str, decision_id: str, question_text: str, options: Sequence[m.Option], timeout_s: float) -> m.PendingChoice:
        now = self.clock.now()
        choice = m.PendingChoice(
            choice_id=f"choice-{next(self._n)}",
            question_text=question_text,
            options=list(options)[:3],
            created_at=now,
            expires_at=now + timedelta(seconds=timeout_s),
            status="open",
        )
        rec = (choice, call_id, decision_id)
        self.created.append(rec)
        self._by_id[choice.choice_id] = rec
        return choice

    def get(self, choice_id: str) -> m.PendingChoice | None:
        rec = self._by_id.get(choice_id)
        return rec[0] if rec else None

    def current(self, call_id: str) -> m.PendingChoice | None:
        for choice, cid, _ in reversed(self.created):
            if cid == call_id and choice.status == "open":
                return choice
        return None

    def answer(self, choice_id: str, option_id: str) -> m.PendingChoice:
        rec = self._by_id.get(choice_id)
        if rec is None:
            raise ToolFailure("unknown_choice")
        if option_id not in {o.id for o in rec[0].options}:
            raise ToolFailure("invalid_option")
        answered = rec[0].model_copy(update={"status": "answered"})
        self._by_id[choice_id] = (answered, rec[1], rec[2])
        return answered

    def expire_due(self) -> list[m.PendingChoice]:
        return []

    def decision_of(self, choice_id: str) -> str | None:
        rec = self._by_id.get(choice_id)
        return rec[2] if rec else None


class FakeLlm:
    """interfaces.LlmTasks 的替身（A 线）。通话引擎只会用到 L3。"""

    def __init__(self, offer_options: Sequence[m.OfferOption] = ()) -> None:
        self.offer_options = list(offer_options)
        self.calls: list[str] = []

    async def extract_offer_options(self, company_text: str) -> list[m.OfferOption]:
        self.calls.append(company_text)
        return list(self.offer_options)

    async def extract_request(self, user_text: str) -> m.ExtractedRequest:  # pragma: no cover - 引擎不该调用
        raise AssertionError("the call engine must not call L1")

    async def phrase_reply(self, **kwargs: Any) -> str:  # pragma: no cover
        raise AssertionError("the call engine must not call L2")

    async def extract_result_items(self, transcript: Any) -> m.ResultItems:  # pragma: no cover
        raise AssertionError("the call engine must not call L4")

    async def write_summary(self, **kwargs: Any) -> str:  # pragma: no cover
        raise AssertionError("the call engine must not call L5")


class FakeStore:
    """interfaces.Store 的替身（A 线）。"""

    def __init__(self, profile: Mapping[str, str]) -> None:
        self.profile = m.Profile(**dict(profile))
        self.rules = [
            m.StandingRule(rule_key="decline_surveys", rule_text="Decline surveys and marketing offers.", scope="global"),
            m.StandingRule(rule_key="paperless", rule_text="Choose paperless billing when asked.", scope="global"),
            m.StandingRule(rule_key="english", rule_text="Use English.", scope="global"),
        ]
        self.preferences: list[m.Preference] = []

    def save_profile(self, fields: Mapping[str, str]) -> m.Profile:
        self.profile = self.profile.model_copy(update=dict(fields))
        return self.profile

    def get_profile(self) -> m.Profile:
        return self.profile

    def list_standing_rules(self) -> list[m.StandingRule]:
        return list(self.rules)

    def list_preferences(self, company_id: str) -> list[m.Preference]:
        return [p for p in self.preferences if p.company_id == company_id]

    def add_preference(self, pref: m.Preference) -> None:
        self.preferences.append(pref)


class SimpleLineTokens:
    """interfaces.LineTokenIssuer 的简单实现，只给「令牌不是被测对象」的测试用。"""

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}
        self._n = itertools.count(1)

    def issue(self, call_id: str) -> str:
        token = f"line-token-{next(self._n)}"
        self._tokens[token] = call_id
        return token

    def verify(self, token: str) -> str | None:
        return self._tokens.get(token)

    def revoke(self, call_id: str) -> None:
        self._tokens = {t: c for t, c in self._tokens.items() if c != call_id}


class SimpleHandoffTokens:
    """interfaces.HandoffTokenIssuer 的简单实现，只给「令牌不是被测对象」的测试用。"""

    def __init__(self, clock: Any, ttl_s: int) -> None:
        self.clock = clock
        self.ttl_s = ttl_s
        self._tokens: dict[tuple[str, str], str] = {}
        self._n = itertools.count(1)

    def issue(self, call_id: str, handoff_id: str) -> tuple[str, str]:
        token = f"handoff-token-{next(self._n)}"
        self._tokens[(handoff_id, token)] = call_id
        return token, (self.clock.now() + timedelta(seconds=self.ttl_s)).isoformat()

    def consume(self, handoff_id: str, token: str) -> str | None:
        return self._tokens.pop((handoff_id, token), None)


class OnEnded:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, call_id: str) -> None:
        self.calls.append(call_id)


def make_deps(
    *,
    settings: Any,
    trace: FakeTrace,
    clock: Any,
    gate: Any,
    store: Any,
    pending: Any = None,
    llm: Any = None,
    line_tokens: Any = None,
    handoff_tokens: Any = None,
    on_ended: Any = None,
) -> CallDeps:
    return CallDeps(
        settings=settings,
        store=store,
        trace=trace,
        gate=gate,
        pending=pending or FakePending(clock),
        llm=llm or FakeLlm(),
        clock=clock,
        line_tokens=line_tokens or SimpleLineTokens(),
        handoff_tokens=handoff_tokens or SimpleHandoffTokens(clock, settings.engine.handoff.token_ttl_s),
        on_ended=on_ended or OnEnded(),
    )


class FakeRegistry:
    """interfaces.CallRegistry 的替身（A 线的 CallManager）。"""

    def __init__(self, sessions: Mapping[str, Any]) -> None:
        self.sessions = dict(sessions)
        self.lookups: list[str] = []

    def get_session(self, call_id: str) -> Any:
        self.lookups.append(call_id)
        return self.sessions.get(call_id)


class FakePipelineTask:
    """Pipecat PipelineWorker/PipelineTask 的替身：记下 queue_frame(s) 收到的帧；可选转发给一条 Chain 的头部。"""

    def __init__(self, forward: Callable[[Frame], Any] | None = None) -> None:
        self.frames: list[Frame] = []
        self.forward = forward

    async def queue_frame(self, frame: Frame, direction: FrameDirection = DOWN) -> None:
        self.frames.append(frame)
        if self.forward is not None and direction == DOWN:
            await self.forward(frame)

    async def queue_frames(self, frames: Iterable[Frame], direction: FrameDirection = DOWN) -> None:
        for frame in frames:
            await self.queue_frame(frame, direction)

    def dtmf(self) -> list[Any]:
        return [f for f in self.frames if isinstance(f, (OutputDTMFFrame, OutputDTMFUrgentFrame))]


def dtmf_digits(frames: Iterable[Any]) -> str:
    return "".join(f.to_string() for f in frames)


def response_audio(sonic: Any, n: int) -> list[bytes]:
    """FakeSonicService 第 n 段回应（从 1 起，按 LLMFullResponseStartFrame 计）推出的音频块。"""
    from pipecat.frames.frames import LLMFullResponseStartFrame

    out: list[bytes] = []
    idx = 0
    for f in sonic.emitted_frames:
        if isinstance(f, LLMFullResponseStartFrame):
            idx += 1
        elif isinstance(f, TTSAudioRawFrame) and idx == n:
            out.append(f.audio)
    return out


def audio_bytes(frames: Iterable[Any]) -> list[bytes]:
    return [f.audio for f in frames]


# -- LiveActionBackend 的协作者替身（只记录调用；方法名取自骨架） ----------------------------


class CallLog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _log(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


class FakeInjector(CallLog):
    def __init__(self) -> None:
        super().__init__()
        self.pending = 0

    async def inject(self, text: str, decision_id: str, *, template: str | None = None) -> None:
        self._log("inject", text, decision_id, template=template)

    async def flush(self) -> int:
        self._log("flush")
        return 0

    def stream_ready(self) -> bool:
        return True


class FakeGuard(CallLog):
    def __init__(self) -> None:
        super().__init__()
        self.state = "ivr"
        self.counters = m.GuardCounters(blocked=0, released=0)

    def set_state(self, state: str) -> None:
        self._log("set_state", state)
        self.state = state

    async def release(self, seq: int) -> bool:
        self._log("release", seq)
        return True

    async def discard(self, seq: int, reason: str) -> int:
        self._log("discard", seq, reason)
        return 0


class FakeMediaGate(CallLog):
    def __init__(self) -> None:
        super().__init__()
        self.in_handoff = False

    async def enter_handoff(self) -> None:
        self._log("enter_handoff")
        self.in_handoff = True

    async def exit_handoff(self) -> None:
        self._log("exit_handoff")
        self.in_handoff = False

    def attach_bridge(self, bridge: Any) -> None:
        self._log("attach_bridge", bridge)


class FakeCarrierApi(CallLog):
    """call/live.py CarrierClient 的替身（只给 LiveActionBackend 的单元测试用）。"""

    async def hangup(self, call_sid: str) -> m.CarrierHangupOut:
        self._log("hangup", call_sid)
        return m.CarrierHangupOut(call_sid=call_sid, status="completed")

    async def dial(self, req: m.CarrierDialIn) -> m.CarrierDialOut:
        self._log("dial", req)
        raise AssertionError("LiveActionBackend must never dial")


class FakeTurnDetector(CallLog):
    async def on_provider_text(self, ev: m.ProviderTextEvent) -> None:
        self._log("on_provider_text", ev)


class FakeActions(CallLog):
    async def on_assistant_final(self, ev: m.ProviderTextEvent) -> None:
        self._log("on_assistant_final", ev)

    async def execute(self, result: m.DecisionResult) -> m.ActionRecord:  # pragma: no cover
        self._log("execute", result)
        return m.ActionRecord(decision_id=result.decision_id, action=result.action, started_ms=0, finished_ms=0, ok=True, detail="")


class FakeBridge(CallLog):
    """HandoffBridge 的替身：记下 MediaGateIn 复制来的对方音频。"""

    def __init__(self) -> None:
        super().__init__()
        self.to_browser_frames: list[bytes] = []

    async def to_browser(self, pcm16_16k: bytes) -> None:
        self.to_browser_frames.append(bytes(pcm16_16k))


async def register_turn(guard: Any, turn: m.CompanyTurn) -> None:
    """把一轮登记给 OutputGuard：骨架 r2 的 `OutputGuard.on_turn(turn)`（A-15：LiveCall 在把轮次交给决策门之前调用，
    记下 seq → (t_start_ms, t_end_ms)）。r2 定为同步方法；这里同步、异步都接受。"""
    result = guard.on_turn(turn)
    if inspect.isawaitable(result):
        await result


def make_turn(
    *, call_id: str, seq: int, t_start_ms: int, t_end_ms: int, text: str, menu: Sequence[tuple[str, str]] = ()
) -> m.CompanyTurn:
    return m.CompanyTurn(
        call_id=call_id,
        seq=seq,
        t_start_ms=t_start_ms,
        t_end_ms=t_end_ms,
        text=text,
        kind="menu" if menu else "speech",
        menu_options=[m.MenuOption(digits=d, label=label) for d, label in menu],
    )


# ---------------------------------------------------------------------------
# 菜单解析（G 线的 decision.menu_parse.parse_menu）的替身
# ---------------------------------------------------------------------------

_MENU_RE = re.compile(r"(?:for|to)\s+([^,.;]+?),?\s+press\s+([0-9*#]|[a-z]+)", re.IGNORECASE)


def stub_parse_menu(
    text: str, patterns: Sequence[str], *, digit_words: Mapping[str, str] | None = None
) -> list[m.MenuOption]:
    words = {w.lower(): k for w, k in (digit_words or {}).items()}
    seen: dict[str, m.MenuOption] = {}
    for label, digits in _MENU_RE.findall(text):
        key = digits if digits in "0123456789*#" else words.get(digits.lower())
        if key is None:
            continue  # 不认识的数字词（没给 digit_words 时 "press one" 解析不出选项）
        seen.setdefault(key, m.MenuOption(digits=key, label=label.strip()))
    return list(seen.values())


def patch_menu_parser(monkeypatch: Any) -> list[tuple[str, tuple[str, ...], dict[str, str] | None]]:
    """把 G 线的 parse_menu 换成确定的替身（D 线的 worktree 里 G 还没实现）。D-G-6：TurnDetector 按
    waitless.decision.menu_parse.parse_menu 调用（模块属性，或模块顶层同名导入后按名字取），测试替换这两处。
    返回调用记录 (text, patterns, digit_words)。"""
    import waitless.call.turn_detector as td
    import waitless.decision.menu_parse as mp

    calls: list[tuple[str, tuple[str, ...], dict[str, str] | None]] = []

    def fake(text: str, patterns: Sequence[str], *, digit_words: Mapping[str, str] | None = None) -> list[m.MenuOption]:
        calls.append((text, tuple(patterns), dict(digit_words) if digit_words is not None else None))
        return stub_parse_menu(text, patterns, digit_words=digit_words)

    monkeypatch.setattr(mp, "parse_menu", fake)
    monkeypatch.setattr(td, "parse_menu", fake, raising=False)
    return calls




# ---------------------------------------------------------------------------
# WebSocket：在测试的事件循环里直接驱动端点（ASGI 消息）
# ---------------------------------------------------------------------------


class AsgiWs:
    """一个真正的 starlette WebSocket，收发走内存队列；客户端动作由测试推入，服务端发出的 ASGI 消息记进 sent。"""

    def __init__(
        self,
        *,
        path: str,
        query: Mapping[str, str] | None = None,
        services: Any = None,
        path_params: Mapping[str, str] | None = None,
    ) -> None:
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.raised_code: int | None = None
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "http_version": "1.1",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": urlencode(dict(query or {})).encode(),
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "subprotocols": [],
            "app": SimpleNamespace(state=SimpleNamespace(services=services)),
            "path_params": dict(path_params or {}),
            "state": {},
        }
        self.scope = scope
        self.inbox.put_nowait({"type": "websocket.connect"})
        self.websocket = WebSocket(scope, receive=self._receive, send=self._send)

    async def _receive(self) -> dict[str, Any]:
        return await self.inbox.get()

    async def _send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        if message["type"] == "websocket.close":
            # 像真实客户端那样回应关闭握手：服务端之后的 receive() 拿到 disconnect，而不是永远等下去
            self.inbox.put_nowait({"type": "websocket.disconnect", "code": int(message.get("code", 1000))})

    # -- 客户端一侧 -----------------------------------------------------------
    def text(self, data: str) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "text": data})

    def json(self, obj: Any) -> None:
        self.text(json.dumps(obj))

    def binary(self, data: bytes) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "bytes": data})

    def disconnect(self, code: int = 1000) -> None:
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": code})

    # -- 服务端发出的 -----------------------------------------------------------
    @property
    def accepted(self) -> bool:
        return any(msg["type"] == "websocket.accept" for msg in self.sent)

    @property
    def close_code(self) -> int | None:
        for msg in self.sent:
            if msg["type"] == "websocket.close":
                return int(msg.get("code", 1000))
        return self.raised_code

    @property
    def closed(self) -> bool:
        return self.close_code is not None or self.websocket.application_state == WebSocketState.DISCONNECTED

    def sent_texts(self) -> list[str]:
        return [msg["text"] for msg in self.sent if msg["type"] == "websocket.send" and msg.get("text") is not None]

    def sent_json(self) -> list[Any]:
        out = []
        for text in self.sent_texts():
            try:
                out.append(json.loads(text))
            except ValueError:
                out.append(text)
        return out

    def sent_bytes(self) -> list[bytes]:
        return [msg["bytes"] for msg in self.sent if msg["type"] == "websocket.send" and msg.get("bytes") is not None]


async def run_endpoint(coro: Any, ws: AsgiWs, *, watchdog_s: float = 5.0) -> None:
    """跑一个 WebSocket 端点协程直到返回。端点以 WebSocketException 拒绝时把关闭码记进 ws.raised_code。
    watchdog_s 只是防挂死的看门狗（真实时间），不是被测的超时逻辑。"""
    try:
        await asyncio.wait_for(coro, watchdog_s)
    except WebSocketException as exc:
        ws.raised_code = exc.code
    except WebSocketDisconnect:
        pass
    except TimeoutError as exc:
        raise AssertionError("WebSocket endpoint did not return (watchdog)") from exc


async def run_app_ws(app: Any, ws: AsgiWs, *, watchdog_s: float = 5.0) -> None:
    """把一次 WebSocket 连接交给整个 ASGI 应用（经 FastAPI 路由与异常中间件），在测试的事件循环里跑完。
    不用 TestClient 的线程：端点卡住时看门狗能取消它，测试判失败而不是挂死。"""
    scope = dict(ws.scope)
    scope.pop("app", None)  # 应用自己会填 scope["app"]
    try:
        await asyncio.wait_for(app(scope, ws._receive, ws._send), watchdog_s)
    except TimeoutError as exc:
        raise AssertionError("WebSocket endpoint did not return (watchdog)") from exc


def ws_test_app(services: Any) -> Any:
    """只挂 D 线两个 WebSocket 端点的 FastAPI 应用（A 线的 create_app 不在本线）。"""
    from fastapi import FastAPI

    from waitless.call.handoff import handoff_endpoint
    from waitless.call.line_ws import line_endpoint

    app = FastAPI()
    app.state.services = services

    @app.websocket(line_ws_path(services.settings))
    async def _line(websocket: FastAPIWebSocket) -> None:
        await line_endpoint(websocket)

    @app.websocket(HANDOFF_ROUTE)
    async def _handoff(websocket: FastAPIWebSocket, handoff_id: str) -> None:
        await handoff_endpoint(websocket, handoff_id)

    return app


class FakeLineSession:
    """line_endpoint 找到的通话（LiveSession 的替身）：记下 attach_line 收到的 StartMsg，回一条 mark 作为确认。"""

    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self.attached: list[StartMsg] = []

    async def attach_line(self, websocket: Any, start: StartMsg) -> None:
        self.attached.append(start)
        await websocket.send_text(json.dumps({"event": "mark", "streamSid": start.streamSid, "mark": {"name": "attached"}}))


class FakeHandoffSession:
    """handoff_endpoint 找到的通话（CallSession 的替身）：attach_handoff 回一条 ready 后返回。

    E-G-6：连接已由端点 accept，session 不再 accept。D-G-5：端点在 attach_handoff 返回后看 status().phase，
    仍是 in_handoff 才计时自动接回；这里 attach_handoff 返回后 phase 变成 phase_after（默认 resuming，
    即「用户点了 Return」），return_to_agent 在没有转接时抛 ToolFailure("no_active_handoff")。"""

    def __init__(self, company_name: str = COMPANY_NAME, *, phase_after: str = "resuming") -> None:
        self.company_name = company_name
        self.phase = "handoff_ready"
        self.phase_after = phase_after
        self.attached: list[str] = []
        self.returned: list[str] = []

    async def attach_handoff(self, websocket: Any, handoff_id: str) -> None:
        self.attached.append(handoff_id)
        self.phase = "in_handoff"
        await websocket.send_json(
            {
                "type": "ready",
                "company_name": self.company_name,
                "media": {"ai_media": "disconnected", "recording": False, "transcription": False},
            }
        )
        self.phase = self.phase_after

    def status(self, debug: bool = False) -> Any:
        return SimpleNamespace(phase=self.phase)

    async def return_to_agent(self, handoff_id: str) -> Any:
        self.returned.append(handoff_id)
        if self.phase != "in_handoff":
            raise ToolFailure("no_active_handoff")
        self.phase = "resuming"
        return None


# ---------------------------------------------------------------------------
# 一通完整的 LiveCall（只替换 transport 与 Sonic；其余全是 D 线真实组件）
# ---------------------------------------------------------------------------


class _LineIn(FrameProcessor):
    """transport.input() 的替身：测试经 feed() 推入运营商一侧的音频；StartFrame 到达即视为管线已启动。"""

    def __init__(self) -> None:
        super().__init__(name="fake-line-in")
        self.started = False
        self.upstream: list[Frame] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == UP:
            self.upstream.append(frame)
        elif isinstance(frame, StartFrame):
            self.started = True
        await self.push_frame(frame, direction)

    async def feed(self, frame: Frame) -> None:
        await self.push_frame(frame, DOWN)


class _LineOut(FrameProcessor):
    """transport.output() 的替身：记下送往线路的每一帧（下行）。"""

    def __init__(self) -> None:
        super().__init__(name="fake-line-out")
        self.frames: list[Frame] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == DOWN:
            self.frames.append(frame)
        await self.push_frame(frame, direction)

    def tts_audio(self) -> list[TTSAudioRawFrame]:
        return [f for f in self.frames if isinstance(f, TTSAudioRawFrame)]

    def dtmf(self) -> list[Any]:
        return [f for f in self.frames if isinstance(f, (OutputDTMFFrame, OutputDTMFUrgentFrame))]

    def user_audio(self, rate: int) -> list[OutputAudioRawFrame]:
        return [
            f
            for f in self.frames
            if isinstance(f, OutputAudioRawFrame) and not isinstance(f, TTSAudioRawFrame) and f.sample_rate == rate
        ]


class FakeLineTransport:
    """pipeline.build_transport 的替身。"""

    def __init__(self) -> None:
        self._in = _LineIn()
        self._out = _LineOut()

    def input(self) -> _LineIn:
        return self._in

    def output(self) -> _LineOut:
        return self._out


class LiveCallHarness:
    """一通 live-sim 电话：LiveCall + FakeCarrier（经 CarrierClient）+ FakeSonicService + 线路 transport 替身。

    替换点只有骨架的两个公开构造函数：`pipeline.build_transport` 与 `pipeline.build_sonic_service`
    （tech §7.1 AC-65：LiveCall 建管线时调 build_sonic_service(compiled, settings)）。LiveCall.attach_line
    要经这两个函数构造 transport 和 Sonic。其余（LineTokens、HandoffTokens、TurnDetector、OutputGuard、
    SonicTextInjector、LiveActionBackend、MediaGate、HandoffBridge、TranscriptTap）都是 D 线真实组件。
    时间：D 线的计时全部经 deps.clock（FakeClock）；每送一帧推进 20 ms。
    """

    def __init__(
        self,
        *,
        monkeypatch: Any,
        settings: Any,
        spec: m.CallSpec,
        clock: Any,
        trace: FakeTrace,
        gate: FakeGate,
        sonic: Any,
        carrier: Any,
        store: Any,
    ) -> None:
        from waitless.call import live as live_mod
        from waitless.call import pipeline as pipeline_mod
        from waitless.call.handoff import HandoffTokens
        from waitless.call.line_ws import LineTokens
        from waitless.call.live import CarrierClient, LiveCall

        self.settings = settings
        self.spec = spec
        self.clock = clock
        self.trace = trace
        self.gate = gate
        self.sonic = sonic
        self.carrier = carrier
        self.rate = settings.engine.audio.pipeline_in_sample_rate
        self.transport = FakeLineTransport()
        self.transport_builds: list[Any] = []
        self.sonic_builds: list[Any] = []

        def build_transport(websocket: Any, serializer: Any, settings_: Any) -> FakeLineTransport:
            self.transport_builds.append(serializer)
            return self.transport

        def build_sonic_service(compiled: Any, settings_: Any) -> Any:
            self.sonic_builds.append(compiled)
            return sonic

        for mod in (pipeline_mod, live_mod):
            monkeypatch.setattr(mod, "build_transport", build_transport, raising=False)
            monkeypatch.setattr(mod, "build_sonic_service", build_sonic_service, raising=False)

        self.line_tokens = LineTokens(clock)
        self.handoff_tokens = HandoffTokens(clock, ttl_s=settings.engine.handoff.token_ttl_s)
        self.on_ended = OnEnded()
        self.deps = make_deps(
            settings=settings,
            trace=trace,
            clock=clock,
            gate=gate,
            store=store,
            line_tokens=self.line_tokens,
            handoff_tokens=self.handoff_tokens,
            on_ended=self.on_ended,
        )
        self.carrier_client = CarrierClient(base_url=settings.carrier_url, http=carrier.transport)
        self.call = LiveCall(spec, self.deps, carrier=self.carrier_client)
        self.services = AppServices(
            settings=settings,
            store=store,
            trace=trace,
            clock=clock,
            calls=FakeRegistry({spec.call_id: self.call}),
            line_tokens=self.line_tokens,
            handoff_tokens=self.handoff_tokens,
        )
        self.line_ws: AsgiWs | None = None
        self.line_task: asyncio.Task[Any] | None = None
        self.handoff_ws: AsgiWs | None = None
        self.handoff_task: asyncio.Task[Any] | None = None
        self.start_msg: StartMsg | None = None

    # -- 拨号与接线 -------------------------------------------------------------
    @property
    def dial(self) -> m.CarrierDialIn:
        return self.carrier.dials[-1]

    @property
    def call_sid(self) -> str:
        sids = [sid for sid, d in self.carrier.call_sids.items() if d.call_id == self.spec.call_id]
        return sids[-1]

    async def start(self) -> None:
        await self.call.start()

    async def connect_line(self) -> None:
        msgs = self.carrier.line_messages(self.dial, self.call_sid, include_stop=False)
        self.start_msg = StartMsg.model_validate(msgs[1])
        self.line_ws = AsgiWs(
            path=line_ws_path(self.settings), query={"token": self.dial.stream_token}, services=self.services
        )
        await self.line_ws.websocket.accept()  # line_endpoint 已 accept 并读完 connected、start
        self.line_task = asyncio.create_task(self.call.attach_line(self.line_ws.websocket, self.start_msg))
        await wait_until(lambda: self.transport.input().started or self.line_task.done(), what="pipeline start")
        if self.line_task.done():
            self.line_task.result()  # 把 attach_line 的异常抛出来
            raise AssertionError("attach_line returned before the pipeline started")

    # -- 线路音频（每帧推进 20 ms） --------------------------------------------
    async def frame(self, pcm: bytes) -> None:
        await self.clock.advance(STEP_MS)
        await self.transport.input().feed(in_frame(pcm, self.rate))
        await settle()

    async def speech(self, ms: int) -> None:
        for _ in range(ms // STEP_MS):
            await self.frame(tone_pcm16(STEP_MS, self.rate))

    async def silence(self, ms: int, *, on_frame: Callable[[int], None] | None = None) -> None:
        elapsed = 0
        while elapsed < ms:
            await self.frame(zero_pcm16(STEP_MS, self.rate))
            elapsed += STEP_MS
            if on_frame is not None:
                on_frame(elapsed)

    async def company_says(
        self,
        text: str,
        *,
        speech_ms: int = 1000,
        final_after_ms: int = 300,
        silence_ms: int = 900,
        stop_reason: str = "END_TURN",
        on_frame: Callable[[int], None] | None = None,
    ) -> None:
        """对方说一段话：有声 speech_ms → 静音；静音满 final_after_ms 时 provider 发出 USER FINAL。"""
        await self.speech(speech_ms)
        elapsed = 0
        said = False
        while elapsed < silence_ms:
            await self.frame(zero_pcm16(STEP_MS, self.rate))
            elapsed += STEP_MS
            if not said and elapsed >= final_after_ms:
                await self.sonic.emit_user_text(text, stop_reason=stop_reason)
                said = True
                await settle()
            if on_frame is not None:
                on_frame(elapsed)

    async def sonic_plays_pending(self) -> int:
        n = await self.sonic.play_pending()
        await settle(200)
        return n

    async def sonic_says(self, response: Any) -> int:
        n = await self.sonic.emit_response(response)
        await settle(200)
        return n

    # -- 转接 WebSocket ---------------------------------------------------------
    async def open_handoff(self, join: m.JoinCallOut, *, hello: Mapping[str, Any] = HANDOFF_HELLO) -> AsgiWs:
        from waitless.call.handoff import handoff_endpoint

        ws = AsgiWs(path=join.ws_path, query={"token": join.token}, services=self.services)
        self.handoff_ws = ws
        self.handoff_task = asyncio.create_task(handoff_endpoint(ws.websocket, join.handoff_id))
        ws.json(dict(hello))
        await wait_until(
            lambda: any(isinstance(j, dict) and j.get("type") == "ready" for j in ws.sent_json()) or self.handoff_task.done(),
            what="handoff ready frame",
        )
        if self.handoff_task.done():
            self.handoff_task.result()
        return ws

    # -- 收尾 -------------------------------------------------------------------
    async def close(self) -> None:
        try:
            if not self.call.ended:
                await self.call.hangup("test_cleanup")
        except Exception:  # noqa: BLE001 - 收尾不掩盖测试本身的结论
            pass
        await settle()
        if self.handoff_task is not None and not self.handoff_task.done():
            self.handoff_task.cancel()  # 端点可能还在 FakeClock 上等 resume_grace_ms；收尾时不再等
        if self.line_task is not None and not self.line_task.done():
            try:
                await asyncio.wait_for(self.line_task, 3.0)  # 看门狗（真实时间）：挂断后管线应当结束
            except BaseException:  # noqa: BLE001
                self.line_task.cancel()
        try:
            await self.carrier_client.aclose()
        except Exception:  # noqa: BLE001
            pass
        # 防挂死：测试中途失败时，管线（WorkerRunner）若还在跑，pytest 收尾会一直等。把本测试留下的任务全部取消。
        current = asyncio.current_task()
        leftovers = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        for t in leftovers:
            t.cancel()
        if leftovers:
            await asyncio.wait(leftovers, timeout=3.0)


async def drive(coro: Any, clock: Any, *, limit_ms: int = 10_000) -> Any:
    """跑一个可能在 deps.clock 上等待的协程：每轮推进 FakeClock 20 ms，直到它结束（上限 limit_ms 假时间）。"""
    task = asyncio.ensure_future(coro)
    elapsed = 0
    await settle()
    while not task.done():
        if elapsed >= limit_ms:
            task.cancel()
            raise AssertionError(f"coroutine still waiting after {limit_ms} ms of fake time")
        await clock.advance(STEP_MS)
        elapsed += STEP_MS
        await settle()
    return task.result()


async def run_for(coro: Any, clock: Any, *, ms: int = 2_000) -> "asyncio.Future[Any]":
    """跑一个协程 ms 毫秒假时间（每轮推进 20 ms），不要求它结束；返回任务。用于「执行器可以等后续事件，
    也可以立即返回」都合规的场合（例如 say 之后等 ASSISTANT FINAL 做 DP-C2）。调用方负责 cancel。"""
    task = asyncio.ensure_future(coro)
    elapsed = 0
    await settle()
    while not task.done() and elapsed < ms:
        await clock.advance(STEP_MS)
        elapsed += STEP_MS
        await settle()
    if task.done() and task.exception() is not None:
        raise task.exception()  # type: ignore[misc]
    return task


async def finish_or_cancel(task: "asyncio.Future[Any]") -> Any:
    if task.done():
        return task.result()
    task.cancel()
    try:
        await task
    except BaseException:  # noqa: BLE001
        pass
    return None


class MiniCall:
    """不带线路与 LiveCall 的一段通话链：FakeSonicService →(sink) OutputGuard → 下游记录；
    SonicTextInjector、LiveActionBackend、TranscriptTap 用 D 线真实实现；MediaGate、pipeline_task、运营商用替身。

    run(result)：执行一个决定，并像真实 Sonic 那样在每次注入后播出脚本里的下一段回应，直到安静 quiet_ms。"""

    def __init__(self, *, spec: m.CallSpec, deps: CallDeps, engine: m.EngineConfig, make_sonic: Callable[..., Any], responses: Sequence[Any] = ()) -> None:
        from waitless.call.actions import LiveActionBackend
        from waitless.call.injector import SonicTextInjector
        from waitless.call.live import TranscriptTap
        from waitless.call.output_guard import OutputGuard

        self.spec = spec
        self.deps = deps
        self.clock = deps.clock
        self.trace = deps.trace
        self.guard = OutputGuard(
            stall_phrases=spec.compiled.stall_phrases,
            held_response_max_ms=engine.guard.held_response_max_ms,
            trace=deps.trace,
            call_id=spec.call_id,
            task_id=spec.task_id,
            clock=deps.clock,
        )
        self.chain = Chain(self.guard)
        self.sonic = make_sonic(sink=self.chain.push, responses_on_inject=list(responses))
        self.injector = SonicTextInjector(
            self.sonic, trace=deps.trace, call_id=spec.call_id, task_id=spec.task_id, clock=deps.clock
        )
        self.media_gate = FakeMediaGate()
        self.pipeline_task = FakePipelineTask(forward=self.chain.push)
        self.carrier = FakeCarrierApi()
        self.call_sid = TEST_CALL_SID
        self.actions = LiveActionBackend(
            spec=spec,
            deps=deps,
            injector=self.injector,
            output_guard=self.guard,
            media_gate=self.media_gate,
            pipeline_task=self.pipeline_task,
            carrier=self.carrier,
            call_sid=self.call_sid,  # D-G-1
        )
        self.turn_detector = FakeTurnDetector()
        self.tap = TranscriptTap(
            deps=deps, spec=spec, turn_detector=self.turn_detector, actions=self.actions, media_gate=self.media_gate
        )
        self.sonic.add_text_listener(self.tap.on_provider_text)

    async def __aenter__(self) -> "MiniCall":
        await self.chain.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.chain.stop()

    def line_audio(self) -> list[TTSAudioRawFrame]:
        return self.chain.tail.tts_audio()

    def sent_injections(self) -> list[str]:
        """Sonic 实际收到的注入文字（去掉前缀）。"""
        return [t.removeprefix(m.SONIC_INSTRUCTION_PREFIX) for t in self.sonic.sent_texts]

    async def run(self, result: m.DecisionResult, *, limit_ms: int = 15_000, quiet_ms: int = 2_000) -> m.ActionRecord:
        task = asyncio.ensure_future(self.actions.execute(result))
        elapsed = 0
        quiet = 0
        while elapsed < limit_ms:
            await settle()
            played = await self.sonic.play_pending()
            await settle()
            quiet = quiet + STEP_MS if (task.done() and played == 0) else 0
            if quiet >= quiet_ms:
                break
            await self.clock.advance(STEP_MS)
            elapsed += STEP_MS
        if not task.done():
            task.cancel()
            raise AssertionError(f"execute() still running after {limit_ms} ms of fake time")
        return task.result()


def handoff_round(call_phase: str = "live_rep") -> m.DecisionResult:
    """坐席要验证身份时 G 线给出的转接决定（§8 第 7 步）。"""
    return make_result("handoff", intent="request_handoff", call_phase=call_phase)


ASK_DOB_TEXT = "To finish setting this up I need to verify the account holder's date of birth."


def handoff_sonic(make_fake_sonic: Callable[..., Any], *extra: Any) -> Any:
    """Sonic 替身：对 announce 注入回一段话，对 resume 注入再回一段话（之后按 extra 继续）。"""
    from fakes import SonicResponse

    responses = [
        SonicResponse(text=SAY_TEMPLATES["handoff.announce"], audio_ms=400, chunk_ms=40),
        SonicResponse(text="Thanks for waiting. Let's continue with the service start.", audio_ms=400, chunk_ms=40),
        *extra,
    ]
    return make_fake_sonic(responses_on_inject=responses)


async def reach_handoff_ready(h: LiveCallHarness, engine: m.EngineConfig) -> None:
    """§8 第 7 步：坐席要出生日期 → G 判 handoff → 注入 announce → Sonic 说完 → 空闲满 announce_idle_ms。"""
    h.gate.round_script.append(handoff_round())
    await h.start()
    await h.connect_line()
    await h.company_says(ASK_DOB_TEXT, speech_ms=1000, final_after_ms=300,
                         silence_ms=engine.turns.turn_end_silence_ms + 200)
    await wait_until(lambda: h.sonic.injections, what="the announce injection")
    await h.sonic_plays_pending()
    await h.silence(engine.handoff.announce_idle_ms + 200)


async def join_handoff(h: LiveCallHarness) -> tuple[m.JoinCallOut, AsgiWs]:
    join = await h.call.join()
    ws = await h.open_handoff(join)
    return join, ws


HANDOFF_EVENT_ORDER = ("handoff_announced", "handoff_ready", "handoff_started", "handoff_ended")  # D-G-4
# 主会话转达的 Jev 定稿 no_trace_in_handoff（verdicts/G3a-r4-D-guard-handoff.json）：handoff 状态下 OutputGuard 丢弃 Sonic
# 输出不写 trace，只在内存计数；handoff_ended 事件的 payload（TraceEvent.action）带丢弃次数。
DROPPED_SONIC_OUTPUTS = "dropped_sonic_outputs"


def dropped_counts(trace: FakeTrace, call_id: str) -> list[Any]:
    """每次 handoff_ended 的 action.dropped_sonic_outputs（按事件顺序）。一次「丢弃」= Sonic 的一段回应。"""
    return [(e.action or {}).get(DROPPED_SONIC_OUTPUTS) for e in trace.of_type("handoff_ended", call_id=call_id)]


def first_seq(trace: FakeTrace, call_id: str, event_type: str) -> int:
    evs = trace.of_type(event_type, call_id=call_id)
    if not evs:
        raise AssertionError(f"no {event_type} in trace: {trace.types(call_id)}")
    return int(evs[0].seq)


def window(trace: FakeTrace, call_id: str, start_type: str, end_type: str) -> list[m.TraceEvent]:
    """start_type 第一次出现与其后 end_type 第一次出现之间（不含两端）的事件。"""
    evs = trace.events(call_id=call_id)
    starts = [i for i, e in enumerate(evs) if e.event_type == start_type]
    if not starts:
        raise AssertionError(f"no {start_type} event in trace: {[e.event_type for e in evs]}")
    i = starts[0]
    ends = [j for j, e in enumerate(evs) if j > i and e.event_type == end_type]
    if not ends:
        raise AssertionError(f"no {end_type} after {start_type}: {[e.event_type for e in evs]}")
    return evs[i + 1 : ends[0]]


__all__ = [name for name in dir() if not name.startswith("_")]
