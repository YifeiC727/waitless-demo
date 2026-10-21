"""待决选项登记：创建、作答、到期（G 线；tech §6.5 ask_user；AC-50、AC-86）。

公开接口（§7）：PendingRegistry.create()/answer()/expire_due()（实现 interfaces.PendingRegistry）。
"""

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from uuid import uuid4

from waitless.interfaces import Clock, ToolFailure
from waitless.models import Option, PendingChoice

__all__ = ["PendingRegistry"]


class PendingRegistry:
    def __init__(self, clock: Clock, *, ids: Callable[[], str] | None = None) -> None:
        self._clock, self._ids = clock, ids or (lambda: str(uuid4()))
        self._choices: dict[str, PendingChoice] = {}
        self._calls: dict[str, str] = {}
        self._decisions: dict[str, str] = {}

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
        if self.current(call_id) is not None:
            raise ValueError("this call already has an open pending choice")
        now = datetime.fromisoformat(self._clock.now_iso())
        choice = PendingChoice(choice_id=self._ids(), question_text=question_text, options=list(options),
                               created_at=now, expires_at=now + timedelta(seconds=timeout_s), status="open")
        if choice.choice_id in self._choices:
            raise ValueError("duplicate pending choice id")
        self._choices[choice.choice_id] = choice
        self._calls[choice.choice_id] = call_id
        self._decisions[choice.choice_id] = decision_id
        return choice.model_copy(deep=True)

    def get(self, choice_id: str) -> PendingChoice | None:
        choice = self._choices.get(choice_id)
        return choice.model_copy(deep=True) if choice is not None else None

    def current(self, call_id: str) -> PendingChoice | None:
        for choice_id, choice in self._choices.items():
            if self._calls[choice_id] == call_id and choice.status == "open":
                return choice.model_copy(deep=True)
        return None

    def answer(self, choice_id: str, option_id: str) -> PendingChoice:
        """未知 choice → ToolFailure("unknown_choice")；option 不在选项里 → ToolFailure("invalid_option")；
        已过期 → 返回 status=expired（AnswerChoiceOut.status=expired）。"""
        choice = self._choices.get(choice_id)
        if choice is None:
            raise ToolFailure("unknown_choice", "Unknown pending choice")
        if option_id not in {o.id for o in choice.options}:
            raise ToolFailure("invalid_option", "Option is not in this choice")
        if choice.status == "open":
            choice.status = "expired" if choice.expires_at <= datetime.fromisoformat(self._clock.now_iso()) else "answered"
        return choice.model_copy(deep=True)

    def expire_due(self) -> list[PendingChoice]:
        """把 expires_at 已过的 open 选项标为 expired 并返回；调用方走 safe_default（AC-50）。"""
        now = datetime.fromisoformat(self._clock.now_iso())
        expired = []
        for choice in self._choices.values():
            if choice.status == "open" and choice.expires_at <= now:
                choice.status = "expired"
                expired.append(choice.model_copy(deep=True))
        return expired

    def decision_of(self, choice_id: str) -> str | None:
        """创建该选项的 decision_id（后续注入沿用它，trace check 才找得到对应 decision）。"""
        return self._decisions.get(choice_id)
