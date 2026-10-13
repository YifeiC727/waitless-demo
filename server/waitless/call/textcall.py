"""text-sim 与 replay 的通话实现（E 线；tech §5.1、§6.5、§8 末段；AC-70、AC-85、AC-86、AC-87）。

公开接口（§7）：TextCall、TextActionBackend.execute(result)。与 live 共用 ActionBackend 接口和同一条决策链：
TextCompany 给出台词，TextCall 把动作翻译成文本公司的输入，没有音频。
本模块不得导入 waitless.call.pipeline、Pipecat 或任何 WebSocket 客户端（AC-87）。
"""

from typing import Any

from waitless.interfaces import CallDeps, CompanyEngine
from waitless.models import (
    ActionRecord,
    AnswerChoiceOut,
    CallSpec,
    CallStatusOut,
    CompanyLine,
    DecisionResult,
    JoinCallOut,
    ReturnOut,
)

__all__ = ["TextActionBackend", "TextCall", "create_text_call"]


class TextActionBackend:
    """实现 interfaces.ActionBackend（文本版）。"""

    def __init__(self, *, call: "TextCall", deps: CallDeps, company: CompanyEngine) -> None:
        raise NotImplementedError

    async def execute(self, result: DecisionResult) -> ActionRecord:
        """先 deps.trace.has_decision(result.decision_id)，否则抛 UnknownDecision（AC-70，与 live 相同）。"""
        # todo(E): press → company.on_digits(digits)，写 dtmf_sent。
        # todo(E): say → company.on_speech(say_text)，写 injection_sent 与 waitless_utterance；DP-C2 用这句文字核对：
        #          DecisionRequest(point="said_as_decided", transcript 最后一行=这句, template_vars={SAID_SUMMARY_VAR: say_text},
        #          attempt=0/1)，按返回 action 执行 said_ok / reinject / safe_default（A-02，与 live 相同）。
        # todo(E): release（A-16，定稿 field_templates）→ 文本模式没有 Sonic 草稿：按 result.turn_seq（缺口 D-G-2）找到那一轮，
        #          用 compiled.lexicons 里 "field_cues.<field>"（models.FIELD_CUES_PREFIX）把对方这一轮的问句对到字段，取
        #          say_templates["open_answer.<field>"] 用 allowed_fields 与任务卡的值（如 effective_date）format，
        #          再 company.on_speech(...)；对不上字段或缺值就用 say_templates["ask_repeat"]。不调用 Lite。
        # todo(E): ask_user → L3 extract_offer_options（AC-86）→ deps.pending.create → 等作答或超时 → 转告公司。
        # todo(E): handoff → 与 live 同一事件顺序（缺口 D-G-4）：注入 handoff.announce（company.on_speech）→ 写 handoff_announced
        #          → 写 handoff_ready、phase=handoff_ready → 写 handoff_started（带本次 decision_id，由本类写，D-G-3）；
        #          用户 join 后 phase=in_handoff、company.on_handoff(True)；return/断开后 on_handoff(False)，TextCall 写
        #          handoff_ended（action={"dropped_sonic_outputs": 0}，text 模式没有 Sonic，D-G-4b），phase=resuming。
        #          on_handoff(True) 返回的是坐席对账户持有人说的话，只记进状态，不送决策门、不写 company_turn；
        #          on_handoff(False) 返回的台词照常逐句处理（缺口 B-G-8）。
        # todo(E): 不写 user_action 事件（缺口 A-G-9：只由 A 的 CallManager 写）。
        # todo(E): end → company 挂断 → 写 hangup → deps.on_ended(call_id)。
        raise NotImplementedError


class TextCall:
    """实现 interfaces.CallSession（text-sim / replay）。台词节奏按 settings.engine.textsim.pace_ms（0 表示不等；
    网页演示时 WAITLESS_TEXTSIM_PACE_MS 已由 load_settings 覆盖进这个值，A-20）。"""

    def __init__(self, spec: CallSpec, deps: CallDeps, company: CompanyEngine) -> None:
        raise NotImplementedError

    @property
    def call_id(self) -> str:
        raise NotImplementedError

    @property
    def task_id(self) -> str:
        raise NotImplementedError

    @property
    def scenario_id(self) -> str | None:
        """= spec.scenario_id（CallManager 已按 A-11 定好：text-sim 参数或默认 baseline，replay 取回放头）。"""
        raise NotImplementedError

    async def start(self) -> None:
        # todo(E): 公司每批台词以「需要回应的一句」或 hangup 结尾，hold 与 wait 台词之后同一批接着说（缺口 B-G-5，暂定）；
        #          逐句组 CompanyTurn，菜单判断用 parse_menu(text, spec.compiled.press_patterns,
        #          digit_words=spec.compiled.digit_words or None)（缺口 E-G-4、G-G-03）。
        # todo(E): CompanyTurn 的 t_start_ms、t_end_ms 用 deps.clock.monotonic_ms()（A-15）；每轮组 RoundContext，
        #          prior_choices 含任务卡决定、本通作答与 Store 偏好（PriorChoice.from_preference，A-03）。
        # todo(E): 后台任务跑循环：company.start() 的台词 → 按 kind 组 CompanyTurn（menu 用 decision.menu_parse）→
        #          deps.gate.decide_round → TextActionBackend.execute → 下一批台词，直到挂断；超时用 deps.clock。
        raise NotImplementedError

    def status(self, debug: bool = False) -> CallStatusOut:
        """phase 按 models.CALL_PHASE_FROM_JEV 的规则取，与 LiveCall 相同（缺口 D-G-11）；用户接入转接期间是 in_handoff（D-G-5）。"""
        raise NotImplementedError

    async def answer(self, choice_id: str, option_id: str) -> AnswerChoiceOut:
        raise NotImplementedError

    async def join(self) -> JoinCallOut:
        """签发 deps.handoff_tokens 的一次性令牌（与 live 相同的 4401 规则，AC-63）。"""
        raise NotImplementedError

    async def return_to_agent(self, handoff_id: str) -> ReturnOut:
        raise NotImplementedError

    async def attach_handoff(self, websocket: Any, handoff_id: str) -> None:
        """§6.4：text-sim 与 replay 下只回 ready 和 ended，收到的音频直接丢弃，不存。
        websocket 已由 D 的 handoff_endpoint accept 并核对令牌，这里不再 accept（缺口 E-G-6）；连接关闭时返回，
        没经 return 就断开的自动接回由 endpoint 计时后调 return_to_agent（缺口 E-G-5）。"""
        raise NotImplementedError

    async def hangup(self, reason: str) -> None:
        raise NotImplementedError

    @property
    def ended(self) -> bool:
        raise NotImplementedError

    async def wait_ended(self) -> None:
        raise NotImplementedError

    def lines(self) -> list[CompanyLine]:
        """本通公司方说过的全部台词（评测比对 expect 用）。"""
        raise NotImplementedError


def create_text_call(spec: CallSpec, deps: CallDeps) -> TextCall:
    """interfaces.CallSessionFactory（text-sim、replay）。公司用 deps.company_factory(spec.scenario_id)，
    没有时用 waitless.company.text.TextCompany 加载 spec.scenario_id（A-11：CallManager 已填好，不会是 None）。"""
    raise NotImplementedError
