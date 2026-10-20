"""跨施工线接口（第 3 步骨架写，冻结：各施工线不许改）。

每个 Protocol 写明：谁实现、谁调用、对应 tech.v4.md 的章节与 AC（§6、§7 与 v2 相同）。
写法按 RUN/step3/ambiguity-decisions.md 的定稿（A-xx）。方法体一律
`raise NotImplementedError`；实现类在各自模块里按结构实现（可以显式继承这些 Protocol）。

  Clock               时钟（A 的 SystemClock；测试用 FakeClock）
  Store               SQLite 存储里跨线用到的部分（A 实现；G 读写偏好）           §6.8
  Trace               trace 事件写入与查询（A 实现；D、E、G 写）                  §6.11
  JevClient           Jev 客户端（G 的 HttpJevClient；E 的回放；FakeJev）          §6.6
  ConverseClient      Bedrock Converse（A 的 BotoConverseClient；E 的回放）        §6.7
  LlmTasks            L1–L5（A 实现；D、E 的 ask_user 用 L3）                    §6.7
  DecisionGate        决策门（G 实现；A、D、E 调用）                              §6.5
  PendingRegistry     待决选项（G 实现；D、E 创建与作答，A 经 CallSession 查询）     §6.5
  ActionBackend       动作执行（D 的 LiveActionBackend；E 的 TextActionBackend）   §6.5
  CallSession         一通电话（D 的 LiveCall；E 的 TextCall；A 的 CallManager 调用） §7
  LiveSession         LiveCall 额外的线路接入（D）                               §6.3
  CallRegistry        按 call_id 找通话（A 的 CallManager；D 的端点调用）
  CallSessionFactory  按模式建 CallSession（D、E 提供；A 选用）
  LineTokenIssuer     每通线路令牌（D 的 LineTokens）                            AC-62
  HandoffTokenIssuer  转接一次性令牌（D 的 HandoffTokens；E 的 TextCall 也用）     AC-63
  CompanyEngine       文本级公司（B 的 TextCompany；E 的 TextCall 调用）          §6.10
  SonicTap            Sonic 的 provider 文字事件（D 的 pipeline 包装；FakeSonicService）S0-r2
  CallDeps            建 CallSession 时注入的服务
  AppServices         挂在 FastAPI app.state.services 上的跨线服务（A 组装；D 的端点读取）

共享异常也在这里定义，模块按 §7 的名字再导出（例如 call/actions.py 导出 UnknownDecision）。
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from waitless.models import (
    ActionRecord,
    AnswerChoiceOut,
    AssistantDecision,
    CallSpec,
    CallStatusOut,
    CallTranscriptLine,
    CaseResultOut,
    CompanyLine,
    CompanyTurn,
    ConverseCall,
    DecisionPoint,
    DecisionRequest,
    DecisionResult,
    ExtractedRequest,
    JevQuestion,
    JevResponse,
    JevState,
    JoinCallOut,
    OfferOption,
    Option,
    PendingChoice,
    Preference,
    Profile,
    ProviderTextEvent,
    ResultItems,
    ReturnOut,
    RoundContext,
    StandingRule,
    ToolError,
    ToolResultIn,
    TraceEvent,
)
from waitless.call.line_protocol import StartMsg
from waitless.config import Settings  # A-27：Settings 归 A 线的 config.py

# ---------------------------------------------------------------------------
# 共享异常
# ---------------------------------------------------------------------------


class ToolFailure(Exception):
    """业务错误：mcp_server 把它转成 isError 结果，content[0].text = {"code","message"}（§6.1）。

    CallSession、PendingRegistry、TaskService 等抛出它，code 取 models.ErrorCode。
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message

    def to_error(self) -> ToolError:
        return ToolError(code=self.code, message=self.message)  # type: ignore[arg-type]


class UnknownDecision(Exception):
    """动作执行器收到的 decision_id 不在 trace 里（AC-70）。call/actions.py 再导出。"""

    def __init__(self, decision_id: str) -> None:
        super().__init__(f"unknown decision_id: {decision_id}")
        self.decision_id = decision_id


