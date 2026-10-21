"""评测指标（E 线；tech §6.12；AC-90）。

公开接口（§7）：compute_metrics()。
  menu_accuracy            菜单选对率
  auto_answer_error_rate   自动作答中选错的比例
  interrupts_per_call      每通平均打扰次数（同时报最大值 interrupts_per_call_max）
  task_completion_rate     任务完成率
"""

from collections.abc import Sequence

from pydantic import BaseModel

from waitless.models import EvalMetrics

__all__ = ["CaseRun", "compute_metrics"]


class CaseRun(BaseModel):
    """一个用例或一通场景电话的结果（E 线内部，可扩展）。"""

    case_id: str
    kind: str
    menu_correct: bool | None = None
    auto_answered: bool = False
    auto_answer_correct: bool | None = None
    interrupts: int = 0
    completed: bool | None = None


def compute_metrics(runs: Sequence[CaseRun]) -> EvalMetrics:
    """分母为 0 的指标如何取值写进 compare.md 的说明（E 线定）。"""
    raise NotImplementedError
