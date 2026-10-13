"""E 线单元测试的进程内替身（第 3 步写，冻结）。

每个替身按 waitless.interfaces 里冻结的 Protocol 实现，只记录被调用的情况、按脚本应答；
测试断言的是这些替身「从外面看到」的东西：公司收到的按键与话、决策门收到的 CompanyTurn、
trace 里的事件、待决选项登记收到的参数、令牌签发记录、CallSession 公开方法的返回值。

不导入 tests/fakes（见包说明）；需要 FakeClock 的测试自己从 conftest 的 fake_clock fixture 拿，
这里的 SimpleClock 只给 AC-87 的子进程探针用。
"""

import asyncio
import itertools
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import ConfigDict

from waitless import models as m
from waitless.config import Settings  # A-27：r2 起 Settings 定义在 config.py；r2 之前 config 从 models 导入同一个类
from waitless.decision.registry import RegistryError
from waitless.interfaces import CallDeps, ToolFailure

# 真实时间兜底：只为了实现误用真实计时（asyncio.sleep、wait_for）时让测试失败而不是挂住；不是业务等待。
REAL_GUARD_S = 20.0


# ---------------------------------------------------------------------------
# 时钟
# ---------------------------------------------------------------------------


def _now(clock: Any) -> datetime:
    return datetime.fromisoformat(clock.now_iso())


class SimpleClock:
    """子进程探针用的时钟（实现 interfaces.Clock）：sleep 立即返回，并把内部时间推进相应毫秒。"""

    def __init__(self, start_wall: datetime | None = None) -> None:
        self._ms = 0
        self._wall0 = start_wall or datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        self.sleep_calls: list[float] = []

    def monotonic_ms(self) -> int:
        return self._ms

    def now(self) -> datetime:
        return self._wall0 + timedelta(milliseconds=self._ms)

    def now_iso(self) -> str:
        return self.now().isoformat()

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self._ms += max(0, int(round(seconds * 1000)))
        await asyncio.sleep(0)

    async def advance(self, ms: int) -> None:
        self._ms += ms
        for _ in range(5):
            await asyncio.sleep(0)


async def run_until(
    clock: Any,
    predicate: Callable[[], bool],
    *,
    step_ms: int = 100,
    max_ms: int = 120_000,
    what: str = "condition",
) -> None:
    """按假时钟一步步推进，直到 predicate() 为真；超过 max_ms 的假时间就判失败。"""

    async def loop() -> None:
        waited = 0
        await clock.advance(0)
        while not predicate():
            if waited >= max_ms:
                raise AssertionError(f"{what} not reached after {max_ms} ms of fake time")
            await clock.advance(step_ms)
            waited += step_ms

    await asyncio.wait_for(loop(), timeout=REAL_GUARD_S)


# ---------------------------------------------------------------------------
# trace、档案存储
# ---------------------------------------------------------------------------


class TraceWriteRejected(ValueError, AssertionError):
    """MemoryTrace 拒绝坏事件时抛它：与真实 Trace.event 一样是 ValueError（r2、缺口 A-G-11），
    同时是 AssertionError，被测代码即使捕获 ValueError 也逃不过测试（r3 第二段按 r2_signature_drift_E 修正）。"""


class MemoryTrace:
    """实现 interfaces.Trace：只在内存里按顺序记事件（写入时按 TraceEvent 校验）。

    写入检查与 interfaces.Trace.event 一致（A-25、A-08）：有后果事件要 decision_id；decision 事件要
    DECISION_EVENT_REQUIRED；call_round 的 decision 事件 action 要有整数 turn_seq。被拒的事件记在 rejected 里。
    """

    def __init__(self, clock: Any) -> None:
        self._clock = clock
        self._events: list[m.TraceEvent] = []
        self._seq = itertools.count(0)
        self.rejected: list[tuple[m.TraceEvent, str]] = []

    def event(self, ev: m.TraceEvent | Mapping[str, Any]) -> int:
        if isinstance(ev, Mapping):
            ev = m.TraceEvent.model_validate(ev)
        if not isinstance(ev, m.TraceEvent):
            raise AssertionError(f"Trace.event expects a TraceEvent, got {type(ev).__name__}")
        problem = None
        if ev.event_type in m.CONSEQUENTIAL_EVENTS and not ev.decision_id:
            problem = f"{ev.event_type} written without decision_id (§6.11, A-25)"
        elif ev.event_type == "decision":
            missing = [n for n in m.DECISION_EVENT_REQUIRED if getattr(ev, n) is None]
            turn_seq = (ev.action or {}).get(m.TURN_SEQ_KEY)
            if missing:
                problem = f"decision event written without {missing} (§6.11, A-25)"
            elif ev.decision_point == "call_round" and (not isinstance(turn_seq, int) or isinstance(turn_seq, bool)):
                problem = "call_round decision event written without action.turn_seq (A-08)"
        if problem is not None:
            self.rejected.append((ev, problem))
            raise TraceWriteRejected(problem)
        seq = next(self._seq)
        data = ev.model_dump()
        data["seq"] = seq
        if data.get("t_ms") is None:
            data["t_ms"] = self._clock.monotonic_ms()
        self._events.append(m.TraceEvent.model_validate(data))
        return seq

    def has_decision(self, decision_id: str) -> bool:
        return any(e.event_type == "decision" and e.decision_id == decision_id for e in self._events)

    def events(self, *, call_id: str | None = None, task_id: str | None = None) -> list[m.TraceEvent]:
        return [
            e
            for e in self._events
            if (call_id is None or e.call_id == call_id) and (task_id is None or e.task_id == task_id)
        ]

    # -- 测试辅助 -----------------------------------------------------------
    @property
    def all(self) -> list[m.TraceEvent]:
        return list(self._events)

    def of_type(self, *types: str) -> list[m.TraceEvent]:
        return [e for e in self._events if e.event_type in types]

    def decision_ids(self) -> set[str]:
        return {e.decision_id for e in self._events if e.event_type == "decision" and e.decision_id}


