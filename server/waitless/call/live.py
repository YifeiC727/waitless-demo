"""live-sim 的一通电话：拨号、接线、管线、决策循环、转接（D 线；tech §6.3–§6.5、§8；AC-75、AC-76、AC-95）。

公开接口（§7）：LiveCall（实现 interfaces.LiveSession）；另有 TranscriptTap、CarrierClient、create_live_call。
"""

from typing import Any

from waitless.interfaces import CallDeps
from waitless.models import (
    AnswerChoiceOut,
    CallStatusOut,
    CallSpec,
    CarrierDialIn,
    CarrierDialOut,
    CarrierHangupOut,
    JoinCallOut,
    ProviderTextEvent,
    ReturnOut,
    ScenarioSelection,
)

__all__ = ["CarrierClient", "LiveCall", "TranscriptTap", "create_live_call"]


class TranscriptTap:
    """截取 USER FINAL 与 ASSISTANT FINAL，交给 TurnDetector、LiveActionBackend 与 trace（§6.5）。

    歧义表 A-12（定稿 provider_event_listener）：类名不变，但不再是管线里的一格——它是 SonicTap 唯一的订阅者
    （sonic.add_text_listener(tap.on_provider_text)）。转接期间一律丢弃、不写 trace（AC-75），这一判断只在这里做。
    """

    def __init__(self, *, deps: CallDeps, spec: CallSpec, turn_detector: Any, actions: Any, media_gate: Any) -> None:
        raise NotImplementedError

    async def on_provider_text(self, ev: ProviderTextEvent) -> None:
        """SonicTap 回调：不得阻塞，也不得等后续 Sonic 事件（缺口 D-G-8）；要等的工作交给后台任务。"""
        # todo(D): media_gate.in_handoff → 丢弃。USER FINAL → turn_detector.on_provider_text；
        #          ASSISTANT FINAL → waitless_utterance 事件 + actions.on_assistant_final（它自己把 DP-C2 放进后台任务）。
        #          SPECULATIVE 不写 trace。
        raise NotImplementedError


class CarrierClient:
    """模拟运营商 API 的客户端（§6.3）。base_url = Settings.carrier_url；http 可注入（测试用 FakeCarrier.transport）。"""

    def __init__(self, *, base_url: str, http: Any | None = None) -> None:
        raise NotImplementedError

    async def dial(self, req: CarrierDialIn) -> CarrierDialOut:
        """POST /carrier/calls；404 unknown_number 抛 ToolFailure 或专门异常（D 线定）。"""
        raise NotImplementedError

    async def hangup(self, call_sid: str) -> CarrierHangupOut:
        raise NotImplementedError

    async def set_scenario(self, sel: ScenarioSelection) -> ScenarioSelection:
        """PUT /sim/scenario（评测每次拨号前调用，AC-84、AC-93）。"""
        raise NotImplementedError

    async def get_scenario(self, line_number: str) -> ScenarioSelection:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError


