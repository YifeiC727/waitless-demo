"""把对方的话切成 CompanyTurn，菜单要等说完（D 线；tech §6.5、S0 V1、S0-r2 V12；AC-66；K2）。

公开接口（§7）：TurnDetector（FrameProcessor）。
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from waitless.audio.vad import EnergyVad
from waitless.interfaces import Clock
from waitless.models import CompanyTurn, EngineTurns, ProviderTextEvent

__all__ = ["TurnDetector"]


class TurnDetector(FrameProcessor):
    """音频帧照常向下游传；只观察能量判断对方在说还是静音。文字来自 SonicTap 的 provider 事件。
    menu_patterns、digit_words 由 LiveCall 从 CompiledCall.press_patterns、CompiledCall.digit_words 传入（缺口 E-G-4），
    判菜单用 parse_menu(text, menu_patterns, digit_words=digit_words or None)（G-G-03）。
    缺口 D-G-6（定稿 document_patch_points）——测试替换点的约定：在调用时经 waitless.decision.menu_parse 模块属性
    （`menu_parse.parse_menu(...)`）或本模块顶层同名导入 `parse_menu` 按名字调用；不要在导入时把它存进别的别名、
    默认参数或闭包里，测试会同时替换 menu_parse.parse_menu 与本模块的 parse_menu。"""

    def __init__(
        self,
        *,
        call_id: str,
        vad: EnergyVad,
        turns: EngineTurns,
        menu_patterns: Sequence[str],
        digit_words: Mapping[str, str] | None = None,
        on_turn: Callable[[CompanyTurn], Awaitable[None]],
        clock: Clock,
    ) -> None:
        raise NotImplementedError

    async def on_provider_text(self, ev: ProviderTextEvent) -> None:
        """只收 role=USER、stage=FINAL；stop_reason 为 PARTIAL_TURN 的块同样算 FINAL（菜单最后一块常是它，S0-r2 V12）。
        同一轮的多块 FINAL 按到达顺序拼接成 CompanyTurn.text。转接期间不调用（LiveCall 不转发）。"""
        raise NotImplementedError

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        # todo(D): InputAudioRawFrame → EnergyVad 判有声/静音并记时间，然后原样 push。
        # todo(D): 收到 USER FINAL 后，静音满 turns.turn_end_silence_ms 产出 kind=speech 的 CompanyTurn；
        #          文字能被 decision.menu_parse.parse_menu 解析出选项时改为 kind=menu，并要静音满
        #          turns.menu_end_silence_ms，且最后一块 FINAL 之后再等 turns.transcript_grace_ms（600 ms，
        #          provider USER FINAL 的 p95 575 ms）。菜单结束前绝不产出（S0 V1 抢按，AC-66）。
        # todo(D): 每个 CompanyTurn 交 on_turn；company_turn 事件由 LiveCall 的 on_turn 回调写（缺口 D-G-3，定稿
        #          livecall_writes），TurnDetector 自己不写 trace，构造参数也不带 trace。
        # todo(D): t_start_ms、t_end_ms 用 clock.monotonic_ms()（与 OutputGuard 同一个 deps.clock，A-15）。
        raise NotImplementedError
