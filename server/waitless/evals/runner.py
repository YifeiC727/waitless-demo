"""文本级与音频级评测的运行器（E 线；tech §6.12；AC-90、AC-93）。

公开接口（§7）：run_eval(mode, ...) -> Metrics（models.EvalMetrics）。
"""

from collections.abc import Sequence
from pathlib import Path

from waitless.config import Settings
from waitless.interfaces import ConverseClient, JevClient
from waitless.models import EvalCase, EvalMetrics, MenuVariant

__all__ = ["load_cases", "load_menus", "run_eval"]


async def run_eval(
    mode: str,
    *,
    settings: Settings,
    skills_dir: Path,
    menus_dir: Path,
    cases: Sequence[EvalCase] | None = None,
    scenario: str | None = None,
    audio: bool = False,
    record_dir: Path | None = None,
    jev: JevClient | None = None,
    converse: ConverseClient | None = None,
) -> EvalMetrics:
    """加载 6 个 skill 的全部 cases*.jsonl 与 evals/menus 的手写菜单，跑一遍，输出 4 项指标（AC-90）。

    缺口 E-G-1（定稿 inject_kwargs）：jev、converse 给出时代替在线客户端（默认测试传 FakeJev、FakeConverse）；
    cli 不传，行为不变。整条链经 app.build_services(settings, jev=, converse=, clock=) 组装（缺口 E-G-3）。"""
    # todo(E): mode=replay 用 ReplayPlayer；text-sim 用真实 Jev/Lite（需密钥，不进默认测试）。
    # todo(E): audio=True 或 mode=live-sim：每通电话前 PUT /sim/scenario（call.live.CarrierClient），
    #          跑 live-sim，复用 compute_metrics（AC-93，〔需密钥〕）。
    # todo(E): record_dir 给出时用 ReplayRecorder 录制回放（I-1）；文件头写本通的 scenario_id（A-11）、request_text（X-G3）、
    #          models.planner（D14-1）；文件名 <scenario_id>.jsonl，同名已存在就报错（X-G4）；拿到 call_id 后 recorder.bind（E-G-8）。
    # todo(E): text-sim 每个场景各跑一遍：CallManager.start(..., scenario_id=场景) 指定场景（A-11）。
    raise NotImplementedError


def load_cases(skills_dir: Path) -> list[EvalCase]:
    """skills/*/evals/cases*.jsonl 的全部行；每行按 EvalCase 校验。

    歧义表 A-22（定稿 separate_files，暂定）：G 写 cases.jsonl，E 写同目录的 cases.extra.jsonl；
    两种文件都读（按文件名排序，同一文件按行序）。AC-39 仍只检查 cases.jsonl。"""
    raise NotImplementedError


def load_menus(menus_dir: Path) -> list[MenuVariant]:
    """evals/menus/*.yaml，至少 15 个（R34）。"""
    raise NotImplementedError