class ReplayMismatch(Exception):
    """回放缺行或 shape 对不上（AC-89）。replay/player.py 再导出。"""

    def __init__(self, channel: str, seq: int, detail: str = "") -> None:
        super().__init__(f"replay mismatch on {channel} #{seq}" + (f": {detail}" if detail else ""))
        self.channel = channel
        self.seq = seq
        self.detail = detail


class ForbiddenField(Exception):
    """写入 ssn、card_number、date_of_birth 等键（AC-32）。store.py 再导出。"""

    def __init__(self, keys: Sequence[str]) -> None:
        keys = tuple(keys)
        super().__init__("forbidden field(s): " + ", ".join(keys))
        self.keys = keys


# ---------------------------------------------------------------------------
# 基础服务
# ---------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    """单调毫秒、墙上时间与可替换的 sleep。所有超时（待决 20 s、接回 3 s、令牌 TTL）都经它。"""

    def monotonic_ms(self) -> int:
        raise NotImplementedError

    def now_iso(self) -> str:
        """当前时间，ISO 8601，带时区。"""
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


@runtime_checkable
class Store(Protocol):
    """SQLite 存储中跨线使用的部分（§6.8）。A 的 store.Store 实现，并另有 A 内部方法。"""

    def save_profile(self, fields: Mapping[str, str], *, company_id: str | None = None) -> Profile:
        """合并写入可代说字段；遇到 FORBIDDEN_FIELD_KEYS 或其他未知键抛 ForbiddenField，什么都不写（AC-32、A-05）。

        可写的键：Profile 的 name、phone_number、email、address、zip，以及 account_number。
        歧义表 A-04（定稿 map_by_company）：account_number 写进 Profile.accounts[company_id]
        （每家公司一个账户号，后写覆盖前写）；fields 含 account_number 却没给 company_id 时抛 ValueError。
        """
        raise NotImplementedError

    def get_profile(self) -> Profile:
        raise NotImplementedError

    def list_standing_rules(self) -> list[StandingRule]:
        """只返回 active 的规则；初始化后有 3 条（R47、AC-56）。"""
        raise NotImplementedError

    def list_preferences(self, company_id: str) -> list[Preference]:
        raise NotImplementedError

    def add_preference(self, pref: Preference) -> None:
        """只写 stakes ≤1 的选择（AC-58；Preference 模型已限制）。"""
        raise NotImplementedError


@runtime_checkable
class Trace(Protocol):
    """trace 写入与查询（§6.11）。A 的 trace.Trace 实现。

    歧义表 A-24（定稿 methods_callresult）：check_call(call_id)、metrics(task_id)、menu_to_dtmf_ms(call_id)
    是 A 的 trace.Trace 类的方法（cli 与 A 的测试直接用该类），不在这个跨线协议里。
    """

    def event(self, ev: TraceEvent) -> int:
        """写一条事件，返回分配的 seq。

        歧义表 A-25（定稿 keep_all_relax_trace_event）：写入时检查、不合格抛 ValueError 且不写：
        - event_type 属于 CONSEQUENTIAL_EVENTS 而没有 decision_id；
        - decision 事件缺 DECISION_EVENT_REQUIRED 任一项；
        - 歧义表 A-08：decision_point=call_round 的 decision 事件，action 里没有整数 turn_seq。
        读回（events()）时不再校验这些，坏行由 Trace.check_call() 报成 Violation。
        """
        raise NotImplementedError

    def has_decision(self, decision_id: str) -> bool:
        """是否已有该 decision_id 的 decision 事件（ActionBackend 据此抛 UnknownDecision，AC-70）。"""
        raise NotImplementedError

    def events(self, *, call_id: str | None = None, task_id: str | None = None) -> list[TraceEvent]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 模型客户端
# ---------------------------------------------------------------------------


@runtime_checkable
class JevClient(Protocol):
    """Jev System One 客户端（§6.6）。live/text-sim 用 HTTP；replay 用 ReplayPlayer。

    歧义表 A-06（定稿 infer_from_questions，暂定）：签名同 §7 的 ask(state, questions)，不带决策点。
    回放录制要的 shape.point 由录制器与回放器用 decision.registry.Registry.point_of(questions 的键)
    反推；registry 保证各决策点的题名全局唯一。
    """

    async def ask(self, state: JevState, questions: Mapping[str, JevQuestion]) -> JevResponse:
        raise NotImplementedError


