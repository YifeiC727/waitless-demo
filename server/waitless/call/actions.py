"""live 通话的动作执行器（D 线；tech §6.5 动作表；AC-66、AC-70、AC-72、AC-77；K1）。

公开接口（§7）：LiveActionBackend.execute(result)、UnknownDecision（定义在 interfaces，这里再导出；
E 线的 TextActionBackend 做同样的检查）。
"""

from typing import Any

from waitless.interfaces import CallDeps, UnknownDecision
from waitless.models import ActionRecord, CallSpec, DecisionResult, ProviderTextEvent

__all__ = ["LiveActionBackend", "UnknownDecision"]


class LiveActionBackend:
    """实现 interfaces.ActionBackend。

    缺口 D-G-3（定稿 livecall_writes）：本类写自己动作产生的事件——dtmf_sent、injection_sent（经 injector）、
    pending_choice_created、handoff_announced、handoff_ready、handoff_started（在 media_gate.enter_handoff() 之后，
    带这次 handoff 决策的 decision_id）、hangup、say_mismatch。company_turn 与 handoff_ended 由 LiveCall 写；
    MediaGate、TurnDetector 不写 trace。
    """

    def __init__(
        self,
        *,
        spec: CallSpec,
        deps: CallDeps,
        injector: Any,
        output_guard: Any,
        media_gate: Any,
        pipeline_task: Any,
        carrier: Any,
        call_sid: str,
    ) -> None:
        """缺口 D-G-1（定稿 call_sid_kwarg）：call_sid 是本通在运营商那边的 id，LiveCall 在 attach_line 读完 start 后
        建本类时传入（核对它等于拨号响应 CarrierDialOut.call_sid）；end 动作用它调 carrier.hangup(call_sid)。"""
        raise NotImplementedError

    async def execute(self, result: DecisionResult) -> ActionRecord:
        """先 deps.trace.has_decision(result.decision_id)，否则抛 UnknownDecision（AC-70）。"""
        # todo(D): press    推一次 OutputDTMFUrgentFrame.from_string(digits)，随后补 engine.dtmf.tail_silence_ms 的
        #                   静音把重采样尾部推出去（S0 V4）；写 dtmf_sent（AC-66：每个决策只推一次）。
        # todo(D): say      injector.inject(say_text, decision_id)；随后等下一段 ASSISTANT FINAL 做 DP-C2（见 on_assistant_final）。
        # todo(D): release  output_guard.release(result.turn_seq)（缺口 D-G-2：turn_seq 由 G 的 gate 填在 call_round 结果里）。
        # todo(D): ask_user L3 extract_offer_options（失败用通用 accept/decline/decide_later）→ deps.pending.create(...)
        #                   → 注入 say_templates["handoff.ask_user"]；写 pending_choice_created；用户作答或超时后注入
        #                   "pending.answer" 或 "pending.safe_default"（沿用同一 decision_id）。
        # todo(D): handoff  缺口 D-G-4（定稿 ready_before_started，Jev .58）的固定顺序：注入 "handoff.announce" →
        #                   写 handoff_announced → 等 Sonic 说完（ASSISTANT FINAL 到达，且 OutputGuard 放行最后一帧后
        #                   满 engine.handoff.announce_idle_ms，AC-77）→ 写 handoff_ready、phase=handoff_ready →
        #                   media_gate.enter_handoff() → 写 handoff_started（带本次 decision_id，D-G-3）。
        #                   这样转接窗口里只有 handoff_started 与之后 LiveCall 写的 handoff_ended（AC-75）。
        # todo(D): end      注入 "end.goodbye"，等说完 → carrier.hangup(self.call_sid)（D-G-1）→ 写 hangup（带 decision_id）
        #                   → deps.on_ended(call_id)。
        # todo(D): wait     不动作。
        raise NotImplementedError

    async def on_assistant_final(self, ev: ProviderTextEvent) -> None:
        """注入后的下一段 ASSISTANT FINAL（SonicTap 事件，不是 TTSTextFrame）送 DP-C2 said_as_decided（AC-72）。

        歧义表 A-02（定稿 wait_except_c2）：
        - 组 DecisionRequest(point="said_as_decided", call_id, transcript=[…, 最后一行 speaker=waitless、text=ev.text],
          template_vars={models.SAID_SUMMARY_VAR: 本次注入的文字（不含前缀）}, attempt=0 或 1)，交 deps.gate.decide。
        - 按返回的 action 执行：said_ok（wait）→ 结束核对；reinject（say）→ injector.inject(say_text, 这条 DP-C2 的
          decision_id)，下一段 ASSISTANT FINAL 再核对一次（attempt=1）；safe_default → 写 say_mismatch 事件，
          再按 action 执行（建议 end：注入 end.goodbye、挂断，结果落到 needs_follow_up）。
        - 不自己比较阈值、不自己数次数之外的逻辑：判定全在 G 的策略里（D10、D11）。
        - 缺口 D-G-8（定稿 listeners_must_not_block）：本方法经 TranscriptTap 从 SonicTap 回调里调用，不得阻塞——
          DP-C2 决策与随后的重新注入用 asyncio.create_task 交给后台任务，这里立即返回。"""
        raise NotImplementedError
