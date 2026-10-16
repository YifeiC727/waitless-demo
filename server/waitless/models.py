"""Waitless 共享数据模型（第 3 步骨架写，冻结：各施工线不许改）。

本文件只放跨模块、跨施工线的数据结构，字段名与 tech.v4.md §6 完全一致（v2→v4 的 §6 一字未改）：
  §6.1 MCP 工具的输入输出与共享类型（TaskCard、PendingChoice、Media）
  §6.2 /assistant/turn、/app-config、/healthz
  §6.3 模拟运营商 HTTP API（电话线消息在 call/line_protocol.py）
  §6.4 转接 WebSocket 文本帧
  §6.5 CompanyTurn、DecisionRequest、DecisionResult、ActionRecord
  §6.6 Jev 请求与返回
  §6.7 大模型任务 L1–L5 的输入输出
  §6.8 profile、preferences、standing_rules、turns（trace 事件）
  §6.9 Skill、CompiledCall
  §6.10 公司与场景配置
  §6.12 回放与评测格式
  §5.3 engine.yaml 的结构（EngineConfig）。Settings 与 SETTINGS_ENV 在 config.py，归 A 线（歧义表 A-27）。
  §7   results.finalize 的一通电话结论 CallResult（歧义表 A-24）
  §6.9 公司 skill 与 find-official-number 的 references 文件结构（IvrMap、PrecallQuestionFile、TrustedSources；缺口 A-G-4）

约定：
- 规格里没有标 `|null` 的字段一律必填、非空值；标了 `?` 的字段可省略。
- 时间戳字段用带时区的 datetime，JSON 里是 ISO 8601 字符串。
- 标「骨架补充」或「A-xx」的字段、常量与校验是 §6 没写明、但跨线协作必须定下来的细节，
  写法按 RUN/step3/ambiguity-decisions.md 的定稿（Jev 逐条裁定）；对照见 skeleton-notes.md 第 5 节。
  标「X-G-n」「R2-1」「D14-1」的是 r3 缺口定稿（RUN/step3/gap-decisions-r3.md）；对照见 skeleton-notes.md 第 10 节。
  标「D-G-n」的是 r4（D 线）缺口定稿（RUN/step3/gap-decisions-r4.md）；对照见 skeleton-notes.md 第 13 节。
- 本文件不含任何配置值（端口、主机、路径、URL、密钥）；只有协议常量。
"""

from pathlib import Path
from typing import Annotated, Any, Literal, Union

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StrictBool,
    StrictInt,
    model_validator,
)

# ---------------------------------------------------------------------------
# 基础类型
# ---------------------------------------------------------------------------

Id = Annotated[str, Field(min_length=1)]
Probability = Annotated[float, Field(ge=0.0, le=1.0)]
NonNegInt = Annotated[int, Field(ge=0)]
Digits = Annotated[str, Field(pattern=r"^[0-9*#]+$")]
CallSid = Annotated[str, Field(pattern=r"^CA[0-9a-fA-F]{32}$")]


class _Strict(BaseModel):
    """所有共享模型的基类：不接受未声明的字段。"""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# 枚举与协议常量
# ---------------------------------------------------------------------------

Mode = Literal["live-sim", "text-sim", "replay"]  # §5.1
MODES: tuple[str, ...] = ("live-sim", "text-sim", "replay")

# §6.1 九个 MCP 工具（顺序同表）
ToolName = Literal[
    "find_official_number",
    "prepare_task",
    "update_task",
    "start_assisted_call",
    "get_call_status",
    "answer_pending_choice",
    "join_call",
    "return_to_agent",
    "get_case_result",
]
TOOL_NAMES: tuple[str, ...] = (
    "find_official_number",
    "prepare_task",
    "update_task",
    "start_assisted_call",
    "get_call_status",
    "answer_pending_choice",
    "join_call",
    "return_to_agent",
    "get_case_result",
)
# R13、AC-06、AC-23：只能由用户点按钮触发的 4 个工具
USER_ACTION_TOOLS: tuple[str, ...] = (
    "start_assisted_call",
    "answer_pending_choice",
    "join_call",
    "return_to_agent",
)
# §6.6 DP-A1 的 4 个固定动作（extra_criteria）
ASSISTANT_FIXED_ACTIONS: tuple[str, ...] = (
    "reply_only",
    "ask_clarify",
    "present_task_card",
    "wait_for_call",
)

# §6.6 八个决策点
DecisionPoint = Literal[
    "assistant_next_action",
    "company_skill",
    "task_intent",
    "precall_default",
    "call_round",
    "said_as_decided",
    "call_outcome",
    "result_item_kind",
    "agent_tool",  # v5.20 E133：live 的 AI 工具调用；不进 DECISION_POINT_CODES
]
DECISION_POINT_CODES: dict[str, str] = {
    "assistant_next_action": "DP-A1",
    "company_skill": "DP-P1",
    "task_intent": "DP-P2",
    "precall_default": "DP-P3",
    "call_round": "DP-C1",
    "said_as_decided": "DP-C2",
    "call_outcome": "DP-E1",
    "result_item_kind": "DP-E2",
}
# 歧义表 A-02（定稿 wait_except_c2）：这 6 个点不对应通话动作，DecisionResult.action 固定 wait，
# 选择结果放 intent（工具名、skill 名、意图 id、keep_default/user_chose:<id>/ask_user、结果类别）。
WAIT_ONLY_POINTS: tuple[str, ...] = (
    "assistant_next_action",
    "company_skill",
    "task_intent",
    "precall_default",
    "call_outcome",
    "result_item_kind",
)
# 歧义表 A-02：DP-C2 said_as_decided 的三种结论（intent）与对应动作：
#   said_ok       核对通过                          action=wait
#   reinject      第一次未过（request.attempt=0）     action=say，say_text=原注入文字（template_vars 的 decision_summary）
#   safe_default  第二次仍未过（request.attempt=1）   action 取 policy.yaml 的 said_as_decided.safe_default
#                 （建议 end，这通电话结果记 needs_follow_up），reason_code=say_mismatch，并写 say_mismatch 事件
SAID_OK = "said_ok"
SAID_REINJECT = "reinject"
SAID_SAFE_DEFAULT = "safe_default"
SAID_INTENTS: tuple[str, ...] = (SAID_OK, SAID_REINJECT, SAID_SAFE_DEFAULT)
SAY_MISMATCH = "say_mismatch"
# DP-C2 instructions 里的占位名（§6.6）。值 = 本次注入的文字（填好占位的话术模板，不含
# SONIC_INSTRUCTION_PREFIX），由 D、E 的执行器放进 DecisionRequest.template_vars（AC-72「取本次 say 模板的文字」）。
SAID_SUMMARY_VAR = "decision_summary"
# §6.6 DP-C1 call_round 恰好 4 题（AC-46）
CALL_ROUND_QUESTIONS: dict[str, str] = {
    "menu_choice": "choice",
    "call_phase": "choice",
    "needs_user": "noul",
    "stakes": "score",
}
NONE_OF_THE_ABOVE = "none_of_the_above"

# §6.6 对话轮应答意图（固定 id；带参数的两种用前缀）
ROUND_INTENTS: tuple[str, ...] = (
    "open_answer",
    "decline_offer",
    "state_ai_identity",
    "request_handoff",
    "ask_user",
    "confirm_and_end",
    "ask_repeat",
    "wait",
)
PRESS_FIELD_PREFIX = "press_field:"
ANSWER_PRECALL_PREFIX = "answer_precall:"

# §6.1 get_call_status.phase
CallPhase = Literal[
    "idle",
    "dialing",
    "ivr",
    "hold",
    "ai_agent",
    "live_rep",
    "awaiting_user",
    "handoff_ready",
    "in_handoff",
    "resuming",
    "wrapping_up",
    "ended",
]
# §6.6 Jev call_phase 题的 criteria id（与 CallPhase 不同）
JevCallPhase = Literal["ivr_menu", "hold", "ai_agent", "live_rep", "identity_check", "ended"]
# 缺口 D-G-11（定稿 shared_mapping）：get_call_status.phase 的取法，D 的 LiveCall 与 E 的 TextCall 共用。
#   拨号后到第一轮决策前为 dialing；之后按最近一轮 DecisionResult.call_phase 映射；
#   动作驱动的 awaiting_user（有 open 的待决选项）、handoff_ready、in_handoff、resuming 优先于映射；
#   挂断后为 ended；还没开始通话为 idle。
CALL_PHASE_FROM_JEV: dict[str, str] = {
    "ivr_menu": "ivr",
    "hold": "hold",
    "ai_agent": "ai_agent",
    "live_rep": "live_rep",
    "identity_check": "live_rep",
    "ended": "wrapping_up",
}
# 优先于映射的动作驱动 phase（D-G-11）
ACTION_DRIVEN_PHASES: tuple[str, ...] = ("awaiting_user", "handoff_ready", "in_handoff", "resuming")
# §6.8 tasks.status
TaskStatus = Literal[
    "setup_required", "ready", "in_call", "completed", "needs_follow_up", "failed", "unsupported"
]
# §6.1 prepare_task.status
PrepareStatus = Literal["ready", "setup_required", "unsupported"]
# §6.1 get_case_result.outcome
CaseOutcome = Literal["in_progress", "confirmed", "needs_follow_up", "failed"]
FinalOutcome = Literal["confirmed", "needs_follow_up", "failed"]
# §6.5 DecisionResult.action
Action = Literal["press", "say", "release", "ask_user", "handoff", "end", "wait"]
PendingStatus = Literal["open", "answered", "expired"]
HandoffState = Literal["ready", "active", "ended"]
TailSpeaker = Literal["company", "waitless"]
TranscriptSpeaker = Literal["company", "waitless", "user"]
JevQuestionType = Literal["choice", "noul", "score"]
LlmTaskName = Literal[
    "extract_request",
    "phrase_reply",
    "extract_offer_options",
    "extract_result_items",
    "write_summary",
]
LLM_TASK_NAMES: tuple[str, ...] = (
    "extract_request",
    "phrase_reply",
    "extract_offer_options",
    "extract_result_items",
    "write_summary",
)

