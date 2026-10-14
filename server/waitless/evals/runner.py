"""文本级与音频级评测的运行器（E 线；tech §6.12；AC-90、AC-93）。

公开接口（§7）：run_eval(mode, ...) -> Metrics（models.EvalMetrics）。
"""

import asyncio
import inspect
import logging
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import yaml

from waitless.config import Settings, SystemClock
from waitless.interfaces import ConverseClient, JevClient, ReplayMismatch
from waitless.replay.player import ReplayPlayer
from waitless.replay.recorder import ReplayRecorder
from waitless import models as m
from waitless.models import EvalCase, EvalMetrics, MenuVariant
from waitless.app import build_services
from waitless.bedrock import build_converse_client
from waitless.company.config import list_scenarios, load_company, load_scenario
from waitless.call.handoff import handoff_endpoint
from waitless.decision.gate import DecisionGate
from waitless.decision.jev import HttpJevClient
from waitless.decision.menu_parse import parse_menu
from waitless.decision.policy import Policy
from waitless.decision.registry import load_registry
from waitless.official_number import find_official_number
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills
from waitless.evals.metrics import CaseRun, compute_metrics

logger = logging.getLogger(__name__)

__all__ = ["load_cases", "load_menus", "run_eval"]


async def run_eval(
    mode: str,
    *,
    settings: Settings,
    skills_dir: Path,
    menus_dir: Path,
    cases: Sequence[EvalCase] | None = None,
    scenario: str | None = None,
    audio: bool = False,
    record_dir: Path | None = None,
    jev: JevClient | None = None,
    converse: ConverseClient | None = None,
) -> EvalMetrics:
    """加载 6 个 skill 的全部 cases*.jsonl 与 evals/menus 的手写菜单，跑一遍，输出 4 项指标（AC-90）。

    缺口 E-G-1（定稿 inject_kwargs）：jev、converse 给出时代替在线客户端（默认测试传 FakeJev、FakeConverse）；
    cli 不传，行为不变。整条链经 app.build_services(settings, jev=, converse=, clock=) 组装（缺口 E-G-3）。"""
    if record_dir is not None and (mode != "text-sim" or audio or settings.planner != "bedrock"):
        raise ValueError("Evaluation recording requires text-sim and the bedrock planner")
    if record_dir is not None:
        scenario = scenario or settings.engine.textsim.default_scenario
        path = record_dir / (scenario + ".jsonl")
        if path.exists():
            raise FileExistsError(path)
    settings = settings.model_copy(update={"mode": mode, "skills_dir": skills_dir, "record_dir": None})
    cases = list(cases) if cases is not None else load_cases(skills_dir)
    skills = load_skills(skills_dir)
    registry = load_registry(skills)
    player = None
    if mode == "replay":
        player = ReplayPlayer(settings.replay_dir / settings.replay_file, registry=registry)
        if scenario is not None and scenario != player.header.scenario_id:
            raise ValueError(f"Scenario differs from replay: {scenario}")
        scenario = player.header.scenario_id
        jev, converse = player.jev_client(), player.converse_client()
    scenarios = _select_scenarios(settings, scenario)
    requests = ({scenario: player.header.request_text} if player is not None
                else _scenario_requests(scenarios, cases))
    owned = []
    services = None
    recorder = None
    try:
        if record_dir is not None:
            header = m.ReplayHeader(replay_version=1, recorded_at=SystemClock().now_iso(),
                source_mode="text-sim", scenario_id=scenario, request_text=requests[scenario],
                skills=[m.SkillCommit(name=s.name, commit=s.version.commit) for s in skills.values()],
                models=m.ReplayModels(jev=settings.jev_model, planner="bedrock:" + settings.planner_model_id))
            recorder = ReplayRecorder(path, header, registry=registry)
        if jev is None:
            jev = HttpJevClient(url=settings.jev_url, model=settings.jev_model,
                               api_key=settings.jev_api_key, timeout_s=5.0)
            owned.append(jev)
        if converse is None:
            converse = build_converse_client(settings)
            owned.append(converse)
        if recorder is not None:
            jev, converse = recorder.wrap_jev(jev), recorder.wrap_converse(converse)
        services, parts = build_services(settings, jev=jev, converse=converse)
        gate = DecisionGate(jev=jev, jev_model=player.header.models.jev if player else settings.jev_model,
                            registry=registry, policy=Policy.load(registry.policy_raw()),
                            trace=services.trace, clock=services.clock)
        runs = await _score_cases(settings, skills, gate, services, parts, load_menus(menus_dir), cases)
        for spec in scenarios:
            runs.append(await _scenario_call(settings, services, parts, spec, requests[spec.scenario_id], recorder or player))
            if player is not None and player._first_mismatch is not None:
                raise player._first_mismatch
        if player is not None and player.remaining() > 0:
            raise ReplayMismatch(*player._first_remaining())
        return compute_metrics(runs)
    except Exception:
        if player is not None and player._first_mismatch is not None:
            raise player._first_mismatch
        raise
    finally:
        if services is not None:
            services.store.close()
        if recorder is not None:
            recorder.close()
        for client in reversed(owned):
            await _close(client)


