"""把 skills 编译成一通电话要用的 CompiledCall（G 线；tech §6.9；AC-41–AC-45、AC-59；H4、H5）。

公开接口（§7）：compile_call(task, skills, profile, rules, prefs) -> CompiledCall、build_sonic_prompt(...)。
"""

from collections.abc import Mapping, Sequence

from waitless.models import CompiledCall, Preference, Profile, Skill, StandingRule, TaskCard

__all__ = ["build_sonic_prompt", "compile_call", "select_allowed_fields"]


def compile_call(
    task: TaskCard,
    skills: Mapping[str, Skill],
    profile: Profile,
    rules: Sequence[StandingRule],
    prefs: Sequence[Preference],
) -> CompiledCall:
    """产出 sonic_prompt、jev_templates、policy、ivr_map 等（AC-41）。ivr_map 来自公司 skill 的 references/ivr-map.yaml。"""
    # todo(G): 按 task.company.company_id 找 metadata.company-id 相同的公司 skill。
    # todo(G): jev_templates 放 decide-option 的 DP-C1、C2、E1、E2 题目（来自 jev/questions.yaml）。
    # todo(G): policy 是合并后的 decide-option policy.yaml（交给 decision.policy.Policy.load 校验）。
    # todo(G): intents（references/intents.yaml）、lexicons、stall_phrases、say_templates 原样带上；
    #          say_templates 必须包含 models.SAY_TEMPLATE_KEYS（handoff-to-user 的 handoff.yaml 与
    #          decide-option 的 say-templates.yaml 合并；歧义表 A-07）。策略自用的键（answer_precall 等）G 自定。
    # todo(G): A-16（定稿 field_templates）：say-templates.yaml 增加 open_answer.<field> 模板（键前缀
    #          models.OPEN_ANSWER_TEMPLATE_PREFIX，如 open_answer.address、open_answer.start_date），原样带进 say_templates；
    #          lexicons.yaml 增加 field_cues: {<field>: [问句线索…]}，编译时展平成 lexicons["field_cues.<field>"]
    #          （前缀 models.FIELD_CUES_PREFIX），给 E 的 TextActionBackend 用。
    # todo(G): allowed_fields = select_allowed_fields(profile, 白名单, company_id=task.company.company_id)；
    #          forbidden 字段永远不进（AC-42）。
    # todo(G): prefs 参数保留（§6.9 签名），但 CompiledCall 不带偏好：偏好由 LiveCall/TextCall 每轮从 Store 取出，
    #          经 PriorChoice.from_preference 放进 DecisionContext.prior_choices（A-03，定稿 prior_choices_source）。
    # todo(G): skill_versions 记录全部用到的 skill（name、commit、content_sha256）。
    # todo(G): ivr_map = models.IvrMap.model_validate(公司 skill 的 ivr-map.yaml).model_dump()（缺口 A-G-4）。
    # todo(G): 缺口 E-G-4（定稿 compiled_field）、G-G-03：press_patterns、digit_words 取 navigate-ivr 的
    #          references/ivr-rules.yaml（press_patterns 正则列表、digit_words 数字词 → 按键），填进 CompiledCall 的同名字段；
    #          D 的 LiveCall/TurnDetector 与 E 的 TextCall 只从这里读，不再读 skills 目录。
    raise NotImplementedError


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
    raise NotImplementedError


def select_allowed_fields(profile: Profile, whitelist: Sequence[str], *, company_id: str) -> dict[str, str]:
    """按白名单从档案取可代说字段；值为空的不放；FORBIDDEN_FIELD_KEYS 一律排除。
    歧义表 A-04（定稿 map_by_company）：白名单含 account_number 时取 profile.accounts[company_id]，
    放进结果的 "account_number" 键；别家公司的账户号不进。"""
    raise NotImplementedError