class DecoyProfile(m.Profile):
    """绕过 Store 的「夹具档案」（AC-107）：可代说字段之外还带诱饵 ssn、date_of_birth、card_number。

    它仍是 models.Profile 的实例；model_dump()、model_dump_json() 都会带出诱饵键，
    只有按白名单取字段的实现才不会把它们带进模型输入。
    """

    model_config = ConfigDict(extra="allow")


class StubStore:
    """实现 interfaces.Store 的跨线部分：档案、常设规则、偏好都由测试给定，只记录写入。"""

    def __init__(
        self,
        profile: m.Profile | None = None,
        rules: Sequence[m.StandingRule] = (),
        prefs: Sequence[m.Preference] = (),
    ) -> None:
        self.profile = profile if profile is not None else m.Profile()
        self.rules = list(rules)
        self.prefs = list(prefs)
        self.saved_profiles: list[dict[str, str]] = []
        self.saved_company_ids: list[str | None] = []
        self.added_prefs: list[m.Preference] = []

    def save_profile(self, fields: Mapping[str, str], *, company_id: str | None = None) -> m.Profile:
        """签名同 interfaces.Store.save_profile（r2 起带 company_id，A-04）。"""
        self.saved_profiles.append(dict(fields))
        self.saved_company_ids.append(company_id)
        return self.profile

    def get_profile(self) -> m.Profile:
        return self.profile

    def list_standing_rules(self) -> list[m.StandingRule]:
        return list(self.rules)

    def list_preferences(self, company_id: str) -> list[m.Preference]:
        return [p for p in self.prefs if p.company_id == company_id]

    def add_preference(self, pref: m.Preference) -> None:
        self.added_prefs.append(pref)


# ---------------------------------------------------------------------------
# 待决选项、L3、令牌
# ---------------------------------------------------------------------------


class StubPending:
    """实现 interfaces.PendingRegistry（按 decision/pending.py 文档的语义），并记录 create 的全部参数。"""

    def __init__(self, clock: Any) -> None:
        self._clock = clock
        self._items: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self.created: list[dict[str, Any]] = []
        self.answered: list[tuple[str, str]] = []

    def create(
        self,
        *,
        call_id: str,
        decision_id: str,
        question_text: str,
        options: Sequence[m.Option],
        timeout_s: float,
    ) -> m.PendingChoice:
        now = _now(self._clock)
        opts = [o if isinstance(o, m.Option) else m.Option.model_validate(o) for o in options]
        choice = m.PendingChoice(
            choice_id=f"choice-{next(self._ids)}",
            question_text=question_text,
            options=opts,
            created_at=now,
            expires_at=now + timedelta(seconds=float(timeout_s)),
            status="open",
        )
        self._items[choice.choice_id] = {"choice": choice, "call_id": call_id, "decision_id": decision_id}
        self.created.append(
            {
                "call_id": call_id,
                "decision_id": decision_id,
                "question_text": question_text,
                "options": opts,
                "timeout_s": float(timeout_s),
                "choice": choice,
            }
        )
        return choice

    def get(self, choice_id: str) -> m.PendingChoice | None:
        item = self._items.get(choice_id)
        return item["choice"] if item else None

    def current(self, call_id: str) -> m.PendingChoice | None:
        for item in self._items.values():
            if item["call_id"] == call_id and item["choice"].status == "open":
                return item["choice"]
        return None

    def answer(self, choice_id: str, option_id: str) -> m.PendingChoice:
        item = self._items.get(choice_id)
        if item is None:
            raise ToolFailure("unknown_choice", choice_id)
        choice: m.PendingChoice = item["choice"]
        if option_id not in [o.id for o in choice.options]:
            raise ToolFailure("invalid_option", option_id)
        if choice.status == "expired" or _now(self._clock) >= choice.expires_at:
            item["choice"] = choice.model_copy(update={"status": "expired"})
            return item["choice"]
        item["choice"] = choice.model_copy(update={"status": "answered"})
        self.answered.append((choice_id, option_id))
        return item["choice"]

    def expire_due(self) -> list[m.PendingChoice]:
        now = _now(self._clock)
        out: list[m.PendingChoice] = []
        for item in self._items.values():
            choice = item["choice"]
            if choice.status == "open" and now >= choice.expires_at:
                item["choice"] = choice.model_copy(update={"status": "expired"})
                out.append(item["choice"])
        return out

    def decision_of(self, choice_id: str) -> str | None:
        item = self._items.get(choice_id)
        return item["decision_id"] if item else None


