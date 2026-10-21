"""text-sim 整条决策链的组装（第 3 步写，冻结；AC-85、AC-86、AC-88、AC-90、AC-101、AC-107）。

经 A 线的 app.build_services(settings, jev=, converse=, clock=)（缺口 E-G-3）组装全部服务，只有 Jev 与 Converse
用替身（FakeJev、FakeConverse），零密钥、零网络；然后按主线走：prepare_task → start_assisted_call →
用户在需要时 Join、Return、作答 → get_case_result。被测的 E 线代码是其中的 TextCall、TextActionBackend
（以及录制、回放时的 ReplayRecorder、ReplayPlayer）。

ScenarioOracle 是「按场景作者的期望作答」的 Jev：对方每说一句，它在场景 YAML 的台词里找到这一句，按台词的
expect（以及菜单路径 expected.menu_digits）回答 call_round 的 4 题；规划阶段的 DP-P1/P2/P3 选本场景的公司、
开通意图、保持默认；挂断后的 DP-E1 按 expected.outcome 作答。Converse 替身：L1 原样抽出公司名与日期，
L3 把对方原话按「or」原样切成至多 3 个选项，L4 用 confirmation_pattern 从转写里找确认号，L5 给一句摘要。
所以整条链跑出来的 outcome 与打扰次数，检验的是 TextCall、策略、文本公司、结果流程能否按场景文件走通，
而不是 Jev 的判断力。Waitless 说了什么、按了什么，从 trace 的 waitless_utterance、injection_sent、dtmf_sent 读。
"""

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from waitless import models as m
from waitless.interfaces import ToolFailure

from e_support.stubs import REAL_GUARD_S, REQUEST_TEXT, DecoyProfile

COMPANY_ID = "lakeshore-energy"
SCENARIO_IDS: tuple[str, ...] = ("baseline", "menu-reworded", "account-holder-required")


# ---------------------------------------------------------------------------
# 场景与公司文件（只读，按冻结模型校验；不经 B 线的加载器，便于 oracle 独立于被测代码）
# ---------------------------------------------------------------------------


def read_scenario(company_dir: Path, scenario_id: str) -> m.Scenario:
    raw = yaml.safe_load((company_dir / COMPANY_ID / "scenarios" / f"{scenario_id}.yaml").read_text(encoding="utf-8"))
    return m.Scenario.model_validate(raw)


# ---------------------------------------------------------------------------
# 按场景期望作答的 Jev 与 Converse
# ---------------------------------------------------------------------------


def _strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, Mapping):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, (list, tuple)):
        return [s for v in obj for s in _strings(v)]
    return []


def _pick(criteria: Sequence[str], hint: str) -> str:
    """含 hint 的候选优先，其次第一个不是 none_of_the_above 的候选。"""
    for c in criteria:
        if hint in c.lower():
            return c
    return next((c for c in criteria if c != m.NONE_OF_THE_ABOVE), criteria[0])


def split_offer(text: str, limit: int = 3) -> list[dict[str, str]]:
    """把对方原话按「or」切开，原样保留每一段（L3「原样抽取」的替身）。"""
    parts = [p for p in re.split(r",?\s+or\s+", text.strip()) if p.strip()]
    if len(parts) < 2:
        parts = [text.strip()]
    return [{"label": p.strip(" .?!"), "detail": p.strip()} for p in parts[:limit]]