@runtime_checkable
class ConverseClient(Protocol):
    """Bedrock Converse（§6.7）。返回 boto3 converse 的原始响应字典。bedrock.py 再导出。"""

    async def converse(self, call: ConverseCall) -> dict[str, Any]:
        raise NotImplementedError


@runtime_checkable
class LlmTasks(Protocol):
    """L1–L5（§6.7）。输出只当数据（H3）：只填字段或给出文字，不触发动作。"""

    async def extract_request(self, user_text: str) -> ExtractedRequest:
        """L1：强制抽取工具，原样抽取。"""
        raise NotImplementedError

    async def phrase_reply(
        self,
        *,
        skill_body: str,
        decision: AssistantDecision,
        tool_result: ToolResultIn | None,
        user_text: str | None,
    ) -> str:
        """L2：system = assisted-call SKILL.md 正文；1–2 句英文。"""
        raise NotImplementedError

    async def extract_offer_options(self, company_text: str) -> list[OfferOption]:
        """L3：对方本轮原话里的选项，最多 3 个，原样抽取（AC-86）。"""
        raise NotImplementedError

    async def extract_result_items(self, transcript: Sequence[CallTranscriptLine]) -> ResultItems:
        """L4：确认号与陈述（AC-37、AC-38）。"""
        raise NotImplementedError

    async def write_summary(self, *, result: CaseResultOut, transcript: Sequence[CallTranscriptLine]) -> str:
        """L5：≤3 句。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 决策
# ---------------------------------------------------------------------------


@runtime_checkable
class DecisionGate(Protocol):
    """决策门（§6.5、§6.6）。每次决策：组 Jev 请求 → Jev → policy → 写 decision 事件 → 返回。

    返回前必须已经写入 decision 事件（含 jev_request、jev_response、policy_decision），
    这样 ActionBackend 才能按 decision_id 找到它（AC-34、AC-70）。

    歧义表 A-01（定稿 request_optional_fields）：Jev state 只由 gate 调 decision.jev.build_state 组装
    （白名单与 8 行截断只在这里）；调用方只交 RoundContext / DecisionRequest 里的文字与上下文。
    """

    async def decide_round(self, turn: CompanyTurn, ctx: RoundContext) -> DecisionResult:
        """DP-C1 call_round：一次 Jev 请求问 4 题，再按 call_round 求值顺序出结果。
        写 decision 事件时 action={"turn_seq": turn.seq}（A-08）。"""
        raise NotImplementedError

    async def decide(self, point: DecisionPoint, request: DecisionRequest) -> DecisionResult:
        """其余 7 个决策点。WAIT_ONLY_POINTS 返回 action=wait、选择放 intent；
        said_as_decided 按 request.attempt 返回 said_ok / reinject / safe_default（A-02）。"""
        raise NotImplementedError


@runtime_checkable
class PendingRegistry(Protocol):
    """待决选项（§6.5 ask_user；AC-50、AC-86）。"""

    def create(
        self,
        *,
        call_id: str,
        decision_id: str,
        question_text: str,
        options: Sequence[Option],
        timeout_s: float,
    ) -> PendingChoice:
        raise NotImplementedError

    def get(self, choice_id: str) -> PendingChoice | None:
        raise NotImplementedError

    def current(self, call_id: str) -> PendingChoice | None:
        """该通电话当前 open 的待决选项。"""
        raise NotImplementedError

    def answer(self, choice_id: str, option_id: str) -> PendingChoice:
        """记录作答，返回 status=answered；已过期返回 status=expired。
        choice_id 未知抛 ToolFailure("unknown_choice")，option_id 不在选项里抛 ToolFailure("invalid_option")。
        """
        raise NotImplementedError

    def expire_due(self) -> list[PendingChoice]:
        """把到期的 open 选项标为 expired 并返回它们；调用方随后走 safe_default。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 通话