class StubLlm:
    """实现 interfaces.LlmTasks。只有 L3 按脚本返回；其余方法给出无害的固定值并记录调用。"""

    def __init__(
        self,
        *,
        offer_options: Sequence[m.OfferOption | Mapping[str, str]] = (),
        offer_error: BaseException | None = None,
        spoken_text: str = "Thanks, one moment.",
    ) -> None:
        self.offer_options = [o if isinstance(o, m.OfferOption) else m.OfferOption.model_validate(o) for o in offer_options]
        self.offer_error = offer_error
        self.spoken_text = spoken_text
        self.offer_calls: list[str] = []
        self.other_calls: list[tuple[str, Any]] = []

    async def extract_offer_options(self, company_text: str) -> list[m.OfferOption]:
        self.offer_calls.append(company_text)
        if self.offer_error is not None:
            raise self.offer_error
        return list(self.offer_options)

    async def extract_request(self, user_text: str) -> m.ExtractedRequest:
        self.other_calls.append(("extract_request", user_text))
        return m.ExtractedRequest()

    async def phrase_reply(self, *, skill_body: str, decision: Any, tool_result: Any, user_text: str | None) -> str:
        self.other_calls.append(("phrase_reply", user_text))
        return self.spoken_text

    async def extract_result_items(self, transcript: Sequence[m.CallTranscriptLine]) -> m.ResultItems:
        self.other_calls.append(("extract_result_items", len(transcript)))
        return m.ResultItems(confirmation_numbers=[], statements=[])

    async def write_summary(self, *, result: Any, transcript: Sequence[m.CallTranscriptLine]) -> str:
        self.other_calls.append(("write_summary", len(transcript)))
        return "Summary."


class StubHandoffTokens:
    """实现 interfaces.HandoffTokenIssuer：一次性令牌，记录每次签发。"""

    def __init__(self, clock: Any, *, ttl_s: int = 120) -> None:
        self._clock = clock
        self._ttl_s = ttl_s
        self._live: dict[tuple[str, str], str] = {}
        self.issued: list[dict[str, str]] = []

    def issue(self, call_id: str, handoff_id: str) -> tuple[str, str]:
        token = f"handoff-token-{len(self.issued) + 1}"
        expires_at = (_now(self._clock) + timedelta(seconds=self._ttl_s)).isoformat()
        self._live[(handoff_id, token)] = call_id
        self.issued.append({"call_id": call_id, "handoff_id": handoff_id, "token": token, "expires_at": expires_at})
        return token, expires_at

    def consume(self, handoff_id: str, token: str) -> str | None:
        return self._live.pop((handoff_id, token), None)


class StubLineTokens:
    """实现 interfaces.LineTokenIssuer。text 通话没有 /ws/line，签发记录应当一直为空（AC-87）。"""

    def __init__(self) -> None:
        self.issued: list[str] = []

    def issue(self, call_id: str) -> str:
        self.issued.append(call_id)
        return f"line-token-{len(self.issued)}"

    def verify(self, token: str) -> str | None:
        return None


# ---------------------------------------------------------------------------
# 文本公司
# ---------------------------------------------------------------------------

_step_ids = itertools.count(1)


def line(
    text: str,
    *,
    kind: str = "speech",
    role: str = "agent",
    step_id: str | None = None,
    expect: Mapping[str, Any] | None = None,
    confirmation_number: str | None = None,
) -> m.CompanyLine:
    return m.CompanyLine(
        step_id=step_id or f"step-{next(_step_ids)}",
        kind=kind,  # type: ignore[arg-type]
        role=role,  # type: ignore[arg-type]
        text=text,
        expect=m.EvalExpect.model_validate(expect) if expect is not None else None,
        confirmation_number=confirmation_number,
    )


@dataclass
class When:
    """一条反应规则：kind 是 digits / speech / handoff / timeout；match 为 None 表示任意输入，
    str 表示子串，bool 表示 handoff 的 joined，可调用对象 f(value)->bool。每条规则只触发一次。"""

    kind: str
    match: Any = None
    lines: Sequence[m.CompanyLine] = ()
    used: bool = False

    def hits(self, kind: str, value: Any) -> bool:
        if self.used or kind != self.kind:
            return False
        if self.match is None:
            return True
        if isinstance(self.match, bool):
            return value is self.match
        if isinstance(self.match, str):
            return isinstance(value, str) and self.match in value
        return bool(self.match(value))


def queue_reactions(*rules: When) -> Callable[[str, Any, "ScriptedCompany"], list[m.CompanyLine]]:
    """按顺序找第一条命中且没用过的规则，说出它的台词；没有命中就不说话。"""

    def react(kind: str, value: Any, company: "ScriptedCompany") -> list[m.CompanyLine]:
        for rule in rules:
            if rule.hits(kind, value):
                rule.used = True
                return list(rule.lines)
        return []

    return react


class ScriptedCompany:
    """实现 interfaces.CompanyEngine：开场说 start_lines，之后按 reactions 对输入作出反应。

    inputs 按顺序记下收到的每个输入：("start", None)、("digits", "1")、("speech", "...")、
    ("handoff", True/False)、("timeout", None)。emitted 是说过的全部台词。
    """

    def __init__(
        self,
        start_lines: Sequence[m.CompanyLine],
        reactions: Callable[[str, Any, "ScriptedCompany"], list[m.CompanyLine]] | None = None,
        *,
        scenario_id: str = "stub-scenario",
    ) -> None:
        self._scenario_id = scenario_id
        self._start = list(start_lines)
        self._react = reactions or (lambda kind, value, company: [])
        self.inputs: list[tuple[str, Any]] = []
        self.emitted: list[m.CompanyLine] = []
        self._ended = False

    @property
    def scenario_id(self) -> str:
        return self._scenario_id

    def start(self) -> list[m.CompanyLine]:
        self.inputs.append(("start", None))
        return self._emit(self._start)

    def on_digits(self, digits: str) -> list[m.CompanyLine]:
        self.inputs.append(("digits", digits))
        return self._emit(self._react("digits", digits, self))

    def on_speech(self, text: str) -> list[m.CompanyLine]:
        self.inputs.append(("speech", text))
        return self._emit(self._react("speech", text, self))

    def on_timeout(self) -> list[m.CompanyLine]:
        self.inputs.append(("timeout", None))
        return self._emit(self._react("timeout", None, self))

    def on_handoff(self, joined: bool) -> list[m.CompanyLine]:
        self.inputs.append(("handoff", joined))
        return self._emit(self._react("handoff", joined, self))

    @property
    def ended(self) -> bool:
        return self._ended

    def step_log(self) -> list[str]:
        return [ln.step_id for ln in self.emitted]

    # -- 测试辅助 -----------------------------------------------------------
    def heard(self) -> list[str]:
        return [v for k, v in self.inputs if k == "speech"]

    def digits(self) -> list[str]:
        return [v for k, v in self.inputs if k == "digits"]

    def handoffs(self) -> list[bool]:
        return [v for k, v in self.inputs if k == "handoff"]

    def _emit(self, lines: Sequence[m.CompanyLine]) -> list[m.CompanyLine]:
        out = list(lines)
        for ln in out:
            if ln.kind == "hangup":
                self._ended = True
        self.emitted.extend(out)
        return out