class ScenarioOracle:
    """按场景文件作答。台词表直接取自场景 YAML（不经 B 的加载器，也不旁听公司对象）：
    prompt、menu 步的 text，ai_agent、live_rep 步 text_script 的每句 say（带 expect）。
    台词里的 {占位}（如确认号）按任意文字匹配。"""

    def __init__(self, scenario: m.Scenario) -> None:
        self.scenario = scenario
        self.menu_digit_by_step = self._menu_path(scenario)
        self.script = self._script_lines(scenario)
        self.offer_sources: list[str | None] = []
        pattern = scenario.expected.confirmation_pattern
        self.confirmation_regex = re.compile(pattern.lstrip("^").rstrip("$"))

    # -- 场景结构 -----------------------------------------------------------
    @staticmethod
    def _menu_path(scenario: m.Scenario) -> dict[str, str]:
        """从第一步出发，按 expected.menu_digits 走一遍，得到每个菜单步该按的键。"""
        digits = list(scenario.expected.menu_digits)
        out: dict[str, str] = {}
        step_id: str | None = next(iter(scenario.steps))
        for _ in range(len(scenario.steps) + 2):
            step = scenario.steps.get(step_id) if step_id else None
            if step is None:
                break
            if step.kind == "menu":
                if not digits:
                    break
                d = digits.pop(0)
                out[step_id] = d  # type: ignore[index]
                step_id = (step.options or {}).get(d)
            elif step.kind in ("prompt", "hold"):
                step_id = step.next
            else:
                break
        return out

    @staticmethod
    def _script_lines(scenario: m.Scenario) -> list[tuple[re.Pattern[str], m.CompanyLine]]:
        out: list[tuple[re.Pattern[str], m.CompanyLine]] = []

        def add(step_id: str, kind: str, role: str, text: str | None, expect: m.EvalExpect | None = None) -> None:
            if not text or not text.strip():
                return
            parts = re.split(r"\{[^{}]*\}", text.strip())
            rx = re.compile(".+?".join(re.escape(p) for p in parts), re.IGNORECASE | re.DOTALL)
            out.append((rx, m.CompanyLine(step_id=step_id, kind=kind, role=role, text=text.strip(), expect=expect)))  # type: ignore[arg-type]

        for step_id, step in scenario.steps.items():
            if step.kind in ("prompt", "menu", "hold"):
                add(step_id, step.kind, "ivr", step.text)
            role = {"ai_agent": "agent", "live_rep": "rep"}.get(step.kind)
            if role:
                for ln in step.text_script or []:
                    add(step_id, "speech", role, ln.say, ln.expect)
        return out

    def line_for(self, text: str) -> m.CompanyLine | None:
        """对方刚说的这句（Jev state 转写的最后一行）对应的场景台词。"""
        text = text.strip()
        for rx, ln in self.script:
            if ln.text == text:
                return ln
        for rx, ln in self.script:
            if rx.search(text):
                return ln
        for rx, ln in self.script:
            if ln.text in text or text in ln.text:
                return ln
        return None

    # -- Jev ----------------------------------------------------------------
    def answer_jev(self, state: m.JevState, questions: Mapping[str, m.JevQuestion], point: str | None) -> dict[str, Any]:
        from fakes import jev_choice, jev_noul, jev_score

        last = state.transcript.lines[-1].text if state.transcript.lines else ""
        ln = self.line_for(last)
        out: dict[str, Any] = {}
        for qid, q in questions.items():
            crit = list(q.criteria) if isinstance(q.criteria, dict) else []
            if qid == "menu_choice":
                out[qid] = jev_choice(self._menu_choice(crit, ln), 0.95)
            elif qid == "call_phase":
                out[qid] = jev_choice(self._phase(crit, ln), 0.95)
            elif qid == "needs_user":
                out[qid] = jev_noul(self._needs_user(ln))
            elif qid == "stakes":
                out[qid] = jev_score(self._stakes(ln), 0.95)
            elif qid == "said":
                out[qid] = jev_noul(0.97)
            elif qid == "outcome":
                want = self.scenario.expected.outcome
                out[qid] = jev_choice(want if want in crit else crit[0], 0.95)
            elif qid == "kind":
                out[qid] = jev_choice("verified" if "verified" in crit else crit[0], 0.95)
            elif qid == "company":  # DP-P1：本场景的公司 skill
                out[qid] = jev_choice(_pick(crit, "lakeshore"), 0.95)
            elif qid == "intent":  # DP-P2：开通服务
                out[qid] = jev_choice(_pick(crit, "start"), 0.95)
            elif qid == "precall":  # DP-P3：用户原话没选，保持默认
                out[qid] = jev_choice("keep_default" if "keep_default" in crit else crit[0], 0.95)
            elif q.type == "choice":
                out[qid] = jev_choice(crit[0], 0.95)
            elif q.type == "noul":
                out[qid] = jev_noul(0.5)
            else:
                out[qid] = jev_score(0, 0.95)
        return out

    def _intent(self, ln: m.CompanyLine | None) -> str | None:
        return ln.expect.intent if ln is not None and ln.expect is not None else None

    def _menu_choice(self, crit: list[str], ln: m.CompanyLine | None) -> str:
        if ln is not None and ln.kind == "menu":
            d = self.menu_digit_by_step.get(ln.step_id)
            if d in crit:
                return d  # type: ignore[return-value]
        intent = self._intent(ln)
        if intent in crit:
            return intent  # type: ignore[return-value]
        if ln is not None and ln.expect is not None and ln.expect.digits and ln.expect.digits in crit:
            return ln.expect.digits
        if ln is not None and ln.kind in ("prompt", "hold") and "wait" in crit:
            return "wait"
        for fallback in ("open_answer", "wait"):
            if fallback in crit:
                return fallback
        return crit[0]

    def _phase(self, crit: list[str], ln: m.CompanyLine | None) -> str:
        if ln is None:
            phase = "ivr_menu"
        elif ln.kind == "hangup":
            phase = "ended"
        elif ln.kind == "hold":
            phase = "hold"
        elif ln.role == "agent":
            phase = "ai_agent"
        elif ln.role == "rep":
            phase = "live_rep"
        else:
            phase = "ivr_menu"
        return phase if phase in crit else crit[0]

    def _needs_user(self, ln: m.CompanyLine | None) -> float:
        intent = self._intent(ln)
        if intent in ("request_handoff", "ask_user") or (ln is not None and ln.expect is not None and ln.expect.ask_user):
            return 0.9
        return 0.03

    def _stakes(self, ln: m.CompanyLine | None) -> float:
        intent = self._intent(ln) or ""
        if intent == "ask_user" or intent.startswith(m.ANSWER_PRECALL_PREFIX) or (
            ln is not None and ln.expect is not None and ln.expect.ask_user
        ):
            return 3
        return 0

    # -- Converse -----------------------------------------------------------
    def answer_converse(self, call: m.ConverseCall) -> dict[str, Any]:
        from fakes import converse_text, converse_tool_use, forced_tool_name

        tool = forced_tool_name(call)
        text = "\n".join(_strings(call.messages) + _strings(call.system))
        if call.task == "extract_offer_options":
            source = self._latest_line_in(text)
            self.offer_sources.append(source.text if source else None)
            spoken = source.text if source else text
            if source is not None:
                hit = next((rx.search(text) for rx, ln in self.script if ln is source), None)
                spoken = hit.group(0) if hit else source.text
                self.offer_sources[-1] = spoken
            return converse_tool_use(tool or "extract_offer_options", {"options": split_offer(spoken)})
        if call.task == "extract_result_items":
            numbers: list[str] = []
            for n in self.confirmation_regex.findall(text):
                if n not in numbers:
                    numbers.append(n)
            return converse_tool_use(tool or "extract_result_items", {"confirmation_numbers": numbers, "statements": []})
        if call.task == "extract_request":  # L1：从 REQUEST_TEXT 原样抽取
            return converse_tool_use(
                tool or "extract_request",
                {
                    "company_name": "Lakeshore Energy",
                    "intent_text": "start electricity service",
                    "effective_date": "November 1",
                    "service_address": None,
                    "stated_preferences": [],
                },
            )
        return converse_text("Lakeshore Energy confirmed the new service start.")

    def _latest_line_in(self, text: str) -> m.CompanyLine | None:
        """L3 的输入里出现的那句场景台词（取 text_script 里最后一句能对上的）。"""
        found = None
        for rx, ln in self.script:
            if ln.kind == "speech" and rx.search(text):
                found = ln
        return found


