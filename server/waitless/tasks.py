"""组任务卡与任务状态机（A 线；tech §6.1 prepare_task/update_task、§6.6 DP-P1–P3、§8 第 1 步；AC-26–AC-30、AC-32）。

公开接口（§7）：TaskService.prepare(request, provider_hint)、TaskService.update(...)、build_task_card()。
"""

import json
import re
import uuid
import yaml
from waitless.interfaces import ToolFailure, ForbiddenField
from waitless.models import (Candidate, DecisionContext, DecisionRequest, DecisionSummary, IvrMap, MissingField,
                             NONE_OF_THE_ABOVE, PrecallDecision, PrecallQuestionFile, SharedField, SkillRef, Source,
                             TaskCompany, TranscriptLine, TrustedSources, SKILL_COMPANY_ID_KEY, UPDATABLE_FIELD_KEYS)
from waitless.official_number import find_official_number
from waitless.utility_lookup import LiveLookup, LIVE_LOOKUP_SERVICE_KEY

from collections.abc import Mapping

from waitless.interfaces import Clock, DecisionGate, LlmTasks, Store, Trace
from waitless.models import (
    DecisionResult,
    ExtractedRequest,
    OfficialNumberOut,
    PrepareTaskOut,
    Skill,
    TaskCard,
    UpdateTaskFields,
    UpdateTaskOut,
)
from waitless.official_number import Fetcher

__all__ = ["TaskService", "build_task_card", "line_display_name"]

_NO_COMPANY = {NONE_OF_THE_ABOVE, "unsupported"}
_NO_INTENT = {NONE_OF_THE_ABOVE, "ask_clarify"}