def load_cases(skills_dir: Path) -> list[EvalCase]:
    """skills/*/evals/cases*.jsonl 的全部行；每行按 EvalCase 校验。

    歧义表 A-22（定稿 separate_files，暂定）：G 写 cases.jsonl，E 写同目录的 cases.extra.jsonl；
    两种文件都读（按文件名排序，同一文件按行序）。AC-39 仍只检查 cases.jsonl。"""
    return [
        EvalCase.model_validate_json(line)
        for path in sorted(skills_dir.glob("*/evals/cases*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_menus(menus_dir: Path) -> list[MenuVariant]:
    """evals/menus/*.yaml，至少 15 个（R34）。"""
    return [
        MenuVariant.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in sorted(menus_dir.glob("*.yaml"))
    ]


async def _close(resource):
    closer = getattr(resource, "aclose", None) or getattr(resource, "close", None)
    if closer is not None:
        result = closer()
        if inspect.isawaitable(result):
            await result


def _eval_task_card(company, company_skill, goal):
    pre = yaml.safe_load((company_skill.path / "references" / "pre-call-questions.yaml").read_text(encoding="utf-8"))
    return m.TaskCard(
        card_version=1,
        company=m.TaskCompany(company_id=company.company_id, name=company.name,
                              official_number=company.line_number, sources=[]),
        goal=goal, task_summary=goal, effective_date=None, shared_fields=[],
        precall_decisions=[m.PrecallDecision(
            question_id=q["question_id"], question=q["question"],
            options=[m.Option(**o) for o in q["options"]], default=q.get("default"),
            selected=q.get("default"), needs_user=False) for q in pre["questions"]],
        boundary="Evaluation only; no account changes.",
    )


def _correct(result, expect):
    return all((result.action == "ask_user" if key == "ask_user" else getattr(result, key)) == value
               for key, value in expect.model_dump(exclude_none=True).items())


async def _score_cases(settings, skills, gate, services, parts, menus, cases):
    company_id = sorted(p.parent.name for p in settings.company_dir.glob("*/company.yaml"))[0]
    company = load_company(settings.company_dir, company_id)
    customer = company.customers[0].model_dump()
    profile = m.Profile(**{key: customer[key] for key in m.Profile.model_fields if key != "accounts" and key in customer},
                        accounts={company_id: customer["account_number"]})
    company_skill = next(s for s in skills.values() if s.metadata.get(m.SKILL_COMPANY_ID_KEY) == company_id)
    rules = services.store.list_standing_rules()
    runs = []
    evaluations = [(v.menu_id, "round", {"goal": v.goal, "text": v.text, "kind": "menu"},
                    m.EvalExpect(digits=v.correct_digits)) for v in menus]
    evaluations += [(c.case_id, c.kind, c.input, c.expect) for c in cases]
    for i, (cid, kind, data, expect) in enumerate(evaluations):
        if kind == "scenario":
            continue
        if kind == "official_number":
            try:
                find_official_number(data["company"], data["intent"], parts.fetcher,
                                           trusted_sources=parts.trusted_sources)
                logger.info("Official number case %s succeeded", cid)
            except Exception as exc:
                logger.info("Official number case %s failed: %s", cid, type(exc).__name__)
            continue
        call_id = f"eval-{i}"
        text = data.get("text", "")
        transcript = [m.TranscriptLine(speaker="company" if kind == "round" else "user", text=text)]
        card = _eval_task_card(company, company_skill, data.get("goal") or text)
        compiled = compile_call(card, skills, profile, rules, [])
        context = m.DecisionContext(task_card=card if kind == "round" else None,
            allowed_fields=compiled.allowed_fields if kind == "round" else {}, standing_rules=rules,
            prior_choices=[], interrupts_used=0, phase=data.get("phase", "ivr" if data.get("kind") == "menu" else "live_rep"))
        if kind == "round":
            options = parse_menu(text, compiled.press_patterns, digit_words=compiled.digit_words) if data.get("kind") == "menu" else []
            now = services.clock.monotonic_ms()
            turn = m.CompanyTurn(call_id=call_id, seq=0, t_start_ms=now, t_end_ms=now, text=text,
                                 kind="menu" if options else "speech", menu_options=options)
            result = await gate.decide_round(turn, m.RoundContext(call_id=call_id, task_id="eval",
                compiled=compiled, context=context, transcript=transcript))
        else:
            candidates = [m.Candidate(id=c, label=c, description=c) if isinstance(c, str) else m.Candidate.model_validate(c)
                          for c in data.get("candidates", [])]
            point = "assistant_next_action" if kind == "assistant" else data["point"]
            result = await gate.decide(point, m.DecisionRequest(decision_id=f"eval-decision-{i}",
                point="assistant_next_action" if kind == "assistant" else data["point"], call_id=None,
                task_id="eval", turn=None, candidates=candidates, context=context, transcript=transcript))
        if kind == "round" and data.get("kind") == "menu":
            runs.append(CaseRun(case_id=cid, kind="menu", menu_correct=result.digits == expect.digits))
        else:
            runs.append(CaseRun(case_id=cid, kind="round", auto_answered=result.action not in ("ask_user", "handoff"),
                                auto_answer_correct=_correct(result, expect)))
    return runs


def _select_scenarios(settings, scenario):
    specs = []
    for path in sorted(settings.company_dir.glob("*/company.yaml")):
        for sid in list_scenarios(settings.company_dir, path.parent.name):
            if scenario is None or scenario == sid:
                specs.append(load_scenario(settings.company_dir, path.parent.name, sid))
    if scenario is not None and not specs:
        raise ValueError(f"Unknown scenario: {scenario}")
    return sorted(specs, key=lambda spec: (spec.scenario_id, spec.company_id))


def _scenario_requests(scenarios, cases):
    requests = {}
    for spec in scenarios:
        request = next((c.input.get("request") for c in cases if c.kind == "scenario"
                        and c.input.get("scenario_id") == spec.scenario_id
                        and isinstance(c.input.get("request"), str) and c.input["request"].strip()), None)
        if request is None:
            raise ValueError(f"Missing request for scenario: {spec.scenario_id}")
        requests[spec.scenario_id] = request
    return requests


class _DisconnectedUser:
    """In-process browser connection; D owns token validation and resume timing."""

    def __init__(self, services, token):
        self.app = SimpleNamespace(state=SimpleNamespace(services=services))
        self.query_params = {"token": token}
        self._hello_sent = False

    async def receive(self):
        if not self._hello_sent:
            self._hello_sent = True
            return {"type": "websocket.receive", "text": m.HandoffHello(
                type="hello", format="pcm_s16le", sample_rate=16000, channels=1).model_dump_json()}
        return {"type": "websocket.disconnect", "code": 1000}

    async def accept(self):
        pass

    async def send_json(self, data):
        pass

    async def send_text(self, data):
        pass

    async def send_bytes(self, data):
        pass

    async def close(self, code=1000):
        pass


async def _scenario_call(settings, services, parts, spec, request, backend=None):
    background = []
    session = None
    timed_out = False
    try:
        prepared = await parts.tasks.prepare(request)
        if backend is not None:
            backend.bind(task_id=prepared.task_id)
        if prepared.missing:
            customer = load_company(settings.company_dir, spec.company_id).customers[0].model_dump()
            prepared = await parts.tasks.update(prepared.task_id,
                fields=m.UpdateTaskFields(**{f.key: customer[f.key] for f in prepared.missing}))
        deadline = asyncio.get_running_loop().time() + spec.max_call_seconds + settings.engine.handoff.resume_grace_ms / 1000
        started = await parts.calls.start(prepared.task_id, True, prepared.task_card.card_version, scenario_id=spec.scenario_id)
        if backend is not None:
            backend.bind(task_id=prepared.task_id, call_id=started.call_id)
        session = services.calls.get_session(started.call_id)
        simulated = {}
        for step in spec.steps.values():
            if step.kind == "live_rep":
                simulated.update(getattr(step, "simulated_user", None) or {})
        poll_s = settings.engine.web.poll_ms / 1000
        while True:
            status = parts.calls.status(prepared.task_id)
            if status.ended:
                break
            if asyncio.get_running_loop().time() >= deadline:
                timed_out = True
                await session.hangup("eval_timeout")
                logger.warning("Scenario %s exceeded the call limit", spec.scenario_id)
                break
            if status.pending_choice and status.pending_choice.status == "open":
                choice = status.pending_choice
                choose = simulated.get("choose", "").lower()
                selected = next((o for o in choice.options if choose and
                                 (choose in o.label.lower() or choose in o.detail.lower())), choice.options[0])
                await parts.calls.answer(prepared.task_id, choice.choice_id, selected.id)
            if status.phase == "handoff_ready":
                joined = await parts.calls.join(prepared.task_id)
                if simulated.get("handoff_exit", "return") == "disconnect":
                    background.append(asyncio.create_task(handoff_endpoint(
                        _DisconnectedUser(services, joined.token), joined.handoff_id)))
                else:
                    await parts.calls.return_(prepared.task_id, joined.handoff_id)
            await asyncio.sleep(poll_s)
        await asyncio.gather(*background)
        await session.wait_ended()
        case = parts.results.case_result(prepared.task_id)
        while case.outcome == "in_progress":
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Scenario result did not finish: {spec.scenario_id}")
            await asyncio.sleep(poll_s)
            case = parts.results.case_result(prepared.task_id)
        call = next(c for c in case.calls if c.call_id == started.call_id)
        logger.info("Scenario %s outcome=%s interrupts=%d expected_outcome=%s expected_interrupts=%d",
                    spec.scenario_id, case.outcome, call.interrupts_used, spec.expected.outcome, spec.expected.interrupts)
        return CaseRun(case_id=spec.scenario_id, kind="scenario", interrupts=call.interrupts_used,
                       completed=not timed_out and case.outcome == spec.expected.outcome
                       and call.interrupts_used == spec.expected.interrupts)
    finally:
        for task in background:
            if not task.done():
                task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        if session is not None and not session.ended:
            await session.hangup("eval_cleanup")