# ---------------------------------------------------------------------------


@runtime_checkable
class ActionBackend(Protocol):
    """动作执行（§6.5）。live 与 text 两种实现共用这个接口。"""

    async def execute(self, result: DecisionResult) -> ActionRecord:
        """先 trace.has_decision(result.decision_id)，否则抛 UnknownDecision（AC-70）。"""
        raise NotImplementedError


@runtime_checkable
class LineTokenIssuer(Protocol):
    """每通线路令牌（AC-62）。拨号时签发，/ws/line 握手时校验。"""

    def issue(self, call_id: str) -> str:
        raise NotImplementedError

    def verify(self, token: str) -> str | None:
        """有效时返回 call_id，否则 None（端点随后以 4401 关闭）。"""
        raise NotImplementedError

    def revoke(self, call_id: str) -> None:
        """撤销本通现有令牌；未知或重复撤销无害，之后仍可重新签发。"""
        raise NotImplementedError


@runtime_checkable
class HandoffTokenIssuer(Protocol):
    """转接一次性令牌（AC-63），TTL = engine.handoff.token_ttl_s。

    v5.27 live 的连接代次由 LiveCall 持有：重连撤销旧代次令牌，不能只靠一次消费和 TTL。
    """

    def issue(self, call_id: str, handoff_id: str) -> tuple[str, str]:
        """返回 (token, expires_at ISO 8601)。"""
        raise NotImplementedError

    def consume(self, handoff_id: str, token: str) -> str | None:
        """一次性：有效则作废并返回 call_id；过期、重复或不匹配返回 None。"""
        raise NotImplementedError

    def revoke(self, call_id: str) -> None:
        """撤销本通所有未消费令牌；旧代次 consume 返回 None，不禁止重新签发。"""
        raise NotImplementedError


@runtime_checkable
class CallSession(Protocol):
    """一通电话（§7 calls.py 的 interfaces.CallSession）。LiveCall 与 TextCall 实现。

    状态与结果只经这些方法对外；结束时调用 CallDeps.on_ended(call_id)，
    由 A 的结果流程（results.finalize）接手。
    """

    call_id: str
    task_id: str

    @property
    def scenario_id(self) -> str | None:
        """本通实际用的场景（calls.scenario_id、get_case_result.calls[].scenario_id）。
        text-sim/replay：加载的场景（= CallSpec.scenario_id，A-11）；live-sim：start() 里拨号前
        GET /sim/scenario 得到的值，GET 失败为 None（A-10）。CallManager 在 start() 返回后读它写 calls 行。"""
        raise NotImplementedError

    async def start(self) -> None:
        """开始拨号（live：签发线路令牌、GET /sim/scenario、POST /carrier/calls；text：启动文本公司）。"""
        raise NotImplementedError

    def status(self, debug: bool = False) -> CallStatusOut:
        """phase 按 models.CALL_PHASE_FROM_JEV 的规则取（缺口 D-G-11）：拨号后到第一轮前 dialing，之后按最近一轮的
        call_phase 映射，awaiting_user、handoff_ready、in_handoff、resuming 优先于映射，挂断后 ended。
        转接中（有效 hello 并送出 ready 后）必须是 in_handoff。
        v5.27 live 的 handoff 提供服务器权威 expires_at/remaining_s/reason_kind/generation；
        首次期限从 ready 起算，坏 hello/Join 重试不重置期限，重连不重复写首次通知事件。"""
        raise NotImplementedError

    async def answer(self, choice_id: str, option_id: str) -> AnswerChoiceOut:
        raise NotImplementedError

    async def join(self) -> JoinCallOut:
        """未到 handoff_ready 时抛 ToolFailure("handoff_not_ready")。"""
        raise NotImplementedError

    async def return_to_agent(self, handoff_id: str) -> ReturnOut:
        """保留 text 接回语义；没有进行中的转接时抛 ToolFailure("no_active_handoff")。
        v5.27 live 不通过此入口恢复 AI，用户主动结束使用 hangup_call。
        """
        raise NotImplementedError

    async def attach_handoff(self, websocket: Any, handoff_id: str) -> None:
        """接管已校验令牌的 /ws/handoff 连接（§6.4；text 模式只回 ready 和 ended）。

        缺口 E-G-6（定稿 endpoint_accepts）：D 的 handoff_endpoint 先 accept、再核对令牌（无效 close(4401)），
        交进来的是已 accept 的连接，这里不再 accept。
        缺口 E-G-5（定稿 endpoint_owns_timer）：连接关闭（不论是否经 return_to_agent）时本方法返回；
        text 没经 return 就断开的自动接回由 handoff_endpoint 统一计时（engine.handoff.resume_grace_ms 后调
        return_to_agent(handoff_id)）。v5.27 live 由 LiveCall 管期限与连接代次，保持媒体隔离，
        回 ready 并发新令牌，rejoin_grace_s 到期挂断；旧连接回调不能结束新代次，不恢复 AI。
        缺口 D-G-5（定稿 check_phase）：返回值不变（None）；endpoint 用 status().phase 是否仍为 in_handoff 来区分
        「用户点了 Return」与「断线」，所以用户接入期间 phase 必须是 in_handoff，经 return 后不再是。
        """
        raise NotImplementedError

    async def hangup(self, reason: str) -> None:
        """幂等结束本地通话；live 包含 user_hangup/join_timeout/rejoin_timeout。
        carrier 失败也结束本地通话，保留原 call_id 的远端重试目标；挂断动作输出
        remote_hangup_pending 表示远端仍待重试。text 的 end 动作保持原语义。
        """
        raise NotImplementedError

    @property
    def ended(self) -> bool:
        raise NotImplementedError

    @property
    def remote_hangup_pending(self) -> bool:
        """LiveSession owns its remote retry state; text/replay always return False."""
        raise NotImplementedError

    async def wait_ended(self) -> None:
        raise NotImplementedError