class TaskService:
    def __init__(
        self,
        *,
        store: Store,
        trace: Trace,
        gate: DecisionGate,
        llm: LlmTasks,
        skills: Mapping[str, Skill],
        fetcher: Fetcher,
        clock: Clock,
        lookup: LiveLookup | None = None,
        mode: str = "text-sim",
    ) -> None:
        self.store, self.trace, self.gate, self.llm = store, trace, gate, llm
        self.skills, self.fetcher, self.clock = skills, fetcher, clock
        self.lookup = lookup
        self.mode = mode

    async def prepare(self, request: str, provider_hint: str | None = None) -> PrepareTaskOut:
        """prepare_task：L1 抽字段 → DP-P1 选公司 skill → DP-P2 选意图 → 查官方号码 → DP-P3 逐题定默认 → 组卡。"""
        task_id = uuid.uuid4().hex
        if hasattr(self, "_begin_recording"):
            self._begin_recording(request)
        if hasattr(self, "_bind_backend"):
            self._bind_backend(task_id=task_id)
        extracted = await self.llm.extract_request(request)
        profile = self.store.get_profile()
        companies = [s for s in self.skills.values() if s.metadata.get(SKILL_COMPANY_ID_KEY)]
        used = [self.skills["assisted-call"]]
        results = []
        planning_text = request + ("\nProvider hint: " + provider_hint if provider_hint else "")
        company = await self._decide("company_skill", task_id, planning_text,
                                    [Candidate(id=s.name, label=s.name, description=s.description) for s in companies])
        results.append(company)
        card, missing, status, message = None, [], "unsupported", "This company is not supported."
        if company.intent not in _NO_COMPANY:
            skill = self.skills.get(company.intent)
            if skill not in companies:
                raise ValueError("Decision selected an undeclared company skill")
            used += [skill, self.skills["find-official-number"]]
            company_id = skill.metadata[SKILL_COMPANY_ID_KEY]
            ivr = _read(skill, "references/ivr-map.yaml", IvrMap)
            intent = await self._decide("task_intent", task_id, request,
                                       [Candidate(id=i.id, label=i.id, description=i.description) for i in ivr.intents])
            results.append(intent)
            status, message = "setup_required", "Please clarify what you want done with this company."
            if intent.intent not in _NO_INTENT:
                selected = next((i for i in ivr.intents if i.id == intent.intent), None)
                if selected is None:
                    raise ValueError("Decision selected an undeclared intent")
                trusted = _read(self.skills["find-official-number"], "references/trusted-sources.yaml", TrustedSources)
                official = find_official_number(company_id, intent.intent, self.fetcher, trusted_sources=trusted)
                existing_company = self.store.get_company(company_id)
                self.store.upsert_company(company_id, name=official.company_name,
                        official_number=official.official_number,
                        line_number=existing_company["line_number"] if existing_company else (official.official_number or ""),
                        sources=[s.model_dump(mode="json") for s in official.sources],
                        verified_at=self.clock.now_iso() if official.verified else None)
                if official.verified:
                    choices = {}
                    for q in _precall_questions(skill, self.mode):
                        pick = await self._decide("precall_default", task_id, request,
                                   [Candidate(id="user_chose:"+o.id, label=o.label, description=o.detail) for o in q.options])
                        choices[q.question_id] = pick
                        results.append(pick)
                    fields = _profile_fields(profile, company_id)
                    card = build_task_card(extracted=extracted, company_skill=skill, intent=intent, official=official,
                                           profile_fields=fields, precall_choices=choices, card_version=1, mode=self.mode)
                    if self.lookup is not None and skill.metadata.get(LIVE_LOOKUP_SERVICE_KEY):
                        view = await self.lookup.run(skill.metadata[LIVE_LOOKUP_SERVICE_KEY],
                                                     [extracted.service_address, fields.get("address")])
                        card = _with_lookup(card, view)
                    missing = [MissingField(key=k, label=_label(k)) for k in selected.required_fields if not fields.get(k)]
                    status = "setup_required" if missing else "ready"
                    message = "Complete the task card." if status != "ready" else "Review the task card and click Start."
                else:
                    message = "Official number could not be verified by two agreeing official sources."
        refs = [SkillRef(name=s.name, commit=s.version.commit, content_sha256=s.version.content_sha256) for s in used]
        self.store.create_task(task_id=task_id, company_id=card.company.company_id if card else
                               (skill.metadata[SKILL_COMPANY_ID_KEY] if company.intent not in _NO_COMPANY else None),
                               goal=card.goal if card else "", request_text=request,
                               task_card=card.model_dump(mode="json") if card else None, card_version=1, status=status)
        self.store.update_task(task_id, updated_at=self.clock.now_iso(),
                               intent_id=intent.intent if company.intent not in _NO_COMPANY else None)
        return PrepareTaskOut(task_id=task_id, status=status, task_card=card, missing=missing, skills=refs,
                              decisions=[DecisionSummary(point=r.point, choice=r.intent, confidence=_confidence(r)) for r in results],
                              message=message)

    async def update(
        self,
        task_id: str,
        fields: UpdateTaskFields | None = None,
        decisions: Mapping[str, str] | None = None,
    ) -> UpdateTaskOut:
        """update_task：写可代说字段与拨号前选择；card_version 加一。

        歧义表 A-19（定稿 three_values_card_nullable）：返回的 status 取 prepare_task 的 3 个值，task_card 可空
        （unsupported、号码未核实时为 null）；任务 in_call、completed、needs_follow_up、failed 时一律 task_locked。
        """
        row = self.store.get_task(task_id)
        if row is None:
            raise ToolFailure("unknown_task")
        if row["status"] not in ("ready", "setup_required", "unsupported"):
            raise ToolFailure("task_locked")
        card = self.get_card(task_id)
        updates = fields.model_dump(exclude_none=True) if fields is not None else {}
        # Validate the entire update before any persistent write.
        allowed = {"name", "phone_number", "address", "account_number"}
        if any(k not in allowed for k in updates):
            raise ToolFailure("forbidden_field", "Unknown or forbidden profile field")
        questions = {q.question_id: q for q in card.precall_decisions} if card else {}
        for key, value in (decisions or {}).items():
            if key not in questions or value not in {o.id for o in questions[key].options}:
                raise ToolFailure("invalid_option")
        try:
            if updates:
                self.store.save_profile(updates, company_id=row["company_id"])
        except ForbiddenField as exc:
            raise ToolFailure("forbidden_field", str(exc)) from None
        if card is None:
            return UpdateTaskOut(task_id=task_id, status=row["status"], task_card=None, missing=[])
        for key, value in (decisions or {}).items():
            questions[key].selected = value
            questions[key].needs_user = False
        fields_now = _profile_fields(self.store.get_profile(), row["company_id"])
        card = card.model_copy(update={"card_version": row["card_version"]+1,
                                       "shared_fields": [SharedField(key=k, label=_label(k), display=v) for k,v in fields_now.items()]})
        skill = next(s for s in self.skills.values() if s.metadata.get(SKILL_COMPANY_ID_KEY) == row["company_id"])
        if self.lookup is not None and card.simulated_line is not None and card.company.sources == []:
            view = await self.lookup.run(skill.metadata[LIVE_LOOKUP_SERVICE_KEY], [fields_now.get("address")])
            card = _with_lookup(card, view)
        ivr = _read(skill, "references/ivr-map.yaml", IvrMap)
        selected = next((i for i in ivr.intents if i.id == row["intent_id"]), None)
        if selected is None:
            raise ValueError("Stored goal does not match a declared intent")
        missing = [MissingField(key=k, label=_label(k)) for k in selected.required_fields if not fields_now.get(k)]
        status = "setup_required" if missing else "ready"
        self.store.update_task(task_id, task_card=card.model_dump(mode="json"), card_version=card.card_version,
                               status=status, updated_at=self.clock.now_iso())
        return UpdateTaskOut(task_id=task_id, status=status, task_card=card, missing=missing)

    def get_card(self, task_id: str) -> TaskCard | None:
        row = self.store.get_task(task_id)
        if row is None:
            raise ToolFailure("unknown_task")
        card = TaskCard.model_validate_json(row["task_card_json"]) if row["task_card_json"] else None
        return _card_for_mode(card, self.skills, self.mode) if card else None

    async def _decide(self, point, task_id, text, candidates):
        profile = self.store.get_profile()
        context = DecisionContext(task_card=None, allowed_fields=_profile_fields(profile, None), standing_rules=self.store.list_standing_rules(),
                                  prior_choices=[], interrupts_used=0, phase="idle")
        return await self.gate.decide(point, DecisionRequest(decision_id=uuid.uuid4().hex, point=point,
                          call_id=None, task_id=task_id, turn=None, candidates=candidates, context=context,
                          transcript=[TranscriptLine(speaker="user", text=text)]))