# ---------------------------------------------------------------------------
# 组装（E-G-3：经 app.build_services(settings, jev=, converse=, clock=)，不碰 A 的内部方法）
# ---------------------------------------------------------------------------


@dataclass
class Stack:
    settings: Any
    clock: Any
    services: Any  # interfaces.AppServices
    parts: Any  # app.AppParts（tasks、calls、results、assistant…）
    skills: Mapping[str, m.Skill]
    registry: Any
    fake_jev: Any
    fake_converse: Any
    oracle: ScenarioOracle
    profile: m.Profile

    @property
    def store(self) -> Any:
        return self.services.store

    @property
    def trace(self) -> Any:
        return self.services.trace


def build_stack(
    settings: Any,
    *,
    scenario_id: str,
    profile: Mapping[str, str],
    account: Mapping[str, str],
    clock: Any = None,
    decoys: Mapping[str, str] | None = None,
    clients: Callable[[Any, Any, Any], tuple[Any, Any]] | None = None,
) -> Stack:
    """profile、account 取 conftest 的 test_profile、test_account（B-G-9：company.yaml 的 customers[0] 照抄它们）。

    clients(fake_jev, fake_converse, registry) -> (jev, converse)：录制时包一层 ReplayRecorder；默认直接用两个替身；
    返回 (None, None) 表示不注入（replay 模式由 build_services 按 settings.replay_dir / replay_file 建 ReplayPlayer）。
    decoys 给出时，档案存储读出的是带诱饵的 DecoyProfile（「用夹具档案绕过 Store」，AC-107）：只替换这个 Store 实例的
    get_profile（interfaces.Store 的跨线方法），不碰 A 的内部方法。
    """
    from fakes import FakeClock, FakeConverse, FakeJev
    from waitless.app import build_services
    from waitless.decision.registry import load_registry
    from waitless.skills.loader import load_skills

    clock = clock or FakeClock()
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    skills = load_skills(settings.skills_dir)
    registry = load_registry(skills).validate()
    oracle = ScenarioOracle(read_scenario(settings.company_dir, scenario_id))
    fake_jev = FakeJev(default=oracle.answer_jev)
    fake_converse = FakeConverse(default=oracle.answer_converse)
    jev, converse = clients(fake_jev, fake_converse, registry) if clients else (fake_jev, fake_converse)
    services, parts = build_services(settings, jev=jev, converse=converse, clock=clock)

    services.store.save_profile({k: v for k, v in profile.items() if k in m.UPDATABLE_FIELD_KEYS})
    for company_id, number in account.items():
        services.store.save_profile({"account_number": number}, company_id=company_id)
    fixture = m.Profile(**dict(profile), accounts=dict(account))
    if decoys:
        fixture = DecoyProfile(**dict(profile), accounts=dict(account), **dict(decoys))
        decoy_profile = fixture
        services.store.get_profile = lambda: decoy_profile  # type: ignore[method-assign]

    return Stack(
        settings=settings,
        clock=clock,
        services=services,
        parts=parts,
        skills=skills,
        registry=registry,
        fake_jev=fake_jev,
        fake_converse=fake_converse,
        oracle=oracle,
        profile=fixture,
    )


