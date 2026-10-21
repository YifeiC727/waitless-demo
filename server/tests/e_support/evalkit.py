"""I-3b 评测、录制、回放、对比测试的共用设施（第 3 步写，冻结；施工线不改）。

FakeJev / FakeConverse 按三个场景文件的台词作答（integration.ScenarioOracle），菜单与用例的答案按测试给定；
run_eval 经 E-G-1 注入这两个替身（录制时 run_eval 用 ReplayRecorder 包住它们；回放时不注入，run_eval 自建 ReplayPlayer）。
数据库只用每个测试自己的 tmp 目录（WAITLESS_DB_PATH），读 calls、turns 两张表（tech §6.8 的列）。
"""

import asyncio
import json
import sqlite3
import subprocess
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

import yaml

from waitless import models as m

from e_support.integration import COMPANY_ID, SCENARIO_IDS, ScenarioOracle, read_scenario
from e_support.stubs import REQUEST_TEXT, build_settings

GUARD_S = 180
GIT = ["git", "-c", "user.name=Waitless Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]
RELAY_PREFIX = "The account holder chose"  # say-templates.yaml 的 pending.answer 开头（G 线）


def fast_engine(engine_config: Any, grace_ms: int = 300, poll_ms: int = 50) -> Any:
    """只改测试用的 EngineConfig 副本：轮询 poll_ms、断线宽限 grace_ms（不改 engine.yaml）。"""
    return engine_config.model_copy(update={
        "web": engine_config.web.model_copy(update={"poll_ms": poll_ms}),
        "handoff": engine_config.handoff.model_copy(update={"resume_grace_ms": grace_ms}),
    })


class MultiOracle:
    """三个场景合在一起：按 Jev state 最后一句找到所属场景，再按该场景的 expect 作答。

    只装盘上已有的场景文件（B 第三批合入之前只有 baseline），所以只用 baseline 的测试在 E 的分支上就能跑；
    用到另两个场景的测试仍会因场景文件不存在而失败。"""

    def __init__(self, company_dir: Path, scenario_ids: Iterable[str] = SCENARIO_IDS) -> None:
        folder = Path(company_dir) / COMPANY_ID / "scenarios"
        self.oracles = {sid: ScenarioOracle(read_scenario(company_dir, sid)) for sid in scenario_ids if (folder / f"{sid}.yaml").is_file()}

    def _for_text(self, text: str) -> ScenarioOracle:
        text = text.strip()
        if text:
            for oracle in self.oracles.values():
                if oracle.line_for(text) is not None:
                    return oracle
        return self.oracles["baseline"]

    def answer_jev(self, state: m.JevState, questions: Mapping[str, m.JevQuestion], point: str | None) -> dict[str, Any]:
        last = state.transcript.lines[-1].text if state.transcript.lines else ""
        return self._for_text(last).answer_jev(state, questions, point)

    def answer_converse(self, call: m.ConverseCall) -> dict[str, Any]:
        if call.task == "extract_offer_options" and "menu-reworded" in self.oracles:
            return self.oracles["menu-reworded"].answer_converse(call)
        return self.oracles["baseline"].answer_converse(call)


def write_menus(root: Path, wrong: Iterable[int] = (), count: int = 15) -> tuple[Path, dict[str, str]]:
    """count 个手写变体菜单写进 root；返回 (目录, {菜单原文: Jev 要按的键})，wrong 里的序号故意答错。"""
    wrong = set(wrong)
    root.mkdir(parents=True, exist_ok=True)
    answers: dict[str, str] = {}
    for i in range(count):
        right = str(i % 8 + 1)
        other = "9" if right != "9" else "8"
        v = m.MenuVariant(menu_id=f"fernbrook-{i:02d}", goal="Start electricity service",
                          text=f"Fernbrook Power line {i}. To start new service, press {right}. Press {other} for billing questions.",
                          correct_digits=right)
        (root / f"{v.menu_id}.yaml").write_text(yaml.safe_dump(v.model_dump()), encoding="utf-8")
        answers[v.text] = other if i in wrong else right
    return root, answers


def with_answers(base: Callable[..., dict[str, Any]], by_text: Mapping[str, str] = (), by_question: Mapping[str, str] = ()) -> Callable[..., dict[str, Any]]:
    """在 base 的答案上改两种题：转写最后一句等于 by_text 的键时，menu_choice 选给定候选；
    问题 id 在 by_question 里时，该题选给定候选（例如 assistant_next_action 的 next_action）。"""
    from fakes import jev_choice

    by_text, by_question = dict(by_text), dict(by_question)

    def answer(state: m.JevState, questions: Mapping[str, m.JevQuestion], point: str | None) -> dict[str, Any]:
        last = state.transcript.lines[-1].text.strip() if state.transcript.lines else ""
        out = base(state, questions, point)
        if "menu_choice" in questions and last in by_text:
            crit = list(questions["menu_choice"].criteria or {})
            if by_text[last] in crit:
                out["menu_choice"] = jev_choice(by_text[last], 0.95)
        for qid, want in by_question.items():
            if qid in questions and want in list(questions[qid].criteria or {}):
                out[qid] = jev_choice(want, 0.95)
        return out

    return answer


def scenario_case(scenario_id: str, request: str = REQUEST_TEXT) -> m.EvalCase:
    """场景电话的用例行（kind=scenario），请求原话在 input.request（与 E 写的 cases.extra.jsonl 同格式）。"""
    return m.EvalCase(case_id=f"scenario-{scenario_id}", skill=COMPANY_ID, kind="scenario",
                      input={"scenario_id": scenario_id, "request": request}, expect={"outcome": "confirmed"})


def round_case(case_id: str, text: str, expect: Mapping[str, Any], *, skill: str = "decide-option", phase: str = "live_rep") -> m.EvalCase:
    return m.EvalCase(case_id=case_id, skill=skill, kind="round",
                      input={"goal": "Start electric service", "text": text, "kind": "speech", "phase": phase}, expect=dict(expect))


def text_settings(mode_env: Callable[..., dict[str, str]], engine: Any, root: Path, **env: str) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    return build_settings(mode_env("text-sim", WAITLESS_DB_PATH=str(root / "eval.db"), **env), engine)


def replay_settings(mode_env: Callable[..., dict[str, str]], engine: Any, root: Path, replay_dir: Path, replay_file: str, **env: str) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    s = build_settings(mode_env("replay", WAITLESS_DB_PATH=str(root / "eval.db"), WAITLESS_REPLAY_DIR=str(replay_dir), **env), engine)
    return s.model_copy(update={"replay_file": replay_file})


def calls_rows(db_path: Path) -> dict[str, tuple[str | None, int]]:
    """calls 表：{scenario_id: (outcome, interrupts_used)}。"""
    with closing(sqlite3.connect(db_path)) as con:
        return {sid: (outcome, used) for sid, outcome, used in con.execute("select scenario_id, outcome, interrupts_used from calls")}


def call_events(db_path: Path, scenario_id: str) -> list[tuple[str, int, Any, str | None]]:
    """某个场景那通电话的 turns：[(event_type, t_ms, action, transcript)]，按 seq。"""
    with closing(sqlite3.connect(db_path)) as con:
        (call_id,) = con.execute("select call_id from calls where scenario_id = ?", (scenario_id,)).fetchone()
        rows = con.execute("select event_type, t_ms, action_json, transcript from turns where call_id = ? order by seq", (call_id,)).fetchall()
    return [(et, t, json.loads(a) if a else None, tx) for et, t, a, tx in rows]


def utterances(db_path: Path, scenario_id: str) -> list[str]:
    return [tx or "" for et, _, _, tx in call_events(db_path, scenario_id) if et == "waitless_utterance"]


def all_ids(db_path: Path) -> list[str]:
    with closing(sqlite3.connect(db_path)) as con:
        return [r[0] for r in con.execute("select call_id from calls")] + [r[0] for r in con.execute("select task_id from tasks")]


async def eval_with(jev_answer: Callable[..., dict[str, Any]], converse_answer: Callable[..., dict[str, Any]], **kwargs: Any) -> m.EvalMetrics:
    """run_eval(jev=FakeJev, converse=FakeConverse, **kwargs)，有总时限（不许挂住）。"""
    from fakes import FakeConverse, FakeJev
    from waitless.evals.runner import run_eval

    return await asyncio.wait_for(
        run_eval(jev=FakeJev(default=jev_answer), converse=FakeConverse(default=converse_answer), **kwargs), timeout=GUARD_S
    )


async def replay_eval(settings: Any, **kwargs: Any) -> m.EvalMetrics:
    """replay 模式的 run_eval：不注入任何客户端（run_eval 按 settings.replay_dir / replay_file 自建 ReplayPlayer）。"""
    from waitless.evals.runner import run_eval

    return await asyncio.wait_for(run_eval("replay", settings=settings, **kwargs), timeout=GUARD_S)


def git(repo: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def make_skills_repo(tmp_path: Path, skills_dir: Path) -> Path:
    """临时 git 仓库：skills/ 是仓库 skills 的副本；v1、v2 两个标签，v2 只多一个标记文件（skills 行为不变）。"""
    import shutil

    repo = tmp_path / "repo"
    shutil.copytree(skills_dir, repo / "skills")
    (repo / "replays").mkdir()
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "v1")
    git(repo, "tag", "v1")
    (repo / "skills" / "decide-option" / "eval-marker.txt").write_text("v2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "v2")
    git(repo, "tag", "v2")
    return repo


def md_table(md: str) -> list[list[str]]:
    """compare.md 里的表格行（不含分隔行），每行切成去掉首尾空白的单元格。"""
    rows = []
    for ln in md.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):
            continue
        rows.append(cells)
    return rows


__all__ = [
    "GUARD_S",
    "MultiOracle",
    "RELAY_PREFIX",
    "all_ids",
    "call_events",
    "calls_rows",
    "eval_with",
    "fast_engine",
    "git",
    "make_skills_repo",
    "md_table",
    "replay_eval",
    "replay_settings",
    "round_case",
    "scenario_case",
    "text_settings",
    "utterances",
    "with_answers",
    "write_menus",
]