# §6.1 错误码（content[0].text 里的 code）
ErrorCode = Literal[
    "unknown_task",
    "forbidden_field",
    "task_locked",
    "invalid_option",
    "start_not_confirmed",
    "card_version_mismatch",
    "task_not_ready",
    "call_in_progress",
    "unsupported",
    "unknown_choice",
    "handoff_not_ready",
    "no_active_handoff",
]

# §6.11 trace 事件类型（turns.event_type）
TraceEventType = Literal[
    "call_started",
    "call_ended",
    "hangup",
    "remote_hangup",
    "company_turn",
    "waitless_utterance",
    "decision",
    "action",
    "dtmf_sent",
    "injection_sent",
    "guard_released",
    "guard_blocked",
    "say_mismatch",
    "pending_choice_created",
    "user_answered",
    "pending_expired",
    "handoff_announced",
    "handoff_ready",
    "handoff_started",
    "handoff_ended",
    "resume_injected",
    "user_action",
    "error",
    "agent_tool_call",
    "agent_tool_result",
    "status_updated",
]
# §6.11：这 5 类必须带 decision_id，并指向已有的 decision 事件（AC-34）。
# 歧义表 A-25（定稿 keep_all_relax_trace_event）：这条规则不在 TraceEvent 模型里校验，
# 由 Trace.event() 写入时拒绝、Trace.check_call() 读回时报成 Violation（读回的坏行照样能构造成 TraceEvent）。
CONSEQUENTIAL_EVENTS: frozenset[str] = frozenset(
    {"dtmf_sent", "injection_sent", "pending_choice_created", "handoff_started", "hangup"}
)
# decision 事件写入时必须带齐的字段（同样由 Trace.event() 检查，A-25）
DECISION_EVENT_REQUIRED: tuple[str, ...] = (
    "decision_id",
    "decision_point",
    "jev_request",
    "jev_response",
    "policy_decision",
)
# 歧义表 A-08（定稿 convention_plus_turn_link）：各事件 action_json 里的约定键。
#   company_turn    {kind: "menu"|"speech", seq: CompanyTurn.seq}；事件 t_ms = CompanyTurn.t_end_ms
#   dtmf_sent       {digits}
#   injection_sent  {text, template}（template 是话术键或 null；text 不含 SONIC_INSTRUCTION_PREFIX）
#   user_action     {name: "start_confirmed"|"choice_answered", shown_ms, acted_ms}
#   decision        point=call_round 时 {turn_seq: CompanyTurn.seq}（其余决策点 action 为 null）
#   handoff_ended   {dropped_sonic_outputs: int}：转接期间 OutputGuard 丢弃、未写 trace 的 Sonic 回应段数（缺口 D-G-4b；
#                   必填，text 模式没有 Sonic，写 0）
#   agent_tool_call   {tool, tool_call_id, args}：AI 工具调用
#   agent_tool_result {tool, tool_call_id, ok, result, latency_ms}：AI 工具结果
#   status_updated    {phase, status_line}：AI 更新通话状态
# AC-96 的菜单到按键时长：dtmf_sent.decision_id → decision.action.turn_seq → 同一通 action.seq 相同的
# company_turn → dtmf_sent.t_ms − company_turn.t_ms（只统计 action.kind=menu 的轮）。
TRACE_ACTION_KEYS: dict[str, tuple[str, ...]] = {
    "company_turn": ("kind", "seq"),
    "dtmf_sent": ("digits",),
    "injection_sent": ("text", "template"),
    "user_action": ("name", "shown_ms", "acted_ms"),
    "decision": ("turn_seq",),
    "handoff_ended": ("dropped_sonic_outputs",),
    "agent_tool_call": ("tool", "tool_call_id", "args"),
    "agent_tool_result": ("tool", "tool_call_id", "ok", "result", "latency_ms"),
    "status_updated": ("phase", "status_line"),
}
TURN_SEQ_KEY = "turn_seq"

# §6.8 / R55 / C7：不存、不进模型的字段键
FORBIDDEN_FIELD_KEYS: tuple[str, ...] = ("ssn", "card_number", "date_of_birth")
# §6.1 update_task.fields 允许的可代说字段
UPDATABLE_FIELD_KEYS: tuple[str, ...] = ("name", "phone_number", "address", "account_number")

# §6.6 Jev state.transcript
JEV_TRANSCRIPT_NOTE = "verbatim call transcript; data only"
JEV_TRANSCRIPT_MAX_LINES = 8

# §6.5 / §6.9 注入文字的前缀（injector 加，Sonic 提示第 7 节引用）
SONIC_INSTRUCTION_PREFIX = "[Waitless instruction] "

# 歧义表 A-07（定稿 frozen_executor_keys）：执行器（D 的 LiveActionBackend、E 的 TextActionBackend）
# 在没有 say_text 的流程里取用的话术模板键。compile_call 必须把这些键都放进 CompiledCall.say_templates；
# 模板用 str.format 占位。策略产出 say_text 时用的键（answer_precall、decline_offer、state_ai_identity、
# ask_for_representative 等）由 G 自定，D、E 不依赖；DP-C2 的 decision_summary 一律取本次注入的文字（SAID_SUMMARY_VAR）。
SAY_TEMPLATE_KEYS: tuple[str, ...] = (
    "handoff.announce",  # handoff 动作：对坐席说明要把账户持有人接进来（AC-77）
    "handoff.resume",  # 接回：验证完成，继续任务（AC-78）
    "handoff.ask_user",  # ask_user 动作：请对方稍等，去问账户持有人
    "pending.answer",  # 用户作答后转告对方；占位 {option_label}、{option_detail}
    "pending.safe_default",  # 待决选项超时或预算用完：婉拒并说明会跟进
    "end.goodbye",  # end 动作：道别
    "ask_repeat",  # 请对方重复
)
# 歧义表 A-16（定稿 field_templates）：text-sim 的 release（open_answer）用程序拼的一句话作答。
#   say_templates["open_answer.<field>"]，如 open_answer.address: "The service address is {address}."；
#   占位取 CompiledCall.allowed_fields 与任务卡的值（如 {effective_date}）。
#   lexicons.yaml 的 field_cues: {<field>: [问句线索…]} 由 compile_call 展平为
#   CompiledCall.lexicons["field_cues.<field>"]（保持 lexicons 的 dict[str, list[str]] 形状）。
#   TextActionBackend 用线索把对方的问句对到字段；对不上就用 say_templates["ask_repeat"]。不调用 Lite。
OPEN_ANSWER_TEMPLATE_PREFIX = "open_answer."
FIELD_CUES_PREFIX = "field_cues."

# 歧义表 A-17（定稿 reserved_hangup_target）：场景里的保留目标名，不是一个步骤。
# options / next / on_done 可以指向它；ScenarioEngine 进入它时先说 CompanyConfig.dead_end_text，再发 kind=hangup。
DEAD_END_TARGET = "dead_end"

# 缺口 G-G-05（定稿 use_preference_pending_answer）：策略第 6 步按偏好作答时的意图前缀。
# intent = "use_preference:<question_key>"、action=say，say_text 用 say_templates["pending.answer"]
# （option_label 填 Preference.choice，即用户当时所选选项的 label 原文）。
USE_PREFERENCE_PREFIX = "use_preference:"

# 缺口 A-G-4（定稿 frozen_models）：SKILL.md frontmatter 的 metadata 键（§6.9）。若 agentskills 规范不允许，
# 按 §6.9 挪到 references/skill.yaml，键名不变。
SKILL_KIND_KEY = "waitless-kind"
SKILL_COMPANY_ID_KEY = "company-id"
SKILL_KINDS: tuple[str, ...] = ("client", "harness", "company")

# §6.3 / §6.4：令牌无效、过期或重复使用时的 WebSocket 关闭码
WS_CLOSE_UNAUTHORIZED = 4401
# §6.4 转接音频格式（协议常量）
HANDOFF_SAMPLE_RATE = 16000
HANDOFF_FRAME_BYTES = 640  # PCM16 LE 单声道 16 kHz × 20 ms


# ---------------------------------------------------------------------------
# §6.1 共享类型
# ---------------------------------------------------------------------------


class ToolError(_Strict):
    """isError 结果的 content[0].text（JSON）。"""

    code: ErrorCode
    message: str


class ApiError(_Strict):
    """模拟运营商等内部 HTTP API 的错误体，如 404 unknown_number（§6.3）。code 取 CARRIER_ERROR_CODES。"""

    code: Id
    message: str


# 缺口 B-G-3（定稿 reject_4xx）、B-G-4（定稿 as_fake_carrier）：模拟运营商 API 的状态码与错误码（tech 下一版补进 §6.3 表）
#   POST /carrier/calls                成功 200 {call_sid, status:"queued"}；号码未知 404 unknown_number
#   POST /carrier/calls/{sid}/hangup   成功 200 {call_sid, status:"completed"}；call_sid 未知 404 unknown_call
#   PUT /sim/scenario                  成功 200 回显；线路未知 404 unknown_line；场景不在该线路所属公司的
#                                      list_scenarios(company_dir, company_id) 里 404 unknown_scenario，当前选择不变
#   GET /sim/scenario?line_number=     成功 200；线路未知 404 unknown_line
CARRIER_ERROR_CODES: tuple[str, ...] = ("unknown_number", "unknown_call", "unknown_line", "unknown_scenario")


class Source(_Strict):
    url: Id
    title: str


class SourceCheck(_Strict):
    """find_official_number.sources 的一项。"""

    url: Id
    title: str
    number_found: str | None


class Option(_Strict):
    id: Id
    label: str
    detail: str


class Candidate(_Strict):
    """DecisionRequest.candidates 的一项（§6.5）。"""

    id: Id
    label: str
    description: str


class SharedField(_Strict):
    key: Id
    label: str
    display: str


class MissingField(_Strict):
    key: Id
    label: str


class TaskCompany(_Strict):
    company_id: Id
    name: str
    official_number: str
    sources: list[Source]