# ---------------------------------------------------------------------------
# 决策门
# ---------------------------------------------------------------------------


def spec_press(digits: str, intent: str | None = None) -> dict[str, Any]:
    return {"action": "press", "intent": intent or f"menu:{digits}", "digits": digits}


def spec_say(text: str, intent: str = "decline_offer") -> dict[str, Any]:
    return {"action": "say", "intent": intent, "say_text": text}


def spec_release(intent: str = "open_answer") -> dict[str, Any]:
    return {"action": "release", "intent": intent}


def spec_ask_user() -> dict[str, Any]:
    return {"action": "ask_user", "intent": "ask_user", "call_phase": "live_rep"}


def spec_handoff() -> dict[str, Any]:
    return {"action": "handoff", "intent": "request_handoff", "call_phase": "live_rep"}


def spec_end(intent: str = "confirm_and_end") -> dict[str, Any]:
    return {"action": "end", "intent": intent, "call_phase": "live_rep"}


def spec_wait(intent: str = "wait", call_phase: str | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"action": "wait", "intent": intent}
    if call_phase is not None:
        spec["call_phase"] = call_phase
    return spec


class ScriptedGate:
    """实现 interfaces.DecisionGate：每次决策都先写一条完整的 decision 事件再返回（与真实决策门的约定相同）。

    script：[(对方原话里的子串, spec), ...]（取第一条命中的；都不命中返回 wait），或可调用对象 f(turn, ctx) -> spec。
    points：f(point, request) -> spec，给 decide() 用；默认 said_as_decided 返回 wait/said_ok，其余 wait/ok。
    """

    def __init__(
        self,
        trace: Any,
        *,
        script: Sequence[tuple[str, dict[str, Any]]] | Callable[..., dict[str, Any]] = (),
        points: Callable[[str, m.DecisionRequest], dict[str, Any]] | None = None,
        jev_model: str = "stub-jev-model",
    ) -> None:
        self.trace = trace
        self._script = script
        self._points = points
        self.jev_model = jev_model
        self.round_calls: list[tuple[m.CompanyTurn, m.RoundContext]] = []
        self.point_calls: list[tuple[str, m.DecisionRequest]] = []
        self.results: list[m.DecisionResult] = []
        self._ids = itertools.count(1)

    async def decide_round(self, turn: m.CompanyTurn, ctx: m.RoundContext) -> m.DecisionResult:
        self.round_calls.append((turn, ctx))
        if callable(self._script):
            spec = self._script(turn, ctx)
        else:
            spec = next((s for needle, s in self._script if needle in turn.text), spec_wait())
        return self._result(
            "call_round", spec, task_id=ctx.task_id, call_id=ctx.call_id, transcript=ctx.transcript, turn_seq=turn.seq
        )

    async def decide(self, point: str, request: m.DecisionRequest) -> m.DecisionResult:
        self.point_calls.append((point, request))
        if self._points is not None:
            spec = self._points(point, request)
        else:
            spec = spec_wait("said_ok" if point == "said_as_decided" else "ok")
        return self._result(point, spec, task_id=request.task_id, call_id=request.call_id, transcript=request.transcript)

    # -- 测试辅助 -----------------------------------------------------------
    def record(self, spec: Mapping[str, Any], *, task_id: str, call_id: str, point: str = "call_round") -> m.DecisionResult:
        """直接写一条 decision 事件并返回结果（测试直接驱动执行器时用）。"""
        return self._result(
            point, spec, task_id=task_id, call_id=call_id, transcript=[], turn_seq=0 if point == "call_round" else None
        )

    def turns(self) -> list[m.CompanyTurn]:
        return [t for t, _ in self.round_calls]

    def result_for(self, intent: str) -> m.DecisionResult:
        return next(r for r in self.results if r.intent == intent)

    def _result(
        self,
        point: str,
        spec: Mapping[str, Any],
        *,
        task_id: str,
        call_id: str | None,
        transcript: Sequence[m.TranscriptLine],
        turn_seq: int | None = None,
    ) -> m.DecisionResult:
        decision_id = f"dec-{next(self._ids)}"
        lines = [m.TranscriptLine.model_validate(ln.model_dump()) for ln in list(transcript)[-m.JEV_TRANSCRIPT_MAX_LINES :]]
        intent = str(spec["intent"])
        state = m.JevState(
            task_goal="Start electricity service on November 1",
            user_profile={},
            standing_rules=[],
            prior_choices=[],
            transcript=m.JevTranscript(note=m.JEV_TRANSCRIPT_NOTE, lines=lines),
        )
        questions = {"menu_choice": m.JevQuestion(type="choice", instructions="Pick the option.", criteria={intent: "scripted"})}
        response = m.JevResponse(
            answers={"menu_choice": m.JevAnswer(type="choice", choice=intent, confidence=0.95, probabilities={intent: 0.95})}
        )
        jev = m.JevExchange(request=m.JevRequest(model=self.jev_model, state=state, questions=questions), response=response, latency_ms=0)
        result = m.DecisionResult(
            decision_id=decision_id,
            point=point,  # type: ignore[arg-type]
            intent=intent,
            action=spec["action"],
            digits=spec.get("digits"),
            say_text=spec.get("say_text"),
            call_phase=spec.get("call_phase"),
            turn_seq=turn_seq if point == "call_round" else None,  # D-G-2：call_round 的结果带这一轮的 seq
            reason_code=spec.get("reason_code", "scripted"),
            policy_rule=spec.get("policy_rule", "scripted"),
            jev=jev,
        )
        self.trace.event(
            m.TraceEvent(
                task_id=task_id,
                call_id=call_id,
                event_type="decision",
                decision_id=decision_id,
                decision_point=point,  # type: ignore[arg-type]
                jev_request=jev.request.model_dump(mode="json"),
                jev_response=jev.response.model_dump(mode="json"),
                policy_decision={"intent": intent, "action": result.action, "reason_code": result.reason_code},
                action={"turn_seq": turn_seq} if turn_seq is not None else None,
                latency_ms=0,
            )
        )
        self.results.append(result)
        return result