# ---------------------------------------------------------------------------
# 跑一通主线：prepare_task → start_assisted_call → 用户只在需要时点按钮（Join → Return、作答待决选项）
# ---------------------------------------------------------------------------


def prefer_no_charge(choice: m.PendingChoice) -> m.Option:
    for opt in choice.options:
        if re.search(r"no charge|free", f"{opt.label} {opt.detail}", re.IGNORECASE):
            return opt
    return choice.options[0]


@dataclass
class ScenarioRun:
    scenario_id: str
    task_id: str
    call_id: str
    card: m.TaskCard
    phases: list[str]
    joins: list[m.JoinCallOut]
    returns: list[m.ReturnOut]
    answers: list[tuple[m.PendingChoice, m.Option, m.AnswerChoiceOut]]
    max_interrupts_seen: int
    case: m.CaseResultOut
    events: list[m.TraceEvent]  # 本通电话的 trace
    task_events: list[m.TraceEvent]  # 整个任务的 trace（含 P1–P3 等非通话决策）

    @property
    def said(self) -> list[str]:
        """Waitless 对公司说出的每一句（waitless_utterance）。"""
        return [e.transcript or "" for e in self.events if e.event_type == "waitless_utterance"]

    @property
    def injected(self) -> list[str]:
        return [str((e.action or {}).get("text", "")) for e in self.events if e.event_type == "injection_sent"]

    @property
    def digits(self) -> list[str]:
        """按过的键（dtmf_sent），按顺序。"""
        return [str((e.action or {}).get("digits", "")) for e in self.events if e.event_type == "dtmf_sent"]


