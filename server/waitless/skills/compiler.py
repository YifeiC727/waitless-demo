"""把 skills 编译成一通电话要用的 CompiledCall（G 线；tech §6.9；AC-41–AC-45、AC-59；H4、H5）。

公开接口（§7）：compile_call(task, skills, profile, rules, prefs) -> CompiledCall、build_agent_prompt(...)。
"""

from collections.abc import Mapping, Sequence
import json

import yaml

from waitless.models import (CompiledCall, Preference, Profile, Skill, StandingRule, TaskCard,
                             IvrMap, Mode, PrecallQuestionFile, SkillRef, FORBIDDEN_FIELD_KEYS,
                             SKILL_COMPANY_ID_KEY, SAY_TEMPLATE_KEYS)
from waitless.skills.loader import SkillError

__all__ = ["build_agent_prompt", "compile_call", "select_allowed_fields"]


def compile_call(
    task: TaskCard,
    skills: Mapping[str, Skill],
    profile: Profile,
    rules: Sequence[StandingRule],
    prefs: Sequence[Preference],
    *,
    mode: Mode = "text-sim",
) -> CompiledCall:
    """Preserve text artifacts; live omits tools and account-holder decisions."""
    _check_mode(mode)
    companies = [s for s in skills.values()
                 if s.metadata.get(SKILL_COMPANY_ID_KEY) == task.company.company_id]
    if len(companies) != 1:
        raise SkillError("task company requires exactly one matching company skill")
    company = companies[0]
    ivr = IvrMap.model_validate(_read(company, "references/ivr-map.yaml")).model_dump()
    precall = _precall_questions(company, mode)
    task = _task_for_mode(task, precall, mode)
    option = skills["decide-option"]
    templates = _read(option, "references/say-templates.yaml")["say_templates"]
    templates = {**templates, **_read(skills["handoff-to-user"], "references/handoff.yaml")["say_templates"]}
    if not set(SAY_TEMPLATE_KEYS) <= templates.keys():
        raise SkillError("missing required say templates")
    intent_file = _read(option, "references/intents.yaml")
    allowed = select_allowed_fields(profile, intent_file["allowed_fields"], company_id=task.company.company_id)
    lexicons = _read(option, "references/lexicons.yaml")
    lexicons = {**{k: v for k, v in lexicons.items() if k != "field_cues"},
                **{f"field_cues.{k}": v for k, v in lexicons.get("field_cues", {}).items()}}
    stall = _read(option, "references/stall-phrases.yaml")["phrases"]
    rules_file = _read(skills["navigate-ivr"], "references/ivr-rules.yaml")
    merged = {"version": 1, "decision_points": {}}
    for s in skills.values():
        if "policy.yaml" in s.files:
            raw = _read(s, "policy.yaml")
            merged["decision_points"].update(raw["decision_points"])
            if "global" in raw:
                merged["global"] = raw["global"]
    merged["precall_questions"] = precall.model_dump()["questions"]
    merged["ivr_rules"] = rules_file
    intents = list(intent_file["intents"])
    intents += [{"id": f"press_field:{key}", "description": f"Enter the authorized {key}", "action": "press"}
                for key in ivr["keypad_fields"] if key in allowed]
    intents += [{"id": f"answer_precall:{d.question_id}", "description": d.question, "action": "say"}
                for d in task.precall_decisions if d.selected is not None]
    tools = _read(skills["phone-agent"], "references/tools.yaml")
    if tools.get("version") != 2 or tools.get("live_tools") != []:
        raise SkillError("phone-agent requires tools.yaml v2 with no live tools")
    return CompiledCall(
        sonic_prompt=build_agent_prompt(task=task, allowed_fields=allowed, rules=rules,
                                        skills=skills, company=company, mode=mode),
        agent_tool_descriptions={} if mode == "live-sim" else tools["text_tools"],
        jev_templates=_read(option, "jev/questions.yaml")["decision_points"], policy=merged,
        ivr_map=ivr, intents=intents, lexicons=lexicons, stall_phrases=stall,
        say_templates=templates, allowed_fields=allowed, task_card=task,
        skill_versions=[SkillRef(name=s.name, commit=s.version.commit,
                                content_sha256=s.version.content_sha256) for s in skills.values()],
        press_patterns=rules_file["press_patterns"], digit_words=rules_file["digit_words"])


def build_agent_prompt(
    *,
    task: TaskCard,
    allowed_fields: Mapping[str, str],
    rules: Sequence[StandingRule],
    skills: Mapping[str, Skill],
    company: Skill,
    mode: Mode = "text-sim",
) -> str:
    """Append authorized task data to the exact skill body and company notes.

    speaker.yaml is a Jev question, never a Sonic instruction or template.
    """
    _check_mode(mode)
    if mode == "live-sim":
        task = _task_for_mode(task, _precall_questions(company, mode), mode)
    notes_path = company.path / "references/agent-notes.md"
    notes = notes_path.read_text(encoding="utf-8") if notes_path.is_file() else ""
    data = {
        "goal": task.goal,
        "effective_date": task.effective_date,
        "precall_choices": [
            {"topic": decision.question,
             "choice": next(option.label for option in decision.options if option.id == decision.selected)}
            for decision in task.precall_decisions if decision.selected is not None
        ],
        "standing_rules": [rule.rule_text for rule in rules],
        "authorized_facts": {key: value for key, value in allowed_fields.items()
                             if key not in FORBIDDEN_FIELD_KEYS},
    }
    return (skills["phone-agent"].body + "\n\n" + notes + "\n\n# Task data\n"
            + json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _check_mode(mode: Mode) -> None:
    if mode not in ("live-sim", "text-sim", "replay"):
        raise SkillError(f"unsupported compile mode: {mode}")


def _precall_questions(company: Skill, mode: Mode) -> PrecallQuestionFile:
    precall = PrecallQuestionFile.model_validate(_read(company, "references/pre-call-questions.yaml"))
    # This tag is mode metadata, not a phrase to match in the text decision policy.
    questions = [q.model_copy(update={"tags": [tag for tag in q.tags if tag != "human_decision"]})
                 for q in precall.questions
                 if mode != "live-sim" or "human_decision" not in q.tags]
    return precall.model_copy(update={"questions": questions})


def _task_for_mode(task: TaskCard, precall: PrecallQuestionFile, mode: Mode) -> TaskCard:
    if mode != "live-sim":
        return task
    # KeypadDecider takes prior_choices from this copy. Old/undeclared choices
    # cannot grant live authorization; leave the caller's persisted card intact.
    active_ids = {q.question_id for q in precall.questions}
    return task.model_copy(update={"precall_decisions": [d for d in task.precall_decisions
                                                        if d.question_id in active_ids]})


def select_allowed_fields(profile: Profile, whitelist: Sequence[str], *, company_id: str) -> dict[str, str]:
    """按白名单从档案取可代说字段；值为空的不放；FORBIDDEN_FIELD_KEYS 一律排除。
    歧义表 A-04（定稿 map_by_company）：白名单含 account_number 时取 profile.accounts[company_id]，
    放进结果的 "account_number" 键；别家公司的账户号不进。"""

    fields = {}
    for key in whitelist:
        if key in FORBIDDEN_FIELD_KEYS:
            continue
        if key == "account_number":
            value = profile.accounts.get(company_id)
        elif key in Profile.model_fields and key != "accounts":
            value = getattr(profile, key, None)
        else:
            continue
        if value:
            fields[key] = value
    return fields


def _read(skill: Skill, relative: str):
    try:
        return yaml.safe_load((skill.path / relative).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SkillError(f"cannot read {skill.name}/{relative}") from exc