def bare_decision(
    decision_id: str, spec: Mapping[str, Any], *, point: str = "call_round", turn_seq: int | None = 0
) -> m.DecisionResult:
    """一条没有写进 trace 的 DecisionResult（AC-70：执行器必须拒绝它）。call_round 的结果带 turn_seq（D-G-2）。"""
    state = m.JevState(
        task_goal="x",
        user_profile={},
        standing_rules=[],
        prior_choices=[],
        transcript=m.JevTranscript(note=m.JEV_TRANSCRIPT_NOTE, lines=[]),
    )
    intent = str(spec["intent"])
    jev = m.JevExchange(
        request=m.JevRequest(
            model="stub-jev-model",
            state=state,
            questions={"menu_choice": m.JevQuestion(type="choice", instructions="x", criteria={intent: "x"})},
        ),
        response=m.JevResponse(
            answers={"menu_choice": m.JevAnswer(type="choice", choice=intent, confidence=0.95, probabilities={intent: 0.95})}
        ),
        latency_ms=0,
    )
    return m.DecisionResult(
        decision_id=decision_id,
        point=point,  # type: ignore[arg-type]
        intent=intent,
        action=spec["action"],
        digits=spec.get("digits"),
        say_text=spec.get("say_text"),
        call_phase=spec.get("call_phase"),
        turn_seq=turn_seq if point == "call_round" else None,
        reason_code="scripted",
        policy_rule="scripted",
        jev=jev,
    )


# ---------------------------------------------------------------------------
# 任务卡、编译产物、配置
# ---------------------------------------------------------------------------

# 执行器用到的 7 个模板（models.SAY_TEMPLATE_KEYS，歧义表 A-07）。文字只用于断言「公司听到的是哪个模板」。
SAY_TEMPLATES: dict[str, str] = {
    "handoff.announce": "I will now bring the account holder onto the line.",
    "handoff.resume": "Thank you for waiting; the account holder has finished verification.",
    "handoff.ask_user": "One moment please while I check with the account holder.",
    "pending.answer": "The account holder chose {option_label}.",
    "pending.safe_default": "The account holder is not available, so please leave that as is and we will follow up.",
    "end.goodbye": "Thank you for your help. Goodbye.",
    "ask_repeat": "Sorry, could you please repeat that?",
    # A-16 定稿：text 模式的开放式回答按字段模板作答（open_answer.<field>），值取 allowed_fields 或任务卡
    "open_answer.address": "The service address is {address}.",
    "open_answer.start_date": "Please start service on {effective_date}.",
}

# A-16 定稿：lexicons.yaml 的 field_cues —— 对方问句里的提示词 → 字段（compile_call 展平成 "field_cues.<字段>" 键）。
FIELD_CUES: dict[str, list[str]] = {
    "address": ["service address", "what address", "the address"],
    "start_date": ["start date", "what date", "when would you like"],
}


def is_template_utterance(text: str, templates: Mapping[str, str] = SAY_TEMPLATES) -> bool:
    """这句话是不是某个执行器模板（含 pending.answer 填好占位之后的样子）。"""
    for tpl in templates.values():
        head = tpl.split("{", 1)[0]
        if text == tpl or ("{" in tpl and head and text.startswith(head)):
            return True
    return False


