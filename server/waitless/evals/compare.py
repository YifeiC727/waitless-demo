"""新旧两版 skill 的指标对比（E 线；tech §6.12；AC-91、AC-92；R35、R36）。

公开接口（§7）：compare_refs(base, head) -> CompareReport。
`waitless eval compare --base-ref A --head-ref B`：把两个 ref 的 skills/ 导出到临时目录，各跑一遍，
写 evals/results/compare.json 与 compare.md；任一指标倒退时退出码 1。临时目录用完删除。
"""

from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel

from waitless.config import Settings
from waitless.models import EvalMetrics

__all__ = ["METRIC_DIRECTIONS", "CompareReport", "compare_refs", "regressions", "write_report"]

# 指标 → 更好的方向
METRIC_DIRECTIONS: dict[str, str] = {
    "menu_accuracy": "higher",
    "auto_answer_error_rate": "lower",
    "interrupts_per_call": "lower",
    "task_completion_rate": "higher",
}


class CompareReport(BaseModel):
    base_ref: str
    head_ref: str
    base: EvalMetrics
    head: EvalMetrics
    regressions: list[str]


async def compare_refs(
    base_ref: str,
    head_ref: str,
    *,
    repo_root: Path,
    out_dir: Path,
    settings: Settings,
    replays: Mapping[str, Path],
) -> CompareReport:
    """缺口 E-G-2（定稿 replay_per_ref）：每个版本各用自己的回放跑。replays 是 ref → 回放文件（I-3 用 text-sim 对
    每个版本各录一份，如 replays/eval-<ref>.jsonl，头里记该版 skill 的 commit）；缺哪个 ref 的回放就报错
    （异常的 channel、seq 取法同 E 线对缺文件的约定，E-G-7 由 E 自定），不改走在线调用（C14）。settings 由 cli 传入（mode=replay 的配置）。"""
    # todo(E): `git archive <ref> skills/` 只读导出到自建临时目录（不 checkout、不改工作区），各用 replays[ref] 以
    #          run_eval(mode="replay", ...) 跑；交回前删除临时目录（AC-91）。
    raise NotImplementedError


def regressions(base: EvalMetrics, head: EvalMetrics) -> list[str]:
    """按 METRIC_DIRECTIONS 逐项比较，返回倒退的指标名（AC-92）。"""
    raise NotImplementedError


def write_report(report: CompareReport, out_dir: Path) -> tuple[Path, Path]:
    """写 compare.json 与 compare.md（供 Yifei 放进视频和提交材料，R35）。"""
    raise NotImplementedError
