"""待决选项登记：创建、作答、到期（G 线；tech §6.5 ask_user；AC-50、AC-86）。

公开接口（§7）：PendingRegistry.create()/answer()/expire_due()（实现 interfaces.PendingRegistry）。
"""

from collections.abc import Callable, Sequence

from waitless.interfaces import Clock
from waitless.models import Option, PendingChoice

__all__ = ["PendingRegistry"]


class PendingRegistry:
    def __init__(self, clock: Clock, *, ids: Callable[[], str] | None = None) -> None:
        raise NotImplementedError

    def create(
        self,
        *,
        call_id: str,
        decision_id: str,
        question_text: str,
        options: Sequence[Option],
        timeout_s: float,
    ) -> PendingChoice:
        """options 1–3 个（超过 3 个由调用方先截断；policy ask_user.max_options=3）；
        expires_at = 现在 + timeout_s（policy ask_user.timeout_sec=20）。同一通电话同时只有一个 open。"""
        raise NotImplementedError

    def get(self, choice_id: str) -> PendingChoice | None:
        raise NotImplementedError

    def current(self, call_id: str) -> PendingChoice | None:
        raise NotImplementedError

    def answer(self, choice_id: str, option_id: str) -> PendingChoice:
        """未知 choice → ToolFailure("unknown_choice")；option 不在选项里 → ToolFailure("invalid_option")；
        已过期 → 返回 status=expired（AnswerChoiceOut.status=expired）。"""
        raise NotImplementedError

    def expire_due(self) -> list[PendingChoice]:
        """把 expires_at 已过的 open 选项标为 expired 并返回；调用方走 safe_default（AC-50）。"""
        raise NotImplementedError

    def decision_of(self, choice_id: str) -> str | None:
        """创建该选项的 decision_id（后续注入沿用它，trace check 才找得到对应 decision）。"""
        raise NotImplementedError