def full_policy(*, timeout_sec: int = 20, max_options: int = 3, interrupt_budget: int = 1) -> dict[str, Any]:
    """§6.6 decide-option/policy.yaml 的完整结构（编译产物 CompiledCall.policy 的样子）。"""
    return {
        "version": 1,
        "global": {
            "interrupt_budget": interrupt_budget,
            "auto_answer": {"min_confidence": 0.85},
            "ask_user": {
                "only_if": {"min_stakes": 2, "max_confidence": 0.85},
                "max_options": max_options,
                "timeout_sec": timeout_sec,
                "on_timeout": "safe_default",
            },
            "handoff_always": ["ssn", "card_number", "date_of_birth"],
            "never_claim_to_be_user": True,
            "learn_from_answers": {"enabled": True, "max_stakes": 1},
        },
        "decision_points": {
            "call_round": {
                "consequential": {"min_stakes": 2, "needs_user_threshold": 0.5, "markers_from": "lexicons.consequential"},
                "menu": {"min_confidence": 0.85, "below_threshold": "top_choice", "on_none": "menu_safe_default"},
                "conversation": {"min_confidence": 0.85, "below_threshold": "top_choice"},
                "order": ["identity", "forbidden_info", "menu", "consequential", "standing_rules", "preferences", "confidence"],
                "safe_defaults": {
                    "menu_none": "operator_or_repeat",
                    "ask_user_timeout": "decline_and_follow_up",
                    "budget_exhausted": "end_needs_follow_up",
                    "handoff_not_live_rep": "ask_for_representative",
                    "press_field_refused": "ask_repeat",
                    "end_without_confirmation": "ask_repeat_then_follow_up",
                },
            },
            "said_as_decided": {"min_probability": 0.7, "retries": 1, "on_fail": "safe_default"},
            "call_outcome": {"min_confidence": 0.85, "confirmed_requires": "confirmation_pattern", "below_threshold": "needs_follow_up"},
            "result_item_kind": {"min_confidence": 0.85, "below_threshold": "follow_up"},
        },
    }


def make_task_card(**over: Any) -> m.TaskCard:
    data: dict[str, Any] = {
        "card_version": 1,
        "company": {
            "company_id": "lakeshore-energy",
            "name": "Lakeshore Energy",
            "official_number": "(312) 555-0100",
            "sources": [{"url": "lakeshore-energy.example/contact.html", "title": "Contact"}],
        },
        "goal": "Start electricity service on November 1",
        "task_summary": "Start electricity service at the new address on November 1",
        "effective_date": "2026-11-01",
        "shared_fields": [{"key": "name", "label": "Name", "display": "Alex Example"}],
        "precall_decisions": [
            {
                "question_id": "deposit_or_autopay",
                "question": "Pay a deposit or enroll in autopay?",
                "options": [
                    {"id": "deposit", "label": "Pay a deposit", "detail": "One-time deposit"},
                    {"id": "autopay", "label": "Enroll in autopay", "detail": "Automatic monthly payment"},
                ],
                "default": "deposit",
                "selected": "deposit",
                "needs_user": False,
            }
        ],
        "boundary": "Only start the service; no other account changes.",
    }
    data.update(over)
    return m.TaskCard.model_validate(data)


def make_compiled(
    task_card: m.TaskCard,
    allowed_fields: Mapping[str, str],
    *,
    timeout_sec: int = 20,
    max_options: int = 3,
    say_templates: Mapping[str, str] | None = None,
    stall_phrases: Sequence[str] = ("One moment please.", "Let me check on that."),
    field_cues: Mapping[str, Sequence[str]] | None = None,
    press_patterns: Sequence[str] = (),
    digit_words: Mapping[str, str] | None = None,
) -> m.CompiledCall:
    """单元测试用的 CompiledCall。field_cues 按 A-16 定稿展平成 lexicons["field_cues.<字段>"]（models.FIELD_CUES_PREFIX）。"""
    cues = {k: list(v) for k, v in (field_cues if field_cues is not None else FIELD_CUES).items()}
    return m.CompiledCall(
        sonic_prompt="You are an AI assistant calling Lakeshore Energy on behalf of the account holder.",
        jev_templates={},
        policy=full_policy(timeout_sec=timeout_sec, max_options=max_options),
        ivr_map={},
        intents=[],
        lexicons={f"{m.FIELD_CUES_PREFIX}{k}": v for k, v in cues.items()},
        stall_phrases=list(stall_phrases),
        say_templates=dict(say_templates or SAY_TEMPLATES),
        allowed_fields=dict(allowed_fields),
        task_card=task_card,
        skill_versions=[],
        press_patterns=list(press_patterns),
        digit_words=dict(digit_words or {}),
    )


def build_settings(env: Mapping[str, str], engine: m.EngineConfig, **over: Any) -> Settings:
    """把 conftest 的 mode_env(...) 字典（全是测试值）转成 Settings，不经 A 线的 load_settings。"""

    def path(name: str) -> Path | None:
        return Path(env[name]) if env.get(name) else None

    data: dict[str, Any] = {
        "mode": env["WAITLESS_MODE"],
        "host": env["WAITLESS_HOST"],
        "port": int(env["WAITLESS_PORT"]),
        "allowed_hosts": tuple(h for h in env["WAITLESS_ALLOWED_HOSTS"].split(",") if h),
        "db_path": Path(env["WAITLESS_DB_PATH"]),
        "web_dir": Path(env["WAITLESS_WEB_DIR"]),
        "skills_dir": Path(env["WAITLESS_SKILLS_DIR"]),
        "company_dir": Path(env["WAITLESS_COMPANY_DIR"]),
        "engine_config_path": Path(env["WAITLESS_ENGINE_CONFIG"]),
        "aws_region": env.get("AWS_REGION"),
        "aws_access_key_id": env.get("AWS_ACCESS_KEY_ID"),
        "aws_secret_access_key": env.get("AWS_SECRET_ACCESS_KEY"),
        "planner_model_id": env.get("WAITLESS_PLANNER_MODEL_ID"),
        "jev_url": env.get("WAITLESS_JEV_URL"),
        "jev_model": env.get("WAITLESS_JEV_MODEL"),
        "jev_api_key": env.get("WAITLESS_JEV_API_KEY"),
        "replay_dir": path("WAITLESS_REPLAY_DIR"),
        "record_dir": path("WAITLESS_RECORD_DIR"),
        "engine": engine,
    }
    data.update(over)
    return Settings(**data)