def build_task_card(
    *,
    extracted: ExtractedRequest,
    company_skill: Skill,
    intent: DecisionResult,
    official: OfficialNumberOut,
    profile_fields: Mapping[str, str],
    precall_choices: Mapping[str, DecisionResult],
    card_version: int,
    mode: str = "text-sim",
) -> TaskCard:
    """按公司 skill 的 ivr-map.yaml 意图模板生成 goal 与 task_summary，只把用户原话已选定的拨号前选择写进卡（AC-28、AC-29）。
    两个文件按 models.IvrMap、models.PrecallQuestionFile 读（A-G-4）；goal、task_summary 省去空值占位及前面的介词，
    再填 extracted 的字段（IvrIntent 已校验占位只能是 ExtractedRequest 的字段）。"""
    ivr = _read(company_skill, "references/ivr-map.yaml", IvrMap)
    selected = next(i for i in ivr.intents if i.id == intent.intent)
    precall = []
    for question in _precall_questions(company_skill, mode):
        pick = precall_choices.get(question.question_id)
        if pick is None or not pick.intent.startswith("user_chose:") or _confidence(pick) < 0.85:
            continue
        option = pick.intent.split(":", 1)[1]
        if option not in {o.id for o in question.options}:
            raise ValueError("Undeclared pre-call option")
        precall.append(PrecallDecision(question_id=question.question_id, question=question.question, options=question.options,
                                      default=question.default, selected=option, needs_user=False))
    trusted = TrustedSources.model_validate(yaml.safe_load((company_skill.path.parent / "find-official-number" /
                       "references/trusted-sources.yaml").read_text(encoding="utf-8")))
    domains = trusted.companies[company_skill.metadata[SKILL_COMPANY_ID_KEY]].official_domains
    from urllib.parse import urlsplit
    sources = [Source(url=s.url, title=s.title) for s in official.sources
               if (urlsplit(s.url).hostname or "").casefold() in {d.casefold() for d in domains}
               and s.number_found and _phone_digits(s.number_found) == _phone_digits(official.official_number)]
    goal = _fill(selected.goal, extracted)
    return TaskCard(card_version=card_version,
                    company=TaskCompany(company_id=company_skill.metadata[SKILL_COMPANY_ID_KEY], name=ivr.company_name,
                                        official_number=official.official_number, sources=sources),
                    goal=goal,
                    task_summary=_fill(selected.task_summary, extracted), effective_date=extracted.effective_date,
                    shared_fields=[SharedField(key=k, label=_label(k), display=v) for k,v in profile_fields.items()],
                    precall_decisions=precall, boundary="Only complete this task: " + goal)