async def run_scenario(
    stack: Stack,
    scenario_id: str,
    *,
    choose: Callable[[m.PendingChoice], m.Option] = prefer_no_charge,
    on_ids: Callable[[str, str | None], None] | None = None,
    step_ms: int = 200,
    max_fake_ms: int = 420_000,
) -> ScenarioRun:
    """on_ids(task_id, call_id)：拿到 task_id、call_id 时回调（录制时用来调 ReplayRecorder.bind，E-G-8）。"""
    prepared = await stack.parts.tasks.prepare(REQUEST_TEXT)
    task_id = prepared.task_id
    if on_ids:
        on_ids(task_id, None)
    card = prepared.task_card
    if prepared.status != "ready" and prepared.missing:
        values = stack.profile.model_dump()
        fields = {f.key: values.get(f.key) for f in prepared.missing if values.get(f.key)}
        updated = await stack.parts.tasks.update(task_id, m.UpdateTaskFields(**fields))
        card = updated.task_card
    assert card is not None, f"prepare_task did not build a task card: {prepared}"
    out = await stack.parts.calls.start(task_id, True, card.card_version, scenario_id=scenario_id)
    call_id = out.call_id
    if on_ids:
        on_ids(task_id, call_id)

    phases: list[str] = []
    joins: list[m.JoinCallOut] = []
    returns: list[m.ReturnOut] = []
    answers: list[tuple[m.PendingChoice, m.Option, m.AnswerChoiceOut]] = []
    answered: set[str] = set()
    interrupts = [0]

    async def drive() -> None:
        waited = 0
        while True:
            st = stack.parts.calls.status(task_id)
            interrupts[0] = max(interrupts[0], st.interrupts_used)
            if not phases or phases[-1] != st.phase:
                phases.append(st.phase)
            if st.ended:
                return
            if st.phase == "handoff_ready":
                try:
                    joined = await stack.parts.calls.join(task_id)
                except ToolFailure:
                    joined = None
                if joined is not None:
                    joins.append(joined)
                    await stack.clock.advance(step_ms)
                    returns.append(await stack.parts.calls.return_(task_id, joined.handoff_id))
            elif st.pending_choice is not None and st.pending_choice.status == "open" and st.pending_choice.choice_id not in answered:
                choice = st.pending_choice
                option = choose(choice)
                answered.add(choice.choice_id)
                answers.append((choice, option, await stack.parts.calls.answer(task_id, choice.choice_id, option.id)))
            if waited >= max_fake_ms:
                raise AssertionError(f"scenario {scenario_id} did not end within {max_fake_ms} ms of fake time; phases {phases}")
            await stack.clock.advance(step_ms)
            waited += step_ms

    await asyncio.wait_for(drive(), timeout=REAL_GUARD_S * 3)
    session = stack.services.calls.get_session(call_id)
    if session is not None:
        await asyncio.wait_for(session.wait_ended(), timeout=REAL_GUARD_S)

    case = stack.parts.results.case_result(task_id)
    for _ in range(300):  # 结果流程（on_ended → finalize）在后台跑完
        if case.outcome != "in_progress":
            break
        await stack.clock.advance(step_ms)
        case = stack.parts.results.case_result(task_id)

    return ScenarioRun(
        scenario_id=scenario_id,
        task_id=task_id,
        call_id=call_id,
        card=card,
        phases=phases,
        joins=joins,
        returns=returns,
        answers=answers,
        max_interrupts_seen=interrupts[0],
        case=case,
        events=stack.trace.events(call_id=call_id),
        task_events=stack.trace.events(task_id=task_id),
    )


def compiled_for(stack: Stack, run: ScenarioRun) -> m.CompiledCall:
    """用这通电话的任务卡与组装所用的档案，按同样的输入再调一次 G 的 compile_call（AC-107：检查它返回的 sonic_prompt）。"""
    from waitless.skills.compiler import compile_call

    return compile_call(run.card, stack.skills, stack.store.get_profile(), stack.store.list_standing_rules(), [])


def scenario_settings(build_settings: Callable[..., Any], mode_env: Callable[..., dict[str, str]], engine: Any, root: Path, mode: str = "text-sim", **env_over: str) -> Any:
    """每个场景一个独立的数据库目录（root/<名字>/waitless.db）。"""
    root.mkdir(parents=True, exist_ok=True)
    env = mode_env(mode, WAITLESS_DB_PATH=str(root / "waitless.db"), **env_over)
    return build_settings(env, engine)


__all__ = [
    "COMPANY_ID",
    "compiled_for",
    "REQUEST_TEXT",
    "SCENARIO_IDS",
    "ScenarioOracle",
    "ScenarioRun",
    "Stack",
    "build_stack",
    "prefer_no_charge",
    "read_scenario",
    "run_scenario",
    "scenario_settings",
    "split_offer",
]