def with_pace(engine: m.EngineConfig, pace_ms: int) -> m.EngineConfig:
    """只改 textsim.pace_ms，保留 textsim 的其他字段（A-11 r2 加 default_scenario）。"""
    textsim = engine.textsim.model_copy(update={"pace_ms": pace_ms})
    return engine.model_copy(update={"textsim": textsim})


# ---------------------------------------------------------------------------
# 决策点注册表的替身（A-06：录制器与回放器用 registry.point_of(question_ids) 反推 point；只实现这一个方法）
# ---------------------------------------------------------------------------

# §6.6 八个决策点的题名（全局唯一）
POINT_QUESTIONS: dict[str, tuple[str, ...]] = {
    "assistant_next_action": ("next_action",),
    "company_skill": ("company",),
    "task_intent": ("intent",),
    "precall_default": ("precall",),
    "call_round": tuple(m.CALL_ROUND_QUESTIONS),
    "said_as_decided": ("said",),
    "call_outcome": ("outcome",),
    "result_item_kind": ("kind",),
}


class UnknownQuestionSet(RegistryError, KeyError):
    """对不上任何决策点：与真实 Registry.point_of 一样是 RegistryError（r2、A-06），同时保留原来的 KeyError。"""


class StubRegistry:
    """只实现 point_of(question_ids)：按 §6.6 的题名表反推决策点；对不上抛 UnknownQuestionSet（RegistryError）。"""

    def point_of(self, question_ids: Any) -> str:
        ids = frozenset(question_ids)
        for point, qids in POINT_QUESTIONS.items():
            if ids == frozenset(qids):
                return point
        raise UnknownQuestionSet(f"no decision point asks exactly {sorted(ids)}")


# 测试主线里用户说的那句话（prepare_task 的 request；回放文件头的 request_text，X-G3）
REQUEST_TEXT = "Please start electricity service with Lakeshore Energy at my new address on November 1."


def replay_header(**over: Any) -> m.ReplayHeader:
    """ReplayHeader：scenario_id 记录录制时的场景（A-11），request_text 记录用户原话（X-G3），
    models.planner 写 "<provider>:<model>"（D14-1）。"""
    data: dict[str, Any] = {
        "replay_version": 1,
        "recorded_at": "2026-10-14T12:00:00+00:00",
        "source_mode": "text-sim",
        "scenario_id": "baseline",
        "request_text": REQUEST_TEXT,
        "skills": [{"name": "decide-option", "commit": "unknown"}],
        "models": {"jev": "test-jev-model", "planner": "bedrock:test-planner-model"},
    }
    data.update(over)
    return m.ReplayHeader.model_validate(data)


# ---------------------------------------------------------------------------
# 一通 text 通话的组装
# ---------------------------------------------------------------------------


@dataclass
class Rig:
    settings: Settings
    clock: Any
    spec: m.CallSpec
    compiled: m.CompiledCall
    company: ScriptedCompany
    gate: ScriptedGate
    trace: MemoryTrace
    store: StubStore
    pending: StubPending
    llm: StubLlm
    line_tokens: StubLineTokens
    handoff_tokens: StubHandoffTokens
    deps: CallDeps
    ended_calls: list[str] = field(default_factory=list)
    factory_calls: list[str | None] = field(default_factory=list)
    session: Any = None

    def create(self) -> Any:
        from waitless.call.textcall import create_text_call

        self.session = create_text_call(self.spec, self.deps)
        return self.session

    async def start(self) -> Any:
        session = self.create() if self.session is None else self.session
        await session.start()
        return session

    def events(self, *types: str) -> list[m.TraceEvent]:
        evs = self.trace.events(call_id=self.spec.call_id)
        return [e for e in evs if not types or e.event_type in types]

    def texts(self, event_type: str) -> list[str]:
        out: list[str] = []
        for e in self.events(event_type):
            if event_type == "injection_sent":
                out.append(str((e.action or {}).get("text", "")))
            else:
                out.append(e.transcript or "")
        return out


def make_rig(
    *,
    settings: Settings,
    clock: Any,
    company: ScriptedCompany,
    script: Sequence[tuple[str, dict[str, Any]]] | Callable[..., dict[str, Any]] = (),
    allowed_fields: Mapping[str, str],
    points: Callable[[str, m.DecisionRequest], dict[str, Any]] | None = None,
    profile: m.Profile | None = None,
    rules: Sequence[m.StandingRule] = (),
    llm: StubLlm | None = None,
    compiled: m.CompiledCall | None = None,
    timeout_sec: int = 20,
    call_id: str = "call-e-1",
    task_id: str = "task-e-1",
    scenario_id: str | None = "stub-scenario",
) -> Rig:
    card = compiled.task_card if compiled is not None else make_task_card()
    compiled = compiled or make_compiled(card, allowed_fields, timeout_sec=timeout_sec)
    trace = MemoryTrace(clock)
    store = StubStore(profile=profile, rules=rules)
    gate = ScriptedGate(trace, script=script, points=points)
    pending = StubPending(clock)
    llm = llm or StubLlm()
    line_tokens = StubLineTokens()
    handoff_tokens = StubHandoffTokens(clock, ttl_s=settings.engine.handoff.token_ttl_s)
    ended_calls: list[str] = []
    factory_calls: list[str | None] = []

    async def on_ended(cid: str) -> None:
        ended_calls.append(cid)

    def company_factory(sid: str | None) -> ScriptedCompany:
        factory_calls.append(sid)
        return company

    deps = CallDeps(
        settings=settings,
        store=store,
        trace=trace,
        gate=gate,
        pending=pending,
        llm=llm,
        clock=clock,
        line_tokens=line_tokens,
        handoff_tokens=handoff_tokens,
        on_ended=on_ended,
        company_factory=company_factory,
    )
    spec = m.CallSpec(
        call_id=call_id,
        task_id=task_id,
        mode=settings.mode,
        company_name=card.company.name,
        to_number=card.company.official_number,
        scenario_id=scenario_id,
        compiled=compiled,
    )
    return Rig(
        settings=settings,
        clock=clock,
        spec=spec,
        compiled=compiled,
        company=company,
        gate=gate,
        trace=trace,
        store=store,
        pending=pending,
        llm=llm,
        line_tokens=line_tokens,
        handoff_tokens=handoff_tokens,
        deps=deps,
        ended_calls=ended_calls,
        factory_calls=factory_calls,
    )