class PrecallDecision(_Strict):
    question_id: Id
    question: str
    options: list[Option]
    default: str | None
    selected: str | None
    needs_user: bool

    @model_validator(mode="after")
    def _ids_refer_to_options(self) -> "PrecallDecision":
        ids = [o.id for o in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError("precall option ids must be unique")
        for name in ("default", "selected"):
            value = getattr(self, name)
            if value is not None and value not in ids:
                raise ValueError(f"{name}={value!r} is not one of the options")
        return self


class SimulatedLine(_Strict):
    name: str
    notice: str


class TaskCard(_Strict):
    card_version: StrictInt = Field(ge=1)
    company: TaskCompany
    goal: str
    task_summary: str
    effective_date: str | None
    shared_fields: list[SharedField]
    precall_decisions: list[PrecallDecision]
    boundary: str
    simulated_line: SimulatedLine | None = None


class PendingChoice(_Strict):
    """question_text 是对方原话，只作数据显示；options 1–3 个（AC-09）。"""

    choice_id: Id
    question_text: str
    options: list[Option] = Field(min_length=1, max_length=3)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    status: PendingStatus

    @model_validator(mode="after")
    def _check(self) -> "PendingChoice":
        ids = [o.id for o in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError("pending option ids must be unique")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self


class Media(_Strict):
    ai_media: Literal["connected", "disconnected"]
    recording: Literal[False]
    transcription: bool


class JoinMedia(_Strict):
    """join_call 与转接 ready 帧里的 media（三项都是关）。"""

    ai_media: Literal["disconnected"]
    recording: Literal[False]
    transcription: Literal[False]


class ReturnMedia(_Strict):
    ai_media: Literal["connected"]
    recording: Literal[False]
    transcription: Literal[True]


# ---------------------------------------------------------------------------
# §6.9 Skill 与版本
# ---------------------------------------------------------------------------


class SkillVersion(_Strict):
    """commit 是 40 位十六进制，不在 git 里时为 "unknown"（AC-40）。"""

    commit: Annotated[str, Field(pattern=r"^([0-9a-f]{40}|unknown)$")]
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    dirty: bool


class SkillRef(_Strict):
    """prepare_task.skills 的一项；也用于 CompiledCall.skill_versions 与 trace。"""

    name: Id
    commit: Annotated[str, Field(pattern=r"^([0-9a-f]{40}|unknown)$")]
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SkillCommit(_Strict):
    """get_call_status.debug.skills 与回放文件头的一项。"""

    name: Id
    commit: Annotated[str, Field(pattern=r"^([0-9a-f]{40}|unknown)$")]


class Skill(_Strict):
    """skills/loader.py load_skills() 的产物（§6.9）。files 是相对 skill 目录的路径。"""

    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")]
    description: Annotated[str, Field(min_length=1, max_length=1024)]
    metadata: dict[str, Any]
    body: str
    path: Path
    files: list[str]
    version: SkillVersion


# ---------------------------------------------------------------------------
# §6.9 references 文件的结构（缺口 A-G-4，定稿 frozen_models；A、G 都按这些模型校验读取）
# ---------------------------------------------------------------------------


def _template_fields(template: str) -> set[str]:
    """str.format 模板里出现的占位名（{a}、{a.b} 记作 a）。"""
    import string

    return {name.split(".", 1)[0].split("[", 1)[0] for _, name, _, _ in string.Formatter().parse(template) if name}


def _unique(values: list[str], what: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{what} must be unique")


class IvrIntent(_Strict):
    """公司 skill references/ivr-map.yaml 的 intents[] 一项（§6.9；DP-P2 的候选，AC-28）。

    goal、task_summary 是 str.format 模板，占位只能是 ExtractedRequest 的字段名（L1 抽出的值）。
    required_fields（缺口 A-G-5，定稿 intent_required_fields）：办这件事必需的可代说字段，
    ⊆ UPDATABLE_FIELD_KEYS；TaskService 按 DP-P2 选中的意图取它，一次列出缺的（AC-29）。
    menu_path：§6.9 的「菜单路径」，按键序列，只作参考与评测期望，可以为空。
    """

    id: Id
    description: Id
    goal: Id
    task_summary: Id
    required_fields: list[str] = Field(default_factory=list)
    menu_path: list[Digits] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "IvrIntent":
        allowed = set(ExtractedRequest.model_fields)
        for name in ("goal", "task_summary"):
            extra = _template_fields(getattr(self, name)) - allowed
            if extra:
                raise ValueError(f"{name} template uses {sorted(extra)}; placeholders must be ExtractedRequest fields")
        _unique(self.required_fields, "required_fields")
        bad = [f for f in self.required_fields if f not in UPDATABLE_FIELD_KEYS]
        if bad:
            raise ValueError(f"required_fields {bad} are not speakable fields (UPDATABLE_FIELD_KEYS)")
        return self


class IvrMap(_Strict):
    """公司 skill 的 references/ivr-map.yaml（§6.9）。CompiledCall.ivr_map 是它的 model_dump()（AC-41）。

    confirmation_pattern：结果流程核对确认号用的正则（AC-37），必须能编译。
    keypad_fields：公司允许按键输入的可代说字段（press_field:<key> 候选的前提，AC-53），⊆ UPDATABLE_FIELD_KEYS。
    """

    version: Literal[1] | None = None
    company_name: Id
    intents: list[IvrIntent] = Field(min_length=1)
    confirmation_pattern: Id
    keypad_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "IvrMap":
        import re

        _unique([i.id for i in self.intents], "intent ids")
        try:
            re.compile(self.confirmation_pattern)
        except re.error as exc:
            raise ValueError(f"confirmation_pattern is not a valid regex: {exc}") from exc
        _unique(self.keypad_fields, "keypad_fields")
        bad = [f for f in self.keypad_fields if f not in UPDATABLE_FIELD_KEYS]
        if bad:
            raise ValueError(f"keypad_fields {bad} are not speakable fields (UPDATABLE_FIELD_KEYS)")
        return self


class PrecallQuestion(_Strict):
    """references/pre-call-questions.yaml 的 questions[] 一项（§6.9；AC-29、AC-30、AC-54）。

    default 是选项 id 之一或 null；tags 是 AC-54 匹配本轮文字用的关键词。
    """

    question_id: Id
    question: Id
    options: list[Option] = Field(min_length=2)
    default: str | None
    stakes: Annotated[int, Field(ge=0, le=3)]
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "PrecallQuestion":
        ids = [o.id for o in self.options]
        _unique(ids, "precall option ids")
        if self.default is not None and self.default not in ids:
            raise ValueError(f"default={self.default!r} is not one of the options")
        return self


class PrecallQuestionFile(_Strict):
    """公司 skill 的 references/pre-call-questions.yaml。"""

    version: Literal[1] | None = None
    questions: list[PrecallQuestion]

    @model_validator(mode="after")
    def _check(self) -> "PrecallQuestionFile":
        _unique([q.question_id for q in self.questions], "question ids")
        return self


class TrustedCompany(_Strict):
    """trusted-sources.yaml 的 companies.<company_id>。sources 可以含非官方域名的来源：列出但不计入（AC-26）。"""

    name: Id
    official_domains: list[Id] = Field(min_length=1)
    sources: list[Source] = Field(min_length=1)


class TrustedSources(_Strict):
    """find-official-number skill 的 references/trusted-sources.yaml（§6.9；AC-26）。"""

    version: Literal[1] | None = None
    min_agreeing_sources: PositiveInt
    companies: dict[Id, TrustedCompany]


# ---------------------------------------------------------------------------
# §6.1 工具输入
# ---------------------------------------------------------------------------


class FindOfficialNumberIn(_Strict):
    company: Annotated[str, Field(min_length=1, max_length=100)]
    intent: Annotated[str, Field(min_length=1, max_length=200)]


class PrepareTaskIn(_Strict):
    request: Annotated[str, Field(min_length=1, max_length=500)]
    provider_hint: str | None = None


class UpdateTaskFields(BaseModel):
    """update_task.fields。

    歧义表 A-05（定稿 allow_extra_reject_unknown）：允许未声明的键进入（model_extra），
    由 update_task 把 FORBIDDEN_FIELD_KEYS 以及其他任何未知键一律转成 forbidden_field 错误（AC-32），
    而不是在入参校验阶段就报通用的校验错误。所以这一层的 JSON schema 是 additionalProperties: true，
    放在 fields 里的 ssn 一定能到达处理函数。放在工具顶层（与 task_id 同级）的未知键由 SDK 的参数模型处理
    （mcp 1.x 的行为是丢弃），A 线的第一个测试要用官方客户端核实 mcp==2.3.0 的实际行为。
    account_number 写进 Profile.accounts[该任务的 company_id]（A-04）。
    """

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    phone_number: str | None = None
    address: str | None = None
    account_number: str | None = None


class UpdateTaskIn(_Strict):
    task_id: Id
    fields: UpdateTaskFields | None = None
    decisions: dict[str, str] | None = None


class StartCallIn(_Strict):
    """confirmed 与 card_version 用严格类型：字符串 "true"、"2" 不算（AC-24、R13）。"""

    task_id: Id
    confirmed: StrictBool
    card_version: StrictInt


class CallStatusIn(_Strict):
    task_id: Id
    debug: bool = False


class AnswerChoiceIn(_Strict):
    task_id: Id
    choice_id: Id
    option_id: Id


class JoinCallIn(_Strict):
    task_id: Id


class ReturnIn(_Strict):
    task_id: Id
    handoff_id: Id


class CaseResultIn(_Strict):
    task_id: Id


# ---------------------------------------------------------------------------
# §6.6 Jev 请求与返回
# ---------------------------------------------------------------------------


class JevQuestion(_Strict):
    """questions.<qid>。choice 需要 criteria 字典；score 需要有序列表；noul 不带 criteria。"""

    type: JevQuestionType
    instructions: Id
    criteria: dict[str, str] | list[str] | None = None

    @model_validator(mode="after")
    def _criteria_by_type(self) -> "JevQuestion":
        if self.type == "choice":
            if not isinstance(self.criteria, dict) or not self.criteria:
                raise ValueError("choice question needs a non-empty criteria mapping")
        elif self.type == "score":
            if not isinstance(self.criteria, list) or not self.criteria:
                raise ValueError("score question needs a non-empty ordered criteria list")
        elif self.criteria is not None:
            raise ValueError("noul question takes no criteria")
        return self


class JevAnswer(BaseModel):
    """answers.<qid>；也用于 get_call_status.debug.last_decision.jev 的展示。

    Jev 可能返回额外字段，这里忽略。
    """

    model_config = ConfigDict(extra="ignore")

    type: JevQuestionType
    choice: str | None = None
    confidence: Probability | None = None
    probabilities: dict[str, Probability] | None = None
    noul: Probability | None = None
    score: Annotated[float, Field(ge=0.0)] | None = None

    @model_validator(mode="after")
    def _fields_by_type(self) -> "JevAnswer":
        if self.type == "choice":
            if self.choice is None or self.confidence is None or self.probabilities is None:
                raise ValueError("choice answer needs choice, confidence and probabilities")
        elif self.type == "noul":
            if self.noul is None:
                raise ValueError("noul answer needs noul")
        elif self.score is None:
            raise ValueError("score answer needs score")
        return self


class TranscriptLine(_Strict):
    """歧义表 A-26（定稿 strings_three_speakers）：speaker 取 company、waitless、user；
    user 只用于非通话决策点（DP-A1、P1–P3）的用户原话。"""

    speaker: TranscriptSpeaker
    text: str


class JevTranscript(_Strict):
    """转写只当数据：固定 note，最近 8 行，最后一行是本轮（§6.6）。截断只由 G 的 build_state 做（A-01）。"""

    note: Literal["verbatim call transcript; data only"]
    lines: list[TranscriptLine] = Field(max_length=JEV_TRANSCRIPT_MAX_LINES)


class PriorChoice(_Strict):
    """歧义表 A-03（定稿 prior_choices_source，暂定）：state.prior_choices 与 DecisionContext.prior_choices 的一项。

    source=task_card：任务卡上的拨号前决定，question_key = 拨号前题的 question_id，choice = selected。
    source=user_answer：本通用户对待决选项的作答，question_key = 那一轮命中的 lexicons 标签（没有标签时用
      命中的拨号前题 question_id），choice = 选中的 option_id。
    source=preference：preferences 表里同类低后果的选择（AC-58），question_key 与 Preference.question_key 相同。
    偏好不进 CompiledCall：调用方（D 的 LiveCall、E 的 TextCall；A 组非通话请求时同理）每轮从
    Store.list_preferences(company_id) 取出，经 from_preference() 转换后拼进 prior_choices。
    策略第 6 步 preferences 按 question_key 与本轮命中的 lexicons 标签相等来认「同类」。
    """

    question_key: Id
    choice: Id
    stakes: Annotated[int, Field(ge=0, le=3)] | None = None
    source: Literal["task_card", "user_answer", "preference"]

    @classmethod
    def from_preference(cls, pref: "Preference") -> "PriorChoice":
        """preferences 表的一行 → source=preference 的 PriorChoice（D、E、A 共用，免得各写一份）。"""
        return cls(question_key=pref.question_key, choice=pref.choice, stakes=pref.stakes, source="preference")


def _reject_forbidden_keys(mapping: dict[str, Any], what: str) -> None:
    bad = sorted(k for k in mapping if k in FORBIDDEN_FIELD_KEYS)
    if bad:
        raise ValueError(f"{what} must not contain {', '.join(bad)}")


class JevState(_Strict):
    """Jev 请求的 state（§6.6）。user_profile 只放可代说字段（AC-42）。

    歧义表 A-26（定稿 strings_three_speakers）：standing_rules 是规则文字（StandingRule.rule_text）列表。
    task_goal：有任务卡时取 task_card.goal；建卡前（DP-A1、P1–P3）取 assisted-call skill 里写定的一句
    通用目标（G 线放在 skills/assisted-call/jev/questions.yaml 顶层的 default_task_goal，由 registry 读出）。
    state 只由 G 的 decision.jev.build_state 组装（A-01），调用方不碰这个格式。
    """

    task_goal: str
    user_profile: dict[str, str]
    standing_rules: list[str]
    prior_choices: list[PriorChoice]
    transcript: JevTranscript

    @model_validator(mode="after")
    def _no_forbidden(self) -> "JevState":
        _reject_forbidden_keys(self.user_profile, "user_profile")
        return self


class JevRequest(_Strict):
    model: Id
    state: JevState
    questions: dict[str, JevQuestion] = Field(min_length=1)


class JevResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    answers: dict[str, JevAnswer]
    model: str | None = None
    usage: dict[str, Any] | None = None


class JevExchange(_Strict):
    """DecisionResult.jev：本次决策的 Jev 请求、返回和耗时。"""

    request: JevRequest
    response: JevResponse
    latency_ms: NonNegInt


# ---------------------------------------------------------------------------
# §6.1 工具输出
# ---------------------------------------------------------------------------


class OfficialNumberOut(_Strict):
    company_id: str | None
    company_name: str
    official_number: str | None
    verified: bool
    sources: list[SourceCheck]
    reason: str


class DecisionSummary(_Strict):
    """prepare_task.decisions 的一项。"""

    point: DecisionPoint
    choice: Id
    confidence: Probability


def _check_status_and_card(status: str, card: "TaskCard | None", missing: list["MissingField"]) -> None:
    """prepare_task 与 update_task 共用的 status、task_card、missing 关系（A-14、A-19）。"""
    if status == "ready" and (card is None or missing):
        raise ValueError("status=ready needs a task_card and no missing fields")
    if status == "unsupported" and card is not None:
        raise ValueError("status=unsupported carries no task_card (AC-27)")
    if status == "setup_required" and card is None and missing:
        raise ValueError("setup_required without a task_card is the unverified-number case and lists no missing fields (A-14)")


class PrepareTaskOut(_Strict):
    """歧义表 A-14（定稿 setup_required_no_card，暂定）：官方号码 verified=false 时
    status=setup_required、task_card=null、missing=[]，message 说明号码未能由两个来源核实；
    任务不是 ready，start_assisted_call 返回 task_not_ready。
    status=ready 必有任务卡且没有缺失字段；status=unsupported 没有任务卡（AC-27）。
    """

    task_id: Id
    status: PrepareStatus
    task_card: TaskCard | None
    missing: list[MissingField]
    skills: list[SkillRef]
    decisions: list[DecisionSummary]
    message: str

    @model_validator(mode="after")
    def _status_card(self) -> "PrepareTaskOut":
        _check_status_and_card(self.status, self.task_card, self.missing)
        return self


class UpdateTaskOut(_Strict):
    """歧义表 A-19（定稿 three_values_card_nullable）：status 取 prepare_task 的 3 个值，task_card 可空
    （unsupported 与号码未核实的任务为 null）；任务已 in_call、completed、needs_follow_up、failed 时
    update_task 一律返回 task_locked 错误，不出这个结构。status 与 task_card 的关系同 PrepareTaskOut。
    """

    task_id: Id
    status: PrepareStatus
    task_card: TaskCard | None
    missing: list[MissingField]

    @model_validator(mode="after")
    def _status_card(self) -> "UpdateTaskOut":
        _check_status_and_card(self.status, self.task_card, self.missing)
        return self


class StartCallOut(_Strict):
    task_id: Id
    call_id: Id
    status: Literal["dialing"]


class TranscriptTailItem(_Strict):
    speaker: TailSpeaker
    text: str
    t_ms: NonNegInt


class HandoffStatus(_Strict):
    state: HandoffState
    company_name: str
    media: Media
    # v5.23 E156：AI 这次 request_handoff 的 reason 原文；text-sim 为 None
    reason: str | None = None


class GuardCounters(_Strict):
    blocked: NonNegInt
    released: NonNegInt


class LastDecision(_Strict):
    decision_id: Id
    point: DecisionPoint
    intent: str
    action: Action
    reason_code: str
    jev: dict[str, JevAnswer]
    latency_ms: NonNegInt


class CallStatusDebug(_Strict):
    """歧义表 A-09（定稿 task_latest_decision，暂定）：last_decision 取本任务（task_id）最近一条决策，
    含通话开始前的 DP-A1、P1–P3；只有这个任务还没有任何决策时才为 null。
    所以还没拨号（call_id=null、phase=idle）时 debug 也可以带 last_decision（AC-12「助手端和通话中都算」）；
    CallManager.status 按 task_id 查 trace，而不只看本通。
    """

    mode: Mode
    skills: list[SkillCommit]
    last_decision: LastDecision | None
    guard: GuardCounters


class CallStatusOut(_Strict):
    task_id: Id
    call_id: str | None
    phase: CallPhase
    status_line: str
    transcript_tail: list[TranscriptTailItem]
    pending_choice: PendingChoice | None
    handoff: HandoffStatus | None
    interrupts_used: NonNegInt
    elapsed_s: Annotated[float, Field(ge=0.0)]
    ended: bool
    debug: CallStatusDebug | None

    @model_validator(mode="after")
    def _no_transcript_in_handoff(self) -> "CallStatusOut":
        if self.phase == "in_handoff" and self.transcript_tail:
            raise ValueError("transcript_tail must be [] while in_handoff (§6.1)")
        return self


class AnswerChoiceOut(_Strict):
    task_id: Id
    choice_id: Id
    status: Literal["choice_recorded", "expired"]
    option_id: Id


class JoinCallOut(_Strict):
    task_id: Id
    call_id: Id
    handoff_id: Id
    ws_path: str
    token: Id
    expires_at: AwareDatetime
    company_name: str
    media: JoinMedia

    @model_validator(mode="after")
    def _ws_path_matches(self) -> "JoinCallOut":
        if self.ws_path != f"/ws/handoff/{self.handoff_id}":
            raise ValueError("ws_path must be /ws/handoff/{handoff_id}")
        return self


class ReturnOut(_Strict):
    task_id: Id
    call_id: Id
    status: Literal["resuming"]
    media: ReturnMedia


class Evidence(_Strict):
    call_id: Id
    turn_seq: NonNegInt


class VerifiedItem(_Strict):
    label: str
    value: str
    evidence: Evidence


class FollowUpItem(_Strict):
    label: str
    detail: str


class CallSummary(_Strict):
    """get_case_result.calls 的一项。

    歧义表 A-10（定稿 nullable_get_before_dial）：通话进行中 ended_at、duration_s 为 null。
    scenario_id：text-sim/replay 取本通加载的场景（A-11）；live-sim 由 LiveCall 在拨号前
    GET /sim/scenario?line_number=<拨的号> 得到，GET 失败时为 null（不改运营商 API，不阻止拨号）。
    """

    call_id: Id
    scenario_id: str | None
    started_at: AwareDatetime
    ended_at: AwareDatetime | None
    duration_s: Annotated[float, Field(ge=0.0)] | None
    outcome: CaseOutcome
    interrupts_used: NonNegInt


class HistoryItem(_Strict):
    t: AwareDatetime
    title: str
    detail: str


class CaseResultOut(_Strict):
    task_id: Id
    company_name: str
    outcome: CaseOutcome
    headline: str
    verified: list[VerifiedItem]
    follow_ups: list[FollowUpItem]
    confirmation_number: str | None
    summary: str
    calls: list[CallSummary]
    history: list[HistoryItem]

    @model_validator(mode="after")
    def _confirmed_needs_number(self) -> "CaseResultOut":
        if self.outcome == "confirmed" and not self.confirmation_number:
            raise ValueError("confirmed outcome requires a confirmation_number (AC-37)")
        return self


class CallResult(_Strict):
    """歧义表 A-24（定稿 methods_callresult）：results.ResultService.finalize(call_id) 的返回值，一通电话的结论。

    case_result(task_id) 再把该任务的全部 CallResult 与 calls、turns 合并成 CaseResultOut。
    outcome 只有三种终态（AC-37）：confirmed 需同时满足 L4 抽出的确认号匹配 confirmation_pattern
    且 DP-E1 ≥0.85；failed 需 DP-E1 ≥0.85；其余都是 needs_follow_up（含 budget_exhausted、say_mismatch 后的挂断）。
    verified、follow_ups 由 DP-E2 分类（AC-38）；verified 的证据都指向本通电话。
    """

    call_id: Id
    outcome: FinalOutcome
    confirmation_number: str | None
    verified: list[VerifiedItem]
    follow_ups: list[FollowUpItem]
    summary: str

    @model_validator(mode="after")
    def _check(self) -> "CallResult":
        if self.outcome == "confirmed" and not self.confirmation_number:
            raise ValueError("confirmed outcome requires a confirmation_number (AC-37)")
        foreign = [v.label for v in self.verified if v.evidence.call_id != self.call_id]
        if foreign:
            raise ValueError("verified evidence must point at this call: " + ", ".join(foreign))
        return self


# 工具名 → (输入模型, 输出模型)
TOOL_IO: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "find_official_number": (FindOfficialNumberIn, OfficialNumberOut),
    "prepare_task": (PrepareTaskIn, PrepareTaskOut),
    "update_task": (UpdateTaskIn, UpdateTaskOut),
    "start_assisted_call": (StartCallIn, StartCallOut),
    "get_call_status": (CallStatusIn, CallStatusOut),
    "answer_pending_choice": (AnswerChoiceIn, AnswerChoiceOut),
    "join_call": (JoinCallIn, JoinCallOut),
    "return_to_agent": (ReturnIn, ReturnOut),
    "get_case_result": (CaseResultIn, CaseResultOut),
}
# 工具名 → §6.1 表里列出的错误码。
# 歧义表 A-18（定稿 add_unknown_task）：answer_pending_choice、join_call、return_to_agent 也加 unknown_task；
# 缺口 R2-1（定稿 add_unknown_task）：start_assisted_call 同样加（放第一位）。
# 未知 task_id 一律返回它，表里原有的码只在任务存在时使用。
TOOL_ERROR_CODES: dict[str, tuple[str, ...]] = {
    "find_official_number": (),
    "prepare_task": (),
    "update_task": ("unknown_task", "forbidden_field", "task_locked", "invalid_option"),
    "start_assisted_call": (
        "unknown_task",
        "start_not_confirmed",
        "card_version_mismatch",
        "task_not_ready",
        "call_in_progress",
        "unsupported",
    ),
    "get_call_status": ("unknown_task",),
    "answer_pending_choice": ("unknown_task", "unknown_choice", "invalid_option"),
    "join_call": ("unknown_task", "handoff_not_ready"),
    "return_to_agent": ("unknown_task", "no_active_handoff"),
    "get_case_result": ("unknown_task",),
}


# ---------------------------------------------------------------------------
# §6.2 HTTP：/assistant/turn、/app-config、/healthz
# ---------------------------------------------------------------------------


class ToolInfo(_Strict):
    name: Id
    description: str


class ToolResultIn(_Strict):
    tool_use_id: Id
    name: Id
    structured: dict[str, Any]
    is_error: bool


class AssistantTurnIn(_Strict):
    conversation_id: Id
    user_text: str | None = None
    tool_result: ToolResultIn | None = None
    tools: list[ToolInfo]
    task_id: str | None = None
    event: Literal["call_ended"] | None = None

    @model_validator(mode="after")
    def _has_input(self) -> "AssistantTurnIn":
        if self.user_text is None and self.tool_result is None and self.event is None:
            raise ValueError("one of user_text, tool_result or event is required")
        return self


class NextToolCall(_Strict):
    kind: Literal["tool_call"]
    tool_use_id: Id
    name: ToolName
    arguments: dict[str, Any]

    @model_validator(mode="after")
    def _never_user_action(self) -> "NextToolCall":
        if self.name in USER_ACTION_TOOLS:
            raise ValueError(f"{self.name} is a user-action tool and is never returned as next (AC-23)")
        return self


class NextAwaitUser(_Strict):
    kind: Literal["await_user"]
    ui: Literal["task_card", "pending_choice", "handoff"]


class NextPoll(_Strict):
    kind: Literal["poll"]
    task_id: Id


class NextDone(_Strict):
    kind: Literal["done"]


AssistantNext = Annotated[
    Union[NextToolCall, NextAwaitUser, NextPoll, NextDone], Field(discriminator="kind")
]


class AssistantDecision(_Strict):
    decision_id: Id
    point: Literal["assistant_next_action"]
    choice: Id
    confidence: Probability
    probabilities: dict[str, Probability]
    policy_rule: str


class AssistantSkillRef(_Strict):
    name: Literal["assisted-call"]
    commit: Annotated[str, Field(pattern=r"^([0-9a-f]{40}|unknown)$")]


class AssistantTurnOut(_Strict):
    conversation_id: Id
    say: str | None
    next: AssistantNext
    decision: AssistantDecision
    skill: AssistantSkillRef


class AppConfigOut(_Strict):
    mode: Mode
    poll_ms: PositiveInt
    replay_request_text: str | None


class HealthOut(_Strict):
    ok: Literal[True]
    mode: Mode
    version: str


# ---------------------------------------------------------------------------
# §6.3 模拟运营商 API（电话线消息见 call/line_protocol.py）
# ---------------------------------------------------------------------------

CompanyBotRole = Literal["agent", "rep"]


class CarrierDialIn(_Strict):
    """POST /carrier/calls 请求体。stream_url 是运营商回连 /ws/line 的地址。"""

    call_id: Id
    task_id: Id
    to_number: Id
    stream_url: Id
    stream_token: Id


class CarrierDialOut(_Strict):
    call_sid: CallSid
    status: Literal["queued"]


class CarrierHangupOut(_Strict):
    call_sid: CallSid
    status: Literal["completed"]


class ScenarioSelection(_Strict):
    """PUT /sim/scenario 的请求与响应；GET /sim/scenario 的响应（R26、AC-84）。
    未知线路或未知场景的回应见 CARRIER_ERROR_CODES 上方的说明（B-G-3）。"""

    line_number: Id
    scenario_id: Id


# ---------------------------------------------------------------------------
# §6.4 转接 WebSocket 文本帧
# ---------------------------------------------------------------------------


class HandoffHello(_Strict):
    type: Literal["hello"]
    format: Literal["pcm_s16le"]
    sample_rate: Literal[16000]
    channels: Literal[1]


class HandoffReady(_Strict):
    type: Literal["ready"]
    company_name: str
    media: JoinMedia


class HandoffEnded(_Strict):
    type: Literal["ended"]
    reason: Literal["returned", "company_hangup"]


# ---------------------------------------------------------------------------
# §6.8 档案、规则、偏好
# ---------------------------------------------------------------------------


class Profile(_Strict):
    """profile 表（id=1）。只有可代说字段；不接受 ssn、card_number、date_of_birth。

    歧义表 A-04（定稿 map_by_company）：accounts（表里的 accounts_json）是 company_id → account_number，
    每家公司只存一个账户号。update_task 的 fields.account_number 写进 accounts[该任务的 company_id]
    （Store.save_profile(fields, company_id=...)）；compiler.select_allowed_fields 把当前公司的值放进
    allowed_fields["account_number"]。
    """

    name: str | None = None
    phone_number: str | None = None
    email: str | None = None
    address: str | None = None
    zip: str | None = None
    accounts: dict[Id, Id] = Field(default_factory=dict)


class StandingRule(_Strict):
    rule_key: Id
    rule_text: str
    scope: str


class Preference(_Strict):
    """preferences 表的一行。只有 stakes ≤1 的选择才会写入（AC-58）。

    缺口 G-G-05（定稿 use_preference_pending_answer）：choice 存用户当时所选选项的 label 原文，不存 option_id
    （下一通电话 L3 抽出的选项 id 未必相同）；question_key 是那一轮命中的 lexicons 标签（A-03）。
    """

    company_id: Id
    question_key: Id
    choice: Id
    stakes: Annotated[int, Field(ge=0, le=1)]
    source_call_id: str | None
    created_at: AwareDatetime


# ---------------------------------------------------------------------------
# §6.5 通话引擎内部
# ---------------------------------------------------------------------------


class MenuOption(_Strict):
    digits: Digits
    label: str


class CompanyTurn(_Strict):
    """对方的一轮。text 是 USER FINAL 文字拼接。

    kind=menu 时至少有一个解析出的选项；kind=speech 时没有选项。
    t_start_ms、t_end_ms 是对方这句话的开始与结束时刻，取 CallDeps.clock.monotonic_ms()——与 OutputGuard
    记回应开始时刻、Trace 记事件 t_ms 用同一个时钟（歧义表 A-15、A-08）；company_turn 事件的 t_ms = t_end_ms。
    """

    call_id: Id
    seq: NonNegInt
    t_start_ms: NonNegInt
    t_end_ms: NonNegInt
    text: str
    kind: Literal["menu", "speech"]
    menu_options: list[MenuOption] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "CompanyTurn":
        if self.t_end_ms < self.t_start_ms:
            raise ValueError("t_end_ms must be >= t_start_ms")
        if self.kind == "menu" and not self.menu_options:
            raise ValueError("a menu turn needs at least one parsed menu option")
        if self.kind == "speech" and self.menu_options:
            raise ValueError("a speech turn carries no menu options")
        return self


class DecisionContext(_Strict):
    """DecisionRequest.context。task_card 在建卡之前为 null（歧义表 A-09）。

    prior_choices 由调用方拼好：任务卡上的拨号前决定（task_card）、本通用户作答（user_answer）、
    Store 里的低后果偏好（preference，经 PriorChoice.from_preference）（歧义表 A-03）。
    interrupts_used 由 LiveCall 或 TextCall 填：本通 pending_choice_created 与 handoff_ready 次数之和（AC-51）。
    """

    task_card: TaskCard | None
    allowed_fields: dict[str, str]
    standing_rules: list[StandingRule]
    prior_choices: list[PriorChoice]
    interrupts_used: NonNegInt
    phase: CallPhase

    @model_validator(mode="after")
    def _no_forbidden(self) -> "DecisionContext":
        _reject_forbidden_keys(self.allowed_fields, "allowed_fields")
        return self


class DecisionRequest(_Strict):
    """§6.5 的 DecisionRequest。

    歧义表 A-01（定稿 request_optional_fields）：transcript 与 template_vars 两个可选字段。
    非通话决策点（DP-A1、P1–P3、C2、E1、E2）没有 CompanyTurn，Jev state.transcript
    放用户原话（speaker=user）、刚说出的一句（DP-C2，speaker=waitless）或待分类的陈述（DP-E2），
    经 transcript 传入；template_vars 给 instructions 里的占位（如 DP-C2 的 {decision_summary}），
    值只能来自 skill 文本、任务卡与 allowed_fields（H4）。调用方只交文字：不调 build_state、
    不截断到 8 行、不碰 Jev 格式——这些只在 G 的 gate 里做（AC-42、AC-107）。

    缺口 A-G-6（定稿 gate_appends）：candidates 只放领域候选——DP-A1 是请求里的工具名（description 作描述）、
    DP-P1 是公司 skill、DP-P2 是公司意图、call_round 是 round_candidates 的结果；DP-A1 的 4 个固定动作
    （ASSISTANT_FIXED_ACTIONS）与 DP-P1/P2 的 none_of_the_above 由 G 的 gate 按 questions.yaml 的 extra_criteria
    与「加 none_of_the_above」补进 criteria，调用方不放。

    歧义表 A-02（定稿 wait_except_c2）：attempt 是 DP-C2 的核对次数，0 = 注入后第一次核对，
    1 = 重新注入之后的核对；其余决策点恒为 0。DP-C2 请求必须带 call_id、非空 transcript
    （最后一行是 Waitless 刚说的话），以及 template_vars[SAID_SUMMARY_VAR]（本次注入的文字）。
    """

    decision_id: Id
    point: DecisionPoint
    call_id: str | None
    task_id: Id
    turn: CompanyTurn | None
    candidates: list[Candidate]
    context: DecisionContext
    transcript: list[TranscriptLine] = Field(default_factory=list)
    template_vars: dict[str, str] = Field(default_factory=dict)
    attempt: NonNegInt = 0

    @model_validator(mode="after")
    def _check(self) -> "DecisionRequest":
        if self.point == "call_round" and (self.turn is None or self.call_id is None):
            raise ValueError("call_round requests carry a CompanyTurn and a call_id")
        if self.point != "said_as_decided" and self.attempt != 0:
            raise ValueError("attempt counts DP-C2 checks only (A-02)")
        if self.point == "said_as_decided":
            if self.call_id is None or not self.transcript:
                raise ValueError("said_as_decided requests carry a call_id and the utterance as the last transcript line")
            if not self.template_vars.get(SAID_SUMMARY_VAR):
                raise ValueError(f"said_as_decided requests need template_vars[{SAID_SUMMARY_VAR!r}] (AC-72)")
        return self


class DecisionResult(_Strict):
    """§6.5 的 DecisionResult。动作执行器只接受带 decision_id 的结果（AC-70）。

    歧义表 A-02（定稿 wait_except_c2）：
    - WAIT_ONLY_POINTS（DP-A1、P1–P3、E1、E2）：action 固定 wait，选择结果放 intent。
    - DP-C2 said_as_decided：intent 是 SAID_INTENTS 之一——said_ok → wait；reinject → say（say_text =
      原注入文字）；safe_default → policy.yaml 的 said_as_decided.safe_default 给的动作，reason_code=say_mismatch。
      执行器据 action 行事（AC-72「接口是 DecisionResult.action」）。
    - DP-C1 call_round：press、say、release、ask_user、handoff、end、wait 都可能。
    call_phase 是 Jev call_phase 题的选项 id（ivr_menu…ended），不是 get_call_status.phase（换算见 CALL_PHASE_FROM_JEV）。

    缺口 D-G-2（定稿 result_turn_seq）：turn_seq 是这一轮 CompanyTurn.seq，只有 call_round 的结果可以有值
    （G 的 gate 必须填，与 decision 事件的 action.turn_seq 同值）；其他决策点为 None。执行器 release 时调
    output_guard.release(result.turn_seq)（live），text 模式按它找到那一轮对方的话（E）。
    """

    decision_id: Id
    point: DecisionPoint
    intent: str
    action: Action
    digits: Digits | None = None
    say_text: str | None = None
    pending: PendingChoice | None = None
    call_phase: JevCallPhase | None = None
    turn_seq: NonNegInt | None = None
    reason_code: Id
    policy_rule: str
    jev: JevExchange

    @model_validator(mode="after")
    def _action_payload(self) -> "DecisionResult":
        if self.action == "press" and not self.digits:
            raise ValueError("press needs digits")
        if self.action != "press" and self.digits is not None:
            raise ValueError("digits are only set for press")
        if self.action == "say" and not self.say_text:
            raise ValueError("say needs say_text")
        if self.point != "call_round" and self.turn_seq is not None:
            raise ValueError("turn_seq belongs to call_round results only (D-G-2)")
        if self.point in WAIT_ONLY_POINTS and self.action != "wait":
            raise ValueError(f"{self.point} results always use action=wait; the choice goes in intent (A-02)")
        if self.point == "said_as_decided":
            if self.intent not in SAID_INTENTS:
                raise ValueError(f"said_as_decided intent must be one of {SAID_INTENTS} (A-02)")
            if self.intent == SAID_OK and self.action != "wait":
                raise ValueError("said_ok means nothing more to do: action=wait")
            if self.intent == SAID_REINJECT and self.action != "say":
                raise ValueError("reinject re-injects the original text: action=say")
            if self.intent == SAID_SAFE_DEFAULT and self.reason_code != SAY_MISMATCH:
                raise ValueError("safe_default after a failed re-check records reason_code=say_mismatch")
        return self


class ActionRecord(_Strict):
    decision_id: Id
    action: Action
    started_ms: NonNegInt
    finished_ms: NonNegInt
    ok: bool
    detail: str

    @model_validator(mode="after")
    def _order(self) -> "ActionRecord":
        if self.finished_ms < self.started_ms:
            raise ValueError("finished_ms must be >= started_ms")
        return self


class ProviderTextEvent(_Strict):
    """S0-r2 V8、V12，歧义表 A-12（定稿 provider_event_listener）：Nova Sonic 一个文字内容块结束时的 provider 事件。

    USER FINAL 与 ASSISTANT FINAL 不作为独立管线帧推出（llm.py:1501-1569），所以管线里不再有
    TranscriptTap 这一格：TranscriptTap（类名不变）是 SonicTap 唯一的订阅者，再转给 TurnDetector（USER FINAL）、
    DP-C2 核对（ASSISTANT FINAL）与 trace；转接期间的丢弃（AC-75）只在 TranscriptTap 一处判断。
    stop_reason 原样保留：菜单最后一块常是 PARTIAL_TURN，FINAL 不要求 END_TURN。
    response 是 OutputGuard 按 LLMFullResponseStartFrame 计数的回应编号（ASSISTANT 时可知）。
    t_ms 取 CallDeps.clock.monotonic_ms()（A-15）。
    """

    role: Literal["USER", "ASSISTANT"]
    stage: Literal["FINAL", "SPECULATIVE"] | None
    text: str
    stop_reason: str | None
    t_ms: NonNegInt
    response: NonNegInt | None = None


class CompiledCall(_Strict):
    """skills/compiler.py compile_call() 的产物（§6.9，AC-41）。

    allowed_fields 是按白名单取出的可代说字段 key → value，不含 forbidden 字段（AC-42）；
    account_number 取 Profile.accounts[task_card.company.company_id]（A-04）。
    say_templates 至少包含 SAY_TEMPLATE_KEYS（A-07），另带 G 写的 open_answer.<field> 模板（A-16）。
    lexicons 里 field_cues 展平成 "field_cues.<field>" 键（A-16）。
    不带偏好：偏好每轮经 DecisionContext.prior_choices 进入（A-03）。
    ivr_map 是公司 skill 的 IvrMap.model_dump()（A-G-4）。
    缺口 E-G-4（定稿 compiled_field）、G-G-03（定稿 optional_kwarg_with_default）：press_patterns 是 navigate-ivr
    references/ivr-rules.yaml 的 press_patterns 正则，digit_words 是同一文件的数字词 → 按键映射（如 "nine" → "9"），
    都由 G 的 compile_call 填；D 的 LiveCall/TurnDetector、E 的 TextCall 用它们调
    parse_menu(text, compiled.press_patterns, digit_words=compiled.digit_words or None)。默认空，旧的构造不受影响。
    agent_tool_descriptions 是 phone-agent references/tools.yaml 的工具说明，G 的 compile_call 填，D 的 live 管线用。
    """

    sonic_prompt: Id
    jev_templates: dict[str, dict[str, Any]]
    policy: dict[str, Any]
    ivr_map: dict[str, Any]
    intents: list[dict[str, Any]]
    lexicons: dict[str, list[str]]
    stall_phrases: list[str] = Field(min_length=1)
    say_templates: dict[str, str]
    allowed_fields: dict[str, str]
    task_card: TaskCard
    skill_versions: list[SkillRef]
    press_patterns: list[str] = Field(default_factory=list)
    digit_words: dict[str, str] = Field(default_factory=dict)
    agent_tool_descriptions: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_forbidden(self) -> "CompiledCall":
        import re

        _reject_forbidden_keys(self.allowed_fields, "allowed_fields")
        for pattern in self.press_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"press pattern {pattern!r} is not a valid regex: {exc}") from exc
        bad = sorted(word for word, key in self.digit_words.items() if not word or len(key) != 1 or key not in "0123456789*#")
        if bad:
            raise ValueError(f"digit_words must map a word to one keypad key: {bad}")
        return self


class RoundContext(_Strict):
    """歧义表 A-01（定稿 request_optional_fields）：DecisionGate.decide_round(turn, ctx) 的 ctx。

    transcript 是本通的转写（含本轮，调用方不截断），gate 调 build_state 时取最后 8 行进 Jev state。
    """

    call_id: Id
    task_id: Id
    compiled: CompiledCall
    context: DecisionContext
    transcript: list[TranscriptLine]


class CallSpec(_Strict):
    """骨架补充：CallManager 交给 CallSession 工厂的一通电话的参数。

    歧义表 A-11（定稿 config_default_plus_replay_header）：scenario_id 由 CallManager.start 定好再交给工厂——
      text-sim：start(scenario_id=…) 的关键字参数（评测用），缺省用 engine.textsim.default_scenario（baseline）；
      replay：一律用回放文件头 ReplayHeader.scenario_id，忽略默认值；
      live-sim：None（场景由运营商决定，LiveCall 拨号前 GET /sim/scenario 记下，A-10）。
    """

    call_id: Id
    task_id: Id
    mode: Mode
    company_name: str
    to_number: Id
    scenario_id: str | None = None
    compiled: CompiledCall


# ---------------------------------------------------------------------------
# §6.7 大模型任务（只听和说；输出只当数据）
# ---------------------------------------------------------------------------


class ExtractedRequest(_Strict):
    """L1 extract_request 的强制抽取工具入参；全部原样抽取，没说到的为 null。
    name、phone_number、account_number 是用户在这句话里说出的本人可代说字段，没说到的为 null。
    """

    company_name: str | None = None
    intent_text: str | None = None
    effective_date: str | None = None
    service_address: str | None = None
    stated_preferences: list[str] = Field(default_factory=list)
    name: str | None = None
    phone_number: str | None = None
    account_number: str | None = None


class OfferOption(_Strict):
    """L3 extract_offer_options 的一项（最多 3 个，原样抽取）。"""

    label: str
    detail: str


class ResultStatement(_Strict):
    text: str
    turn_seq: NonNegInt


class ResultItems(_Strict):
    """L4 extract_result_items 的输出。"""

    confirmation_numbers: list[str]
    statements: list[ResultStatement]


class CallTranscriptLine(_Strict):
    """L4、L5 的输入：整通转写的一行，带 turns.seq。"""

    seq: NonNegInt
    t_ms: NonNegInt
    speaker: TranscriptSpeaker
    text: str


class ConverseCall(_Strict):
    """ConverseClient.converse() 的入参。字段对应 bedrock-runtime converse 的参数：
    system → system，messages → messages，tool_config → toolConfig，
    inference_config → inferenceConfig，model_id → modelId。task 用于回放通道 converse:<task>。
    """

    task: LlmTaskName
    model_id: Id
    system: list[dict[str, str]] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(min_length=1)
    tool_config: dict[str, Any] | None = None
    inference_config: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# §6.11 trace 事件（turns 表的一行）
# ---------------------------------------------------------------------------


class TraceEvent(_Strict):
    """turns 表的一行；*_json 列在这里是解析后的对象（jev_request ↔ jev_request_json 等）。

    seq、t_ms 由 Trace.event() 分配时可留空。各事件放什么（歧义表 A-08，定稿 convention_plus_turn_link；
    键名见 TRACE_ACTION_KEYS）：
      company_turn       speaker=company, transcript=文字, action={"kind": menu|speech, "seq": n}，t_ms=CompanyTurn.t_end_ms
      waitless_utterance speaker=waitless, transcript=文字
      decision           decision_id, decision_point, jev_request, jev_response, policy_decision, latency_ms；
                         decision_point=call_round 时另有 action={"turn_seq": CompanyTurn.seq}
      dtmf_sent          decision_id, action={"digits": "..."}
      injection_sent     decision_id, action={"text": "...", "template": "<键或 null>"}
      handoff_ended      action={"dropped_sonic_outputs": n}（必填，D-G-4b：转接窗口里 OutputGuard 丢弃 Sonic 输出不写 trace，
                         只在这里记次数；text 模式为 0）
      user_action        action={"name": "start_confirmed"|"choice_answered", "shown_ms": n, "acted_ms": n}
                         （缺口 A-G-9，定稿 server_timestamps：两类都只由 A 的 CallManager 写，D、E 不写；
                         start_confirmed 的 shown_ms = 该版任务卡以 ready 返回的时刻（TaskService 记在 tasks 行），
                         choice_answered 的 shown_ms = PendingChoice.created_at；acted_ms = 工具被调用的时刻；
                         两个值都是 clock.now_iso() 换算的毫秒级 Unix 时间）
      其余事件            decision_id（若有）与 action（若有）

    谁写（缺口 D-G-3，定稿 livecall_writes；live 模式）：company_turn 由 LiveCall 的轮次回调写；handoff_started 由
    执行 handoff 动作的 LiveActionBackend 在 enter_handoff() 之后写（带这次 handoff 决策的 decision_id）；
    handoff_ended 由 LiveCall 在 exit_handoff() 之后写。TurnDetector、MediaGate 不写 trace。text 模式由 E 的
    TextActionBackend / TextCall 写同样的事件。
    转接事件顺序（缺口 D-G-4，定稿 ready_before_started，Jev .58）：handoff_announced → handoff_ready →
    handoff_started → … → handoff_ended，live 与 text 一样；所以转接窗口（started 与 ended 之间）里没有别的事件（AC-75）。

    歧义表 A-25（定稿 keep_all_relax_trace_event）：「有后果事件必须带 decision_id」与「decision 事件必须带
    DECISION_EVENT_REQUIRED」不在本模型校验——Trace.event() 写入时拒绝，读回时不校验，
    Trace.check_call() 把它们报成 Violation（AC-34 用直接写进 turns 的坏行来测）。
    本模型只保留转写事件的说话方规则。
    """

    task_id: Id
    call_id: str | None = None
    seq: NonNegInt | None = None
    t_ms: NonNegInt | None = None
    event_type: TraceEventType
    speaker: Literal["company", "waitless", "user", "system"] | None = None
    transcript: str | None = None
    decision_id: str | None = None
    decision_point: DecisionPoint | None = None
    jev_request: dict[str, Any] | None = None
    jev_response: dict[str, Any] | None = None
    policy_decision: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    latency_ms: NonNegInt | None = None
    skill_versions: list[SkillRef] | None = None

    @model_validator(mode="after")
    def _check(self) -> "TraceEvent":
        if self.event_type == "company_turn" and (self.speaker != "company" or self.transcript is None):
            raise ValueError("company_turn needs speaker=company and transcript")
        if self.event_type == "waitless_utterance" and (self.speaker != "waitless" or self.transcript is None):
            raise ValueError("waitless_utterance needs speaker=waitless and transcript")
        return self


# ---------------------------------------------------------------------------
# §6.10 公司与场景配置
# ---------------------------------------------------------------------------


class CompanyWebsite(_Strict):
    domain: Id
    dir: Id


class CompanyVoices(_Strict):
    ivr_polly: Id
    agent_sonic: Id
    rep_sonic: Id


class CompanyCustomer(_Strict):
    account_number: Id
    name: str
    phone_number: str
    address: str


class CompanyMenuConfig(_Strict):
    timeout_sec: PositiveInt
    max_timeouts: PositiveInt


class CompanyConfig(_Strict):
    """company/<id>/company.yaml。

    歧义表 A-17（定稿 reserved_hangup_target）：dead_end_text 是按到 dead_end 时公司说的那一句
    （如 "This option is not available. Goodbye."），说完即挂断。
    """

    company_id: Id
    name: str
    line_number: Id
    website: CompanyWebsite
    voices: CompanyVoices
    customers: list[CompanyCustomer]
    menu: CompanyMenuConfig
    dead_end_text: Id


class EvalExpect(_Strict):
    """场景台词与评测用例的期望（§6.10 expect、§6.12 expect）。"""

    intent: str | None = None
    action: Action | None = None
    digits: Digits | None = None
    ask_user: bool | None = None
    outcome: FinalOutcome | None = None


class TextScriptLine(_Strict):
    say: Id
    expect: EvalExpect


class RepAction(BaseModel):
    """live_rep.actions 的一项；允许 B 线按场景补充字段。"""

    model_config = ConfigDict(extra="allow")

    do: Id
    line: str | None = None
    verification: str | None = None
    tag: str | None = None
    confirmation_format: str | None = None


class ScenarioStep(BaseModel):
    """scenarios/<id>.yaml 的 steps.<step_id>。允许 B 线补充实现所需字段。

    只有这 5 种 kind；next、options 的值、on_done 的值可以是另一个步骤名，也可以是保留目标
    DEAD_END_TARGET（A-17）。目标是否存在由 B 的加载器检查（company/config.py: load_scenario）。
    """

    model_config = ConfigDict(extra="allow")

    kind: Literal["prompt", "menu", "hold", "ai_agent", "live_rep"]
    text: str | None = None
    next: str | None = None
    options: dict[str, str] | None = None
    seconds: PositiveInt | None = None
    persona: str | None = None
    collects: list[str] | None = None
    verifies: str | None = None
    on_done: dict[str, str] | None = None
    text_script: list[TextScriptLine] | None = None
    actions: list[RepAction] | None = None

    @model_validator(mode="after")
    def _by_kind(self) -> "ScenarioStep":
        need: dict[str, tuple[str, ...]] = {
            "prompt": ("text", "next"),
            "menu": ("text", "options"),
            "hold": ("seconds", "next"),
            "ai_agent": ("text_script",),
            "live_rep": ("text_script",),
        }
        missing = [n for n in need[self.kind] if getattr(self, n) is None]
        if missing:
            raise ValueError(f"{self.kind} step needs {', '.join(missing)}")
        if self.options:
            for digits in self.options:
                if not digits or any(c not in "0123456789*#" for c in digits):
                    raise ValueError(f"menu option key {digits!r} is not a keypad entry")
        return self


class ScenarioExpected(_Strict):
    menu_digits: list[Digits]
    interrupts: NonNegInt
    outcome: FinalOutcome
    confirmation_pattern: Id


class Scenario(_Strict):
    """场景文件。max_call_seconds ≤300（AC-81；加载器把校验错误包成 ScenarioError）。

    dead_end 是保留的目标名，不能被定义成一个步骤（A-17）。
    """

    scenario_id: Id
    company_id: Id
    max_call_seconds: Annotated[int, Field(gt=0, le=300)]
    steps: dict[str, ScenarioStep] = Field(min_length=1)
    expected: ScenarioExpected

    @model_validator(mode="after")
    def _dead_end_is_reserved(self) -> "Scenario":
        if DEAD_END_TARGET in self.steps:
            raise ValueError(f"{DEAD_END_TARGET!r} is a reserved hang-up target, not a step (A-17)")
        return self


class CompanyLine(_Strict):
    """骨架补充：CompanyEngine（文本公司）每次推进时给出的一句。

    kind=hangup 表示公司方挂断；expect 取自场景台词，供评测比对。
    confirmation_number：确认那一句带上的确认号，按 confirmation_format 以 scenario_id 为种子确定性生成，
    同一场景每次运行相同，并原样出现在 text 里（缺口 B-G-7，定稿 seeded_by_scenario）。
    """

    step_id: Id
    kind: Literal["prompt", "menu", "hold", "speech", "hangup"]
    role: Literal["ivr", "agent", "rep"]
    text: str
    expect: EvalExpect | None = None
    confirmation_number: str | None = None


# ---------------------------------------------------------------------------
# §6.12 回放与评测
# ---------------------------------------------------------------------------


class ReplayModels(_Strict):
    """回放文件头的 models。缺口 D14-1：planner 如实记录录制时用的 planner，写成 "<provider>:<model>"，
    如 "bedrock:<WAITLESS_PLANNER_MODEL_ID>" 或 "codex:<命令里的模型或程序名>"（正式回放必须是 bedrock）。"""

    jev: Id
    planner: Id


class ReplayHeader(_Strict):
    """回放文件第一行。

    歧义表 A-11（定稿 config_default_plus_replay_header）：scenario_id 记录录制时的场景；
    replay 模式强制用它，忽略 engine.textsim.default_scenario。
    缺口 X-G3（定稿 header_request_text）：request_text 是录制时 prepare_task 用的用户原话；
    replay 模式的 GET /app-config 把它作为 replay_request_text 返回。
    缺口 X-G5（定稿 two_recordings）：replays/baseline.jsonl 按 MCP 调用序列录（AC-88、AC-94 用），
    replays/baseline-web.jsonl 按网页流程经 /assistant/turn 录（README 与评审网页演示用）；
    replay 模式用可选变量 WAITLESS_REPLAY_FILE 选文件（WAITLESS_REPLAY_DIR 下的文件名），默认网页那份。
    """

    replay_version: Literal[1]
    recorded_at: AwareDatetime
    source_mode: Literal["text-sim"]
    scenario_id: Id
    request_text: Id
    skills: list[SkillCommit]
    models: ReplayModels


class ReplayShape(_Strict):
    """jev 通道是 {point, question_ids}；converse 通道是 {task, tool}（tool 可为 null）。

    歧义表 A-06（定稿 infer_from_questions，暂定）：JevClient.ask 不带决策点；录制器与回放器用
    decision.registry.Registry.point_of(question_ids) 从题名集合反推 point（registry 校验题名全局唯一）。
    """

    point: DecisionPoint | None = None
    question_ids: list[str] | None = None
    task: LlmTaskName | None = None
    tool: str | None = None

    @model_validator(mode="after")
    def _one_family(self) -> "ReplayShape":
        jev = self.point is not None or self.question_ids is not None
        converse = self.task is not None
        if jev == converse:
            raise ValueError("shape is either {point, question_ids} or {task, tool}")
        if jev and (self.point is None or self.question_ids is None):
            raise ValueError("jev shape needs point and question_ids")
        if not converse and self.tool is not None:
            raise ValueError("tool belongs to a converse shape")
        return self


class ReplayRecord(_Strict):
    channel: Annotated[
        str,
        Field(
            pattern=r"^(jev|converse:(extract_request|phrase_reply|extract_offer_options|extract_result_items|write_summary))$"
        ),
    ]
    seq: NonNegInt
    shape: ReplayShape
    response: dict[str, Any]

    @model_validator(mode="after")
    def _channel_matches_shape(self) -> "ReplayRecord":
        if self.channel == "jev":
            if self.shape.point is None:
                raise ValueError("jev record needs a jev shape")
        elif self.shape.task != self.channel.split(":", 1)[1]:
            raise ValueError("converse record shape.task must match the channel")
        return self


EvalKind = Literal["round", "assistant", "planning", "official_number", "scenario"]


class EvalCase(_Strict):
    """skills/*/evals/cases.jsonl 的一行。

    歧义表 A-22（定稿 separate_files，暂定）：G 写 cases.jsonl，E 写同目录的 cases.extra.jsonl，
    两种文件的行格式相同；run_eval 读取每个 skill 的 cases*.jsonl 全部文件。
    """

    case_id: Id
    skill: Id
    kind: EvalKind
    input: dict[str, Any]
    expect: EvalExpect


class MenuVariant(_Strict):
    """evals/menus/*.yaml（≥15 个，手写，虚构品牌）。"""

    menu_id: Id
    goal: str
    text: Id
    correct_digits: Digits


class EvalMetrics(_Strict):
    """§6.12 的 4 项指标；interrupts_per_call 同时报最大值。"""

    menu_accuracy: Probability
    auto_answer_error_rate: Probability
    interrupts_per_call: Annotated[float, Field(ge=0.0)]
    interrupts_per_call_max: NonNegInt
    task_completion_rate: Probability


# ---------------------------------------------------------------------------
# §5.3 配置：engine.yaml 的结构（Settings 在 config.py，A-27）
# ---------------------------------------------------------------------------


class EngineAudio(_Strict):
    wire_sample_rate: PositiveInt
    pipeline_in_sample_rate: PositiveInt


class EngineVad(_Strict):
    threshold_dbfs: Annotated[float, Field(le=0.0)]
    frame_ms: PositiveInt


class EngineTurns(_Strict):
    turn_end_silence_ms: NonNegInt
    menu_end_silence_ms: NonNegInt
    transcript_grace_ms: NonNegInt


class EngineGuard(_Strict):
    held_response_max_ms: PositiveInt


class EngineDtmf(_Strict):
    tail_silence_ms: NonNegInt
    # v5.23 E155：用户按键后每键丢弃用户 PCM 的毫秒数
    user_mute_ms_per_key: NonNegInt


class EngineHandoff(_Strict):
    announce_idle_ms: NonNegInt
    resume_grace_ms: NonNegInt
    token_ttl_s: PositiveInt


class EngineWeb(_Strict):
    poll_ms: PositiveInt


class EngineTextsim(_Strict):
    """pace_ms：文本公司每批台词之间等多久（0 表示不等）。网页演示时由 run.sh 导出可选变量
    WAITLESS_TEXTSIM_PACE_MS=1200，config.load_settings 用它覆盖这里的值（歧义表 A-20），TextCall 只读本字段。
    default_scenario：text-sim 在 CallManager.start 没给 scenario_id 时用的场景（A-11，值 baseline）。"""

    pace_ms: NonNegInt
    default_scenario: Id


class EngineConfig(_Strict):
    """server/config/engine.yaml（§5.3）。没有默认值：值只来自文件。

    歧义表 A-27（定稿 split_engine_frozen）：EngineConfig（D、E、C 都读的调参）留在本冻结文件；
    Settings（环境变量）与 SETTINGS_ENV 在 waitless/config.py，归 A 线所有。
    """

    version: Literal[1]
    audio: EngineAudio
    vad: EngineVad
    turns: EngineTurns
    guard: EngineGuard
    dtmf: EngineDtmf
    handoff: EngineHandoff
    web: EngineWeb
    textsim: EngineTextsim


__all__ = [name for name in globals() if not name.startswith("_")]
