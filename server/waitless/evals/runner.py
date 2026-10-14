"""文本级与音频级评测的运行器（E 线；tech §6.12；AC-90、AC-93）。

公开接口（§7）：run_eval(mode, ...) -> Metrics（models.EvalMetrics）。
"""

import asyncio
import inspect
import logging
from collections.abc import Sequence
from pathlib import Path

import yaml

from waitless.config import Settings
from waitless.interfaces import ConverseClient, JevClient
from waitless import models as m
from waitless.models import EvalCase, EvalMetrics, MenuVariant
from waitless.app import build_services
from waitless.bedrock import build_converse_client
from waitless.company.config import list_scenarios, load_company, load_scenario
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
    settings = settings.model_copy(update={"mode": mode, "skills_dir": skills_dir, "record_dir": None})
    cases = list(cases) if cases is not None else load_cases(skills_dir)
    skills = load_skills(skills_dir)
    registry = load_registry(skills)
    owned = []
    services = None
    try:
        if jev is None:
            jev = HttpJevClient(url=settings.jev_url, model=settings.jev_model,
                               api_key=settings.jev_api_key, timeout_s=5.0)
            owned.append(jev)
        if converse is None:
            converse = build_converse_client(settings)
            owned.append(converse)
        services, parts = build_services(settings, jev=jev, converse=converse)
        gate = DecisionGate(jev=jev, jev_model=settings.jev_model, registry=registry,
                            policy=Policy.load(registry.policy_raw()), trace=services.trace, clock=services.clock)
        runs = await _score_cases(settings, skills, gate, services, parts, load_menus(menus_dir), cases)
        for company_path in sorted(settings.company_dir.glob("*/company.yaml")):
            company_id = company_path.parent.name
            for sid in list_scenarios(settings.company_dir, company_id):
                if scenario is not None and sid != scenario:
                    continue
                spec = load_scenario(settings.company_dir, company_id, sid)
                request = next(c.input["request"] for c in cases if c.kind == "scenario"
                               and c.input.get("scenario_id") == sid and c.input.get("request"))
                runs.append(await _scenario_call(settings, services, parts, spec, request))
        return compute_metrics(runs)
    finally:
        if services is not None:
            services.store.close()
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
                await find_official_number(data["company"], data["intent"], parts.fetcher,
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


async def _scenario_call(settings, services, parts, spec, request):
    prepared = await parts.tasks.prepare(request)
    if prepared.missing:
        customer = load_company(settings.company_dir, spec.company_id).customers[0].model_dump()
        prepared = await parts.tasks.update(prepared.task_id,
            fields=m.UpdateTaskFields(**{f.key: customer[f.key] for f in prepared.missing}))
    started = await parts.calls.start(prepared.task_id, True, prepared.task_card.card_version, scenario_id=spec.scenario_id)
    while True:
        status = parts.calls.status(prepared.task_id)
        if status.ended:
            break
        if status.pending_choice and status.pending_choice.status == "open":
            choice = status.pending_choice
            await parts.calls.answer(prepared.task_id, choice.choice_id, choice.options[0].id)
        if status.phase == "handoff_ready":
            joined = await parts.calls.join(prepared.task_id)
            await parts.calls.return_(prepared.task_id, joined.handoff_id)
        await asyncio.sleep(settings.engine.web.poll_ms / 1000)
    case = parts.results.case_result(prepared.task_id)
    call = next(c for c in case.calls if c.call_id == started.call_id)
    return CaseRun(case_id=spec.scenario_id, kind="scenario", interrupts=call.interrupts_used,
                   completed=case.outcome == spec.expected.outcome and call.interrupts_used == spec.expected.interrupts)