def _is_human_decision(question):
    return "human_decision" in question.tags


def _precall_questions(skill, mode):
    questions = _read(skill, "references/pre-call-questions.yaml", PrecallQuestionFile).questions
    return [q for q in questions if mode != "live-sim" or not _is_human_decision(q)]


def _card_for_mode(card, skills, mode):
    if mode != "live-sim":
        return card
    skill = next(s for s in skills.values() if s.metadata.get(SKILL_COMPANY_ID_KEY) == card.company.company_id)
    manual = {q.question_id for q in _read(skill, "references/pre-call-questions.yaml", PrecallQuestionFile).questions
              if _is_human_decision(q)}
    return card.model_copy(update={"precall_decisions": [q for q in card.precall_decisions if q.question_id not in manual]})


def _fill(template: str, extracted: ExtractedRequest) -> str:
    """Omit unknown placeholders and their immediately preceding preposition."""
    fields = extracted.model_dump()
    for name, value in fields.items():
        if value is None:
            template = re.sub(r"(?:\s+(?:at|to|on|in|for|from|by))?\s*\{" + re.escape(name) + r"\}",
                              "", template)
    return " ".join(template.format(**fields).split())


def _read(skill, rel, model):
    return model.model_validate(yaml.safe_load((skill.path / rel).read_text(encoding="utf-8")))


def _profile_fields(profile, company_id):
    fields = {k: getattr(profile, k, None) for k in UPDATABLE_FIELD_KEYS if k != "account_number"}
    fields = {k: v for k, v in fields.items() if v}
    if company_id in profile.accounts:
        fields["account_number"] = profile.accounts[company_id]
    return fields


def _label(key):
    return key.replace("_", " ").title()


def _confidence(result):
    answers = result.jev.response.answers.values()
    return next((a.confidence for a in answers if a.confidence is not None), 0.0)


def _phone_digits(number):
    digits = "".join(c for c in (number or "") if c.isdigit())
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def line_display_name(card: TaskCard) -> str:
    return card.simulated_line.name if card.simulated_line else card.company.name


def _with_lookup(card, view):
    return card.model_copy(update={"company": card.company.model_copy(update={
        "name": view.name, "official_number": view.official_number, "sources": view.sources}),
        "simulated_line": view.simulated_line})