@runtime_checkable
class LiveSession(CallSession, Protocol):
    """live-sim 的通话：额外接入运营商回连的 /ws/line。"""

    async def attach_line(self, websocket: Any, start: StartMsg) -> None:
        """line_endpoint 先 accept、再校验令牌（无效 close(4401)，E-G-6 同一做法）、读完 connected 与 start 后调用；
        交进来的是已 accept 的连接；返回时管线已结束。"""
        raise NotImplementedError


@runtime_checkable
class SonicTap(Protocol):
    """Nova Sonic 的 provider 文字事件订阅（S0-r2 V8、V12）。

    D 线在 call/pipeline.py 用 AWSNovaSonicLLMService 的子类实现（在 provider 的
    contentEnd 处理里先发事件再走父类逻辑）；FakeSonicService 实现同一接口。
    歧义表 A-12（定稿 provider_event_listener）：每通电话只有 call/live.py 的 TranscriptTap 一个订阅者；
    它在转接期间丢弃事件（AC-75），其余转给 TurnDetector（role=USER、stage=FINAL）、
    LiveActionBackend 的 DP-C2 核对（注入后的下一段 role=ASSISTANT、stage=FINAL）与 trace。

    缺口 D-G-8（定稿 listeners_must_not_block）：回调不得阻塞，也不得等待后续的 Sonic 事件——事件就是从这个回调
    发出来的，真实服务里会卡住 provider 的接收任务。回调只做记录与分发；要等后续事件的工作（DP-C2 决策、
    等待说完、转接流程）用 asyncio.create_task 交给后台任务，回调本身尽快返回。实现方（TappedNovaSonicService、
    FakeSonicService）照常同步 await 回调；后台任务的结束与异常由订阅方（D 的 LiveCall）管理。
    """

    def add_text_listener(self, listener: Callable[[ProviderTextEvent], Awaitable[None]]) -> None:
        raise NotImplementedError


@runtime_checkable
class CallRegistry(Protocol):
    """按 call_id 找到进行中的通话（A 的 CallManager 实现；D 的 WebSocket 端点调用）。"""

    def get_session(self, call_id: str) -> CallSession | None:
        raise NotImplementedError