class LiveCall:
    """一通 live 电话。转接只切换媒体门，不重拨、不新建线路，callSid 不变（AC-76）。"""

    def __init__(self, spec: CallSpec, deps: CallDeps, *, carrier: CarrierClient | None = None) -> None:
        raise NotImplementedError

    @property
    def call_id(self) -> str:
        raise NotImplementedError

    @property
    def task_id(self) -> str:
        raise NotImplementedError

    @property
    def scenario_id(self) -> str | None:
        """拨号前 GET /sim/scenario 得到的场景；GET 失败为 None（A-10）。"""
        raise NotImplementedError

    async def start(self) -> None:
        # todo(D): A-10（定稿 nullable_get_before_dial）：先 carrier.get_scenario(line_number=spec.to_number)，
        #          成功记下 scenario_id，失败（含 404）记 None，不阻止拨号。
        # todo(D): token = deps.line_tokens.issue(call_id)；carrier.dial(CarrierDialIn(stream_url=settings.line_ws_url,
        #          stream_token=token, to_number=spec.to_number, ...))；phase=dialing；写 call_started。
        raise NotImplementedError

    async def attach_line(self, websocket: Any, start: Any) -> None:
        """缺口 D-G-6（定稿 document_patch_points）——测试替换点的约定：transport 与 Sonic 服务必须在调用时经
        waitless.call.pipeline 模块上的 build_transport、build_sonic_service 构造（写成 `pipeline.build_transport(...)`，
        或本模块顶层同名导入 `from waitless.call.pipeline import build_transport, build_sonic_service` 后在调用时按名字取）；
        不要在导入时把函数存进别的别名、默认参数或闭包里，测试会替换这两个模块属性。"""
        # todo(D): 构造 LineSerializer、transport、MediaGate、TurnDetector、TappedNovaSonicService、OutputGuard、
        #          SonicTextInjector、LiveActionBackend(..., call_sid=start.start.callSid)（核对等于拨号响应的 call_sid，D-G-1）；
        #          sonic.add_text_listener(TranscriptTap.on_provider_text)；跑 PipelineTask 直到结束；结束时 deps.on_ended(call_id)。
        # todo(D): 缺口 D-G-3（定稿 livecall_writes）：TurnDetector 的 on_turn 回调由本类提供，依次做——写 company_turn
        #          （speaker=company、transcript=turn.text、action={"kind", "seq"}、t_ms=turn.t_end_ms，A-08）→
        #          output_guard.on_turn(turn)（A-15）→ 组 RoundContext（transcript 不截断，A-01；prior_choices 含任务卡决定、
        #          本通作答与 Store 偏好，A-03）→ deps.gate.decide_round → actions.execute(result)。
        #          handoff_ended 也由本类写：return_to_agent（含 endpoint 的自动接回）里 media_gate.exit_handoff() 之后写。
        # todo(D): 全部时间戳（CompanyTurn、ProviderTextEvent、OutputGuard、trace）都用 deps.clock（A-15、A-08）。
        # todo(D): TurnDetector 的 menu_patterns、digit_words 取 spec.compiled.press_patterns、spec.compiled.digit_words（E-G-4）。
        # todo(D): 不写 user_action 事件（缺口 A-G-9：只由 A 的 CallManager 写）；不依赖运营商回显 mark（缺口 B-G-10）。
        # todo(D): websocket 已由 line_endpoint accept（缺口 E-G-6），这里不再 accept。
        raise NotImplementedError

    def status(self, debug: bool = False) -> CallStatusOut:
        # todo(D): phase 按 models.CALL_PHASE_FROM_JEV 的规则（缺口 D-G-11）：拨号后到第一轮前 dialing，之后按最近一轮
        #          DecisionResult.call_phase 映射，awaiting_user、handoff_ready、in_handoff、resuming 优先，挂断后 ended；
        #          用户接入转接期间必须是 in_handoff（handoff_endpoint 据此判断断线，D-G-5）。
        # todo(D): 转接期间 transcript_tail=[]；debug 带 skills、last_decision、guard
        #          （last_decision 按 task_id 取本任务最近一条决策：deps.trace.events(task_id=…) 里最后一条 decision，
        #          含 DP-A1、P1–P3，A-09）。
        raise NotImplementedError

    async def answer(self, choice_id: str, option_id: str) -> AnswerChoiceOut:
        raise NotImplementedError

    async def join(self) -> JoinCallOut:
        raise NotImplementedError

    async def return_to_agent(self, handoff_id: str) -> ReturnOut:
        """media_gate.exit_handoff() → 写 handoff_ended（D-G-3）→ 注入 "handoff.resume" → phase=resuming（AC-78）。
        没有进行中的转接时抛 ToolFailure("no_active_handoff")（endpoint 自动接回时吞掉它，D-G-5）。"""
        raise NotImplementedError

    async def attach_handoff(self, websocket: Any, handoff_id: str) -> None:
        """websocket 已由 handoff_endpoint accept 并核对令牌（E-G-6）；连接关闭时返回，断开后的自动接回由 endpoint 计时（E-G-5）。"""
        raise NotImplementedError

    async def hangup(self, reason: str) -> None:
        raise NotImplementedError

    @property
    def ended(self) -> bool:
        raise NotImplementedError

    async def wait_ended(self) -> None:
        raise NotImplementedError


def create_live_call(spec: CallSpec, deps: CallDeps) -> LiveCall:
    """interfaces.CallSessionFactory（live-sim）。"""
    raise NotImplementedError
