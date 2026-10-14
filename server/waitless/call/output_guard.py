"""Sonic 输出把关：逐段文字闸门（D 线；tech §6.5、S0-r2 V8；AC-67、AC-68、AC-69；K1）。

公开接口（§7）：OutputGuard（FrameProcessor，.set_state()、.release(seq)）。
"""

from collections.abc import Sequence
from typing import Any, Literal

from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from waitless.interfaces import Clock, Trace
from waitless.models import CompanyTurn, GuardCounters, TraceEvent
import asyncio
from pipecat.frames.frames import (LLMFullResponseStartFrame, LLMFullResponseEndFrame,
    LLMTextFrame, OutputAudioRawFrame)

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
        handoff       全部丢弃，且不写 trace（不记 guard_blocked、guard_released），只在内存计数——
                      缺口 D-G-4b（定稿 no_trace_in_handoff，Jev .96）：转接窗口里只能有 handoff_started 与
                      handoff_ended（AC-75）；计数经 dropped_in_handoff 交给 LiveCall，写进 handoff_ended 的
                      action.dropped_sonic_outputs。也不计入 counters.blocked
        ending        道别话术放行
    - 文字不到就一直扣住；扣住超过 guard.held_response_max_ms 也丢弃并记 guard_blocked（handoff 状态除外）。
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
        super().__init__(enable_direct_mode=True)
        self.stall_phrases = {self._normalize(p) for p in stall_phrases}
        self.limit, self.trace, self.call_id, self.task_id, self.clock = held_response_max_ms, trace, call_id, task_id, clock
        self._state = "ivr"
        self._counters = GuardCounters(blocked=0, released=0)
        self._dropped = 0
        self._responses = []
        self._current = None
        self._turns = {}
        self._approved = set()
        self._last_audio_ms = None
        self._tasks = set()
        self._injection_generation = 0

    def set_state(self, state: GuardState) -> None:
        """进入哪个状态由策略结果与 call_phase 决定（LiveActionBackend / LiveCall 调用）。"""
        if state not in ("ivr", "conversation", "injected", "handoff", "ending"):
            raise ValueError("unknown guard state")
        if state == "injected":
            self._injection_generation += 1
        if state != self._state and state in ("ivr", "injected", "handoff", "ending"):
            blocked = False
            for response in self._responses:
                if response["mode"] == "held" or (response is self._current and response["mode"] == "allowed"):
                    self._block(response, "state_changed")
                    blocked = True
            self._approved.clear()
            if blocked:
                task = asyncio.create_task(self.broadcast_interruption())
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        if state == "handoff" and self._state != "handoff":
            self._dropped = 0
        self._state = state

    @property
    def state(self) -> GuardState:
        return self._state

    def on_turn(self, turn: CompanyTurn) -> None:
        """LiveCall 在把 CompanyTurn 交给决策门之前调用：记下 seq → (t_start_ms, t_end_ms)，release/discard 按它配对。"""
        self._turns[turn.seq] = turn

    async def release(self, seq: int) -> bool:
        """放出为第 seq 轮（CompanyTurn.seq）扣住的回应；没有则返回 False。

        歧义表 A-15（定稿 by_company_speech_end）：回应按开始时刻归属。每段回应开始（LLMFullResponseStartFrame）
        时用 clock.monotonic_ms() 记下开始时刻——与 TurnDetector 产出 CompanyTurn 用同一个 deps.clock。
        开始时刻晚于第 seq 轮 t_end_ms（对方这句话结束）的第一段回应归第 seq 轮；开始时刻晚于下一轮
        t_start_ms 的回应归下一轮。所以回应即使早于本轮 CompanyTurn 产出（TurnDetector 还在等静音）也能配上；
        D 的测试要用 FakeClock 覆盖这种情形。"""
        await self._expire()
        self._approved.add(seq)
        found = False
        for response in self._responses:
            if response["mode"] == "held" and self._owner(response) == seq:
                found = True
                response["approved"] = True
                if response["text"] is not None:
                    await self._release(response)
        return found

    async def discard(self, seq: int, reason: str) -> int:
        """丢弃第 seq 轮扣住的回应并广播 interruption，返回丢弃的音频帧数。"""
        dropped = 0
        for response in self._responses:
            if response["mode"] == "held" and self._owner(response) == seq:
                dropped += len(response["audio"])
                self._block(response, reason)
        self._approved.discard(seq)
        await self.broadcast_interruption()
        return dropped

    @property
    def counters(self) -> GuardCounters:
        return self._counters

    @property
    def dropped_in_handoff(self) -> int:
        """最近一次进入 handoff 状态以来丢弃的 Sonic 回应段数（一段回应记一次；没有回应边界的零散音频每次也记一次）。
        set_state("handoff") 时清零；LiveCall 在 exit_handoff() 之后读它写 handoff_ended（D-G-4b）。"""
        return self._dropped

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        # todo(D): 按类说明实现；每次放行/丢弃写 guard_released / guard_blocked 事件——handoff 状态下例外，只累加
        #          dropped_in_handoff，不写 trace（D-G-4b）。
        await super().process_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return
        await self._expire()
        if isinstance(frame, LLMFullResponseStartFrame):
            self._new_response()
        elif isinstance(frame, LLMTextFrame):
            response = self._current or self._new_response()
            if response["text"] is None:
                response["text"] = frame.text
                if response["mode"] == "held" and (self._normalize(frame.text) in self.stall_phrases
                        or response["approved"] or self._owner(response) in self._approved):
                    await self._release(response)
        elif isinstance(frame, OutputAudioRawFrame):
            response = self._current
            if response is None:
                response = self._new_response()
                self._current = None
            if self._state == "ivr":
                return
            if self._state == "handoff":
                if not response["counted"]:
                    response["counted"] = True
                    self._dropped += 1
                return
            if response["mode"] == "allowed":
                self._last_audio_ms = self.clock.monotonic_ms()
                await self.push_frame(frame, direction)
            elif response["mode"] == "held":
                response["audio"].append(frame)
            return
        elif isinstance(frame, LLMFullResponseEndFrame):
            response = self._current
            self._current = None
            if (self._state == "injected" and response and response["source_state"] == "injected"
                    and response["injection_generation"] == self._injection_generation):
                self._state = "conversation"
        await self.push_frame(frame, direction)

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(text.casefold().split())

    def _new_response(self) -> dict:
        mode = "allowed" if self._state in ("injected", "ending") else "held" if self._state == "conversation" else "blocked"
        response = dict(start=self.clock.monotonic_ms(), text=None, audio=[], mode=mode, approved=False, counted=False,
            source_state=self._state, injection_generation=self._injection_generation)
        self._responses.append(response)
        self._current = response
        if mode == "held":
            task = asyncio.create_task(self._timeout(response))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if mode == "allowed":
            self._event("guard_released", "injected")
        elif mode == "blocked" and self._state != "handoff":
            self._event("guard_blocked", "ivr")
        return response

    def _owner(self, response: dict) -> int | None:
        turns = sorted(self._turns.values(), key=lambda turn: (turn.t_start_ms, turn.seq))
        for index, turn in enumerate(turns):
            upper = turns[index + 1].t_start_ms if index + 1 < len(turns) else float("inf")
            if turn.t_end_ms <= response["start"] < upper:
                return turn.seq
        return None

    def _event(self, kind: str, reason: str) -> None:
        if self._state == "handoff":
            return
        if kind == "guard_released":
            self._counters.released += 1
        else:
            self._counters.blocked += 1
        self.trace.event(TraceEvent(task_id=self.task_id, call_id=self.call_id, event_type=kind,
            t_ms=self.clock.monotonic_ms(), action={"reason": reason}))

    def _block(self, response: dict, reason: str) -> None:
        response["audio"].clear()
        response["mode"] = "blocked"
        self._event("guard_blocked", reason)

    async def _release(self, response: dict) -> None:
        response["mode"] = "allowed"
        self._event("guard_released", "text_or_decision")
        audio, response["audio"] = response["audio"], []
        for frame in audio:
            self._last_audio_ms = self.clock.monotonic_ms()
            await self.push_frame(frame)

    async def _expire(self) -> None:
        expired = False
        for response in self._responses:
            if response["mode"] == "held" and self.clock.monotonic_ms() - response["start"] >= self.limit:
                self._block(response, "timeout")
                expired = True
        if expired:
            await self.broadcast_interruption()

    async def _timeout(self, response: dict) -> None:
        remaining = response["start"] + self.limit - self.clock.monotonic_ms()
        await self.clock.sleep(max(0, remaining) / 1000)
        if response["mode"] == "held":
            self._block(response, "timeout")
            await self.broadcast_interruption()

    async def cleanup(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await super().cleanup()
