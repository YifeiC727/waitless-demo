"""把 skills 编译成一通电话要用的 CompiledCall（G 线；tech §6.9；AC-41–AC-45、AC-59；H4、H5）。

公开接口（§7）：compile_call(task, skills, profile, rules, prefs) -> CompiledCall、build_sonic_prompt(...)。
"""

from collections.abc import Mapping, Sequence
import yaml

from waitless.models import (CompiledCall, Preference, Profile, Skill, StandingRule, TaskCard,
                             IvrMap, PrecallQuestionFile, SkillRef, FORBIDDEN_FIELD_KEYS,
                             SKILL_COMPANY_ID_KEY, SAY_TEMPLATE_KEYS, SONIC_INSTRUCTION_PREFIX)
from waitless.skills.loader import SkillError

__all__ = ["build_sonic_prompt", "compile_call", "select_allowed_fields"]


def compile_call(
    task: TaskCard,
    skills: Mapping[str, Skill],
    profile: Profile,
    rules: Sequence[StandingRule],
    prefs: Sequence[Preference],
) -> CompiledCall:
    """产出 sonic_prompt、jev_templates、policy、ivr_map 等（AC-41）。ivr_map 来自公司 skill 的 references/ivr-map.yaml。"""
    companies = [s for s in skills.values()
                 if s.metadata.get(SKILL_COMPANY_ID_KEY) == task.company.company_id]
    if len(companies) != 1:
        raise SkillError("task company requires exactly one matching company skill")
    company = companies[0]
    ivr = IvrMap.model_validate(_read(company, "references/ivr-map.yaml")).model_dump()
    precall = PrecallQuestionFile.model_validate(_read(company, "references/pre-call-questions.yaml"))
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
    # Carry skill data needed by policy; the company map remains the frozen IvrMap dump.
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
    return CompiledCall(
        sonic_prompt=build_sonic_prompt(task=task, user_name=allowed.get("name", "the account holder"),
                                        allowed_fields=allowed, stall_phrases=stall, skills=skills),
        jev_templates=_read(option, "jev/questions.yaml")["decision_points"], policy=merged,
        ivr_map=ivr, intents=intents, lexicons=lexicons, stall_phrases=stall,
        say_templates=templates, allowed_fields=allowed, task_card=task,
        skill_versions=[SkillRef(name=s.name, commit=s.version.commit,
                                content_sha256=s.version.content_sha256) for s in skills.values()],
        press_patterns=rules_file["press_patterns"], digit_words=rules_file["digit_words"])


def build_sonic_prompt(
    *,
    task: TaskCard,
    user_name: str,
    allowed_fields: Mapping[str, str],
    stall_phrases: Sequence[str],
    skills: Mapping[str, Skill],
) -> str:
    """§6.9 的 8 节：
    1 身份：开场说明自己是代表 {name} 的 AI 助手；被问是否本人时如实说不是（AC-43）
    2 任务目标和任务卡的决定
    3 可代说的事实（allowed_fields 白名单）
    4 开放式问题按目标自己组织语言（AC-44）
    5 遇到选择、同意、确认、身份类问题或提示里没有的信息时，只说 stall 说法之一，并逐条列出（AC-45）
    6 不报数字、不按键
    7 收到以 models.SONIC_INSTRUCTION_PREFIX 开头的文字时照它说
    8 不编造
    提示里不放任何转写（AC-59），也不放 forbidden 字段（AC-42）。"""
    fields = {k: v for k, v in allowed_fields.items() if k not in FORBIDDEN_FIELD_KEYS}
    sections = _read(skills["decide-option"], "references/say-templates.yaml")["sonic_sections"]
    decisions = []
    for decision in task.precall_decisions:
        if decision.selected is not None:
            label = next(o.label for o in decision.options if o.id == decision.selected)
            decisions.append(f"{decision.question_id}: {label}")
        else:
            decisions.append(f"{decision.question_id}: ask the account holder")
    values = dict(user_name=user_name, goal=task.goal, effective_date=task.effective_date or "",
                  decisions="\n".join(decisions),
                  facts="\n".join(f"{k}: {v}" for k, v in fields.items()),
                  phrases="\n".join(f"- {p}" for p in stall_phrases), prefix=SONIC_INSTRUCTION_PREFIX.strip())
    return "\n\n".join(sections[key].format(**values)
                        for key in ("identity", "task", "facts", "open", "stall", "dtmf", "injection", "honesty"))


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