@runtime_checkable
class CompanyEngine(Protocol):
    """文本级的对方公司（§6.10；B 的 company/text.py TextCompany 实现，E 的 TextCall 调用）。

    每个方法返回公司方随后说出的若干句；kind=hangup 的一句表示公司挂断。

    缺口 B-G-5（定稿 same_batch，暂定）：文本模式没有时间。hold 不占时间，hold 那句之后同一批接着给出下一步的台词；
    不需要回应的台词（expect.intent=wait）之后也在同一批继续，直到一句需要 Waitless 回应的台词或挂断为止。
    所以每一批都以「需要回应的一句」或 kind=hangup 结尾；E 的 TextCall 逐句处理即可，接口没有「时间流逝」的方法。
    """

    scenario_id: str

    def start(self) -> list[CompanyLine]:
        """接通：开场白与第一层菜单。"""
        raise NotImplementedError

    def on_digits(self, digits: str) -> list[CompanyLine]:
        raise NotImplementedError

    def on_speech(self, text: str) -> list[CompanyLine]:
        """Waitless 说的一句话（文本）：say 的注入文字，或 text-sim 下 release 时按 open_answer.<field>
        模板拼出的回答（A-16）。"""
        raise NotImplementedError

    def on_timeout(self) -> list[CompanyLine]:
        """菜单等待超时一次；连续 max_timeouts 次后挂断（AC-83）。"""
        raise NotImplementedError

    def on_handoff(self, joined: bool) -> list[CompanyLine]:
        """账户持有人加入（True）或离开（False）。文本模式下加入即视为本人验证通过。

        缺口 B-G-8（定稿 rep_lines_during_handoff）：on_handoff(True) 返回坐席对账户持有人说的几句（如验证提问），
        E 的 TextCall 只记进状态，不送决策门、不写 company_turn（AC-75）；on_handoff(False) 返回坐席接着对 Waitless
        说的台词，以需要回应的一句结尾。
        """
        raise NotImplementedError

    @property
    def ended(self) -> bool:
        raise NotImplementedError

    def step_log(self) -> list[str]:
        """按顺序走过的 step_id（AC-82）。"""
        raise NotImplementedError


@dataclass
class CallDeps:
    """建 CallSession 时注入的服务（A 组装）。

    jev 给 live 的 ask_jev 用（v5.20 E129）。
    """

    settings: Settings
    store: Store
    trace: Trace
    gate: DecisionGate
    pending: PendingRegistry
    llm: LlmTasks
    clock: Clock
    line_tokens: LineTokenIssuer
    handoff_tokens: HandoffTokenIssuer
    on_ended: Callable[[str], Awaitable[None]]
    company_factory: Callable[[str | None], CompanyEngine] | None = None
    jev: JevClient | None = None
    # D must await this only after validating hello and successfully sending ready.
    # Token issuance, handoff_started and phase changes do not establish this fact.
    on_handoff_connected: Callable[[str], Awaitable[None]] | None = None


@runtime_checkable
class CallSessionFactory(Protocol):
    """按模式建 CallSession：live-sim → call/live.py create_live_call；
    text-sim、replay → call/textcall.py create_text_call。"""

    def __call__(self, spec: CallSpec, deps: CallDeps) -> CallSession:
        raise NotImplementedError


@dataclass
class AppServices:
    """create_app() 放在 app.state.services 上的跨线服务；D 的 line_endpoint、handoff_endpoint 读取。"""

    settings: Settings
    store: Store
    trace: Trace
    clock: Clock
    calls: CallRegistry
    line_tokens: LineTokenIssuer
    handoff_tokens: HandoffTokenIssuer


__all__ = [
    "ActionBackend",
    "AppServices",
    "CallDeps",
    "CallRegistry",
    "CallSession",
    "CallSessionFactory",
    "Clock",
    "CompanyEngine",
    "ConverseClient",
    "DecisionGate",
    "ForbiddenField",
    "HandoffTokenIssuer",
    "JevClient",
    "LineTokenIssuer",
    "LiveSession",
    "LlmTasks",
    "PendingRegistry",
    "ReplayMismatch",
    "SonicTap",
    "Store",
    "ToolFailure",
    "Trace",
    "UnknownDecision",
]
