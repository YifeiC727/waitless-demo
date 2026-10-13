"""Sonic 输出把关：逐段文字闸门（D 线；tech §6.5、S0-r2 V8；AC-67、AC-68、AC-69；K1）。

公开接口（§7）：OutputGuard（FrameProcessor，.set_state()、.release(seq)）。
"""

from collections.abc import Sequence
from typing import Any, Literal

from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from waitless.interfaces import Clock, Trace
from waitless.models import CompanyTurn, GuardCounters

__all__ = ["GuardState", "OutputGuard"]

GuardState = Literal["ivr", "conversation", "injected", "handoff", "ending"]


class OutputGuard(FrameProcessor):
    """S0-r2 V8 定论（逐段文字闸门）：
    - 以一段回应为单位：LLMFullResponseStartFrame 开始、LLMFullResponseEndFrame 结束，按出现顺序编号。
    - 每段回应的音频（TTSAudioRawFrame）先扣住，等这段自己的文字到达再判断；文字取 SPECULATIVE 的
      LLMTextFrame（不能当 FINAL，也不用 TTSTextFrame）。
    - 状态规则（§6.5）：
        ivr           全部丢弃（AC-67）
        conversation  文字属于 compiled.stall_phrases → 立即放行（AC-68）；否则扣住等这一轮的决策：
                      release → 放出；其他动作 → 丢弃、broadcast_interruption、记 guard_blocked（AC-69）
        injected      注入后的下一段回应直接放行
        handoff       全部丢弃
        ending        道别话术放行
    - 文字不到就一直扣住；扣住超过 guard.held_response_max_ms 也丢弃并记 guard_blocked。
    - 生命周期帧、InterruptionFrame 等系统帧照常传递，不能吞掉。
    """

    def __init__(
        self,
        *,
        stall_phrases: Sequence[str],
        held_response_max_ms: int,
        trace: Trace,
        call_id: str,
        task_id: str,
        clock: Clock,
    ) -> None:
        raise NotImplementedError

    def set_state(self, state: GuardState) -> None:
        """进入哪个状态由策略结果与 call_phase 决定（LiveActionBackend / LiveCall 调用）。"""
        raise NotImplementedError

    @property
    def state(self) -> GuardState:
        raise NotImplementedError

    def on_turn(self, turn: CompanyTurn) -> None:
        """LiveCall 在把 CompanyTurn 交给决策门之前调用：记下 seq → (t_start_ms, t_end_ms)，release/discard 按它配对。"""
        raise NotImplementedError

    async def release(self, seq: int) -> bool:
        """放出为第 seq 轮（CompanyTurn.seq）扣住的回应；没有则返回 False。

        歧义表 A-15（定稿 by_company_speech_end）：回应按开始时刻归属。每段回应开始（LLMFullResponseStartFrame）
        时用 clock.monotonic_ms() 记下开始时刻——与 TurnDetector 产出 CompanyTurn 用同一个 deps.clock。
        开始时刻晚于第 seq 轮 t_end_ms（对方这句话结束）的第一段回应归第 seq 轮；开始时刻晚于下一轮
        t_start_ms 的回应归下一轮。所以回应即使早于本轮 CompanyTurn 产出（TurnDetector 还在等静音）也能配上；
        D 的测试要用 FakeClock 覆盖这种情形。"""
        raise NotImplementedError

    async def discard(self, seq: int, reason: str) -> int:
        """丢弃第 seq 轮扣住的回应并广播 interruption，返回丢弃的音频帧数。"""
        raise NotImplementedError

    @property
    def counters(self) -> GuardCounters:
        raise NotImplementedError

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        # todo(D): 按类说明实现；每次放行/丢弃写 guard_released / guard_blocked 事件。
        raise NotImplementedError