# ---------------------------------------------------------------------------
# 转接 WebSocket 的替身（§6.4 text 模式：只回 ready 与 ended）
# ---------------------------------------------------------------------------

try:  # lane/env 的环境里有 Starlette（fastapi 依赖）；没有时用同名的本地异常
    from starlette.websockets import WebSocketDisconnect
except ImportError:  # pragma: no cover - 取决于环境

    class WebSocketDisconnect(Exception):  # type: ignore[no-redef]
        def __init__(self, code: int = 1000, reason: str | None = None) -> None:
            super().__init__(code)
            self.code = code
            self.reason = reason


class FakeHandoffSocket:
    """Starlette WebSocket 的常用子集：accept、receive*/iter_*、send_*、close。

    浏览器发来的消息用 feed_text / feed_bytes / feed_disconnect 排进队列；发出的帧记在 sent：
    ("text", str) 或 ("bytes", bytes)。accept 可以重复调用（端点与会话谁先 accept 未定，见测试计划第 5 节）。
    """

    def __init__(self) -> None:
        self._incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[tuple[str, Any]] = []
        self.accepted = False
        self.closed_code: int | None = None

    # -- 浏览器一侧 ---------------------------------------------------------
    def feed_text(self, text: str) -> None:
        self._incoming.put_nowait({"type": "websocket.receive", "text": text})

    def feed_json(self, obj: Any) -> None:
        self.feed_text(json.dumps(obj))

    def feed_bytes(self, data: bytes) -> None:
        self._incoming.put_nowait({"type": "websocket.receive", "bytes": data})

    def feed_disconnect(self, code: int = 1000) -> None:
        self._incoming.put_nowait({"type": "websocket.disconnect", "code": code})

    def sent_json(self) -> list[dict[str, Any]]:
        return [json.loads(v) for k, v in self.sent if k == "text"]

    def sent_bytes(self) -> list[bytes]:
        return [v for k, v in self.sent if k == "bytes"]

    # -- Starlette 接口 -----------------------------------------------------
    async def accept(self, subprotocol: str | None = None, headers: Any = None) -> None:
        self.accepted = True

    async def receive(self) -> dict[str, Any]:
        return await self._incoming.get()

    async def _receive_data(self, key: str) -> Any:
        msg = await self.receive()
        if msg["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(msg.get("code", 1000))
        if key not in msg:
            raise RuntimeError(f"expected a {key} frame, got {sorted(msg)}")
        return msg[key]

    async def receive_text(self) -> str:
        return await self._receive_data("text")

    async def receive_bytes(self) -> bytes:
        return await self._receive_data("bytes")

    async def receive_json(self, mode: str = "text") -> Any:
        return json.loads(await self.receive_text())

    async def iter_text(self):  # type: ignore[no-untyped-def]
        try:
            while True:
                yield await self.receive_text()
        except WebSocketDisconnect:
            return

    async def iter_bytes(self):  # type: ignore[no-untyped-def]
        try:
            while True:
                yield await self.receive_bytes()
        except WebSocketDisconnect:
            return

    async def iter_json(self):  # type: ignore[no-untyped-def]
        async for text in self.iter_text():
            yield json.loads(text)

    async def send_text(self, data: str) -> None:
        self.sent.append(("text", data))

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(("bytes", bytes(data)))

    async def send_json(self, data: Any, mode: str = "text") -> None:
        self.sent.append(("text", json.dumps(data)))

    async def send(self, message: Mapping[str, Any]) -> None:
        if message.get("type") == "websocket.send":
            if message.get("text") is not None:
                self.sent.append(("text", message["text"]))
            elif message.get("bytes") is not None:
                self.sent.append(("bytes", bytes(message["bytes"])))
        elif message.get("type") == "websocket.close":
            self.closed_code = int(message.get("code", 1000))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed_code = code


__all__ = [
    "REAL_GUARD_S",
    "REQUEST_TEXT",
    "SAY_TEMPLATES",
    "DecoyProfile",
    "FIELD_CUES",
    "FakeHandoffSocket",
    "MemoryTrace",
    "POINT_QUESTIONS",
    "Rig",
    "ScriptedCompany",
    "ScriptedGate",
    "SimpleClock",
    "StubHandoffTokens",
    "StubLineTokens",
    "StubLlm",
    "StubPending",
    "StubRegistry",
    "StubStore",
    "WebSocketDisconnect",
    "When",
    "bare_decision",
    "build_settings",
    "full_policy",
    "is_template_utterance",
    "line",
    "make_compiled",
    "make_rig",
    "make_task_card",
    "queue_reactions",
    "replay_header",
    "run_until",
    "spec_ask_user",
    "spec_end",
    "spec_handoff",
    "spec_press",
    "spec_release",
    "spec_say",
    "spec_wait",
    "with_pace",
]
