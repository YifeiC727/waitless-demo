"""新旧两版 skill 的指标对比（E 线；tech §6.12；AC-91、AC-92；R35、R36）。

公开接口（§7）：compare_refs(base, head) -> CompareReport。
`waitless eval compare --base-ref A --head-ref B`：把两个 ref 的 skills/ 导出到临时目录，各跑一遍，
写 evals/results/compare.json 与 compare.md；任一指标倒退时退出码 1。临时目录用完删除。
"""

import io
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel

from waitless.config import Settings
from waitless.models import EvalMetrics
from waitless.interfaces import ReplayMismatch
from waitless.evals.runner import run_eval

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
    refs = (base_ref, head_ref)
    for ref in refs:
        if ref not in replays or not Path(replays[ref]).is_file():
            raise ReplayMismatch("file", 0, detail=ref)
    directories = []
    try:
        exports = []
        for ref in refs:
            directory = Path(tempfile.mkdtemp(prefix="waitless-e-evals-compare-"))
            directories.append(directory)
            archive = subprocess.run(["git", "-C", str(repo_root), "archive", "--format=tar", "--", ref, "skills"],
                                     capture_output=True)
            if archive.returncode:
                raise ValueError(f"Cannot export skills for ref: {ref}")
            with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as contents:
                contents.extractall(directory, filter="data")
            exports.append(directory / "skills")
        metrics = []
        for ref, skills in zip(refs, exports):
            replay = Path(replays[ref]).resolve()
            run_settings = settings.model_copy(update={"mode": "replay", "skills_dir": skills,
                "replay_dir": replay.parent, "replay_file": replay.name, "record_dir": None})
            metrics.append(await run_eval("replay", settings=run_settings, skills_dir=skills,
                                          menus_dir=repo_root / "evals" / "menus"))
        report = CompareReport(base_ref=base_ref, head_ref=head_ref, base=metrics[0], head=metrics[1],
                               regressions=regressions(metrics[0], metrics[1]))
    finally:
        for directory in directories:
            shutil.rmtree(directory)
    write_report(report, out_dir)
    return report


def regressions(base: EvalMetrics, head: EvalMetrics) -> list[str]:
    """按 METRIC_DIRECTIONS 逐项比较，返回倒退的指标名（AC-92）。"""
    return [name for name, direction in METRIC_DIRECTIONS.items()
            if (getattr(head, name) < getattr(base, name) if direction == "higher"
                else getattr(head, name) > getattr(base, name))]


def write_report(report: CompareReport, out_dir: Path) -> tuple[Path, Path]:
    """写 compare.json 与 compare.md（供 Yifei 放进视频和提交材料，R35）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / "compare.json", out_dir / "compare.md"
    lines = [f"# Waitless eval compare: {report.base_ref} vs {report.head_ref}", "",
             f"| Metric | {report.base_ref} | {report.head_ref} | Better when | Regressed |",
             "|---|---|---|---|---|"]
    for name, direction in METRIC_DIRECTIONS.items():
        regressed = "yes" if name in report.regressions else "no"
        lines.append(f"| {name} | {getattr(report.base, name):.3f} | {getattr(report.head, name):.3f} | {direction} | {regressed} |")
    lines.extend([
        f"| interrupts_per_call_max | {report.base.interrupts_per_call_max} | {report.head.interrupts_per_call_max} | lower | informational |",
        "", "Regressions: " + (", ".join(report.regressions) or "none"),
        "Note: a metric whose denominator is 0 is reported as 0.0 (max as 0).",
    ])
    json_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path
