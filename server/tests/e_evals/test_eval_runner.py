"""AC-90：evals/menus/ 至少 15 个手写变体菜单；eval 加载这些菜单和 6 个 skill 的全部用例文件，输出 4 项指标（可见组，冻结）。
AC-93〔需密钥〕：`waitless eval --audio --scenario baseline` 在 live-sim 跑通，输出与文本级相同格式的指标。

第二组（10/16–10/18）。A-22 定稿：G 写 skills/<名>/evals/cases.jsonl，E 写 skills/<名>/evals/cases.extra.jsonl，
run_eval 读取每个 skill 的 cases*.jsonl 全部文件。
run_eval 端到端（replay 模式要 I-3 录好的回放，text-sim 要密钥）不在默认测试里，见测试计划的检查命令与第 5 节缺口。
"""

import json
import os
import re

import pytest
import yaml

from e_support import build_settings
from waitless import models as m

SKILL_NAMES = {
    "assisted-call",
    "find-official-number",
    "navigate-ivr",
    "decide-option",
    "handoff-to-user",
    "company-lakeshore-energy",
}
TRADEMARKS = re.compile("|".join(["cost" + "co", "bank of " + "america", r"\b" + "bo" + "a" + r"\b"]), re.IGNORECASE)  # AC-17 词表（拼接构造，避免商标检查自命中）
DIGIT_WORDS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
    "6": "six", "7": "seven", "8": "eight", "9": "nine", "*": "star", "#": "pound",
}


def _offers(text: str, digits: str) -> bool:
    if re.search(rf"(?<![0-9]){re.escape(digits)}(?![0-9])", text):
        return True
    words = " ".join(DIGIT_WORDS[c] for c in digits)
    return re.search(rf"\b{re.escape(words)}\b", text, re.IGNORECASE) is not None


def _strings(obj):
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in _strings(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in _strings(v)]
    return []


def _case_lines(skills_dir):
    out = []
    for path in sorted(skills_dir.glob("*/evals/cases*.jsonl")):
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                out.append((path.parts[-3], m.EvalCase.model_validate_json(raw)))
    return out


def test_ac90_menus_dir_has_at_least_15_handwritten_fictional_variants(menus_dir):
    from waitless.evals.runner import load_menus

    files = sorted(menus_dir.glob("*.yaml"))
    assert len(files) >= 15, f"evals/menus has {len(files)} menus; R34 asks for at least 15"
    on_disk = [m.MenuVariant.model_validate(yaml.safe_load(p.read_text(encoding="utf-8"))) for p in files]
    loaded = load_menus(menus_dir)
    assert sorted(v.menu_id for v in loaded) == sorted(v.menu_id for v in on_disk)
    assert {v.model_dump_json() for v in loaded} == {v.model_dump_json() for v in on_disk}
    assert len({v.menu_id for v in loaded}) == len(loaded), "menu_id must be unique"
    assert len({v.text.strip().lower() for v in loaded}) == len(loaded), "each variant has its own wording"
    for v in loaded:
        assert not TRADEMARKS.search(f"{v.goal} {v.text}"), f"{v.menu_id} uses a real brand"
        assert _offers(v.text, v.correct_digits), f"{v.menu_id}: correct_digits {v.correct_digits!r} is not offered in the text"


def test_ac90_load_menus_reads_every_yaml_file(tmp_path):
    from waitless.evals.runner import load_menus

    written = []
    for i in range(16):
        v = m.MenuVariant(
            menu_id=f"variant-{i:02d}",
            goal="Start electricity service",
            text=f"Thanks for calling Fernbrook Power, menu {i}. To start new service, press {i % 9 + 1}. For billing, press 0.",
            correct_digits=str(i % 9 + 1),
        )
        (tmp_path / f"variant-{i:02d}.yaml").write_text(yaml.safe_dump(v.model_dump()), encoding="utf-8")
        written.append(v)
    loaded = load_menus(tmp_path)
    assert sorted(loaded, key=lambda v: v.menu_id) == written


def test_ac90_load_cases_reads_every_line_of_all_six_skills(skills_dir):
    from waitless.evals.runner import load_cases

    expected = _case_lines(skills_dir)
    assert {skill for skill, _ in expected} == SKILL_NAMES, "every skill has at least one eval case"
    loaded = load_cases(skills_dir)
    assert sorted(c.model_dump_json() for c in loaded) == sorted(c.model_dump_json() for _, c in expected)


def test_ac90_load_cases_reads_every_line_from_each_cases_file(tmp_path):
    from waitless.evals.runner import load_cases

    def case(cid: str, skill: str) -> m.EvalCase:
        return m.EvalCase(case_id=cid, skill=skill, kind="round", input={"text": f"turn {cid}"}, expect={"intent": "open_answer"})

    a, b = tmp_path / "skill-a" / "evals", tmp_path / "skill-b" / "evals"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (tmp_path / "skill-c").mkdir()  # 没有 evals 目录的 skill
    (a / "cases.jsonl").write_text(case("a1", "skill-a").model_dump_json() + "\n\n" + case("a2", "skill-a").model_dump_json() + "\n", encoding="utf-8")
    (a / "cases.extra.jsonl").write_text(case("a3", "skill-a").model_dump_json() + "\n", encoding="utf-8")
    (b / "cases.jsonl").write_text(case("b1", "skill-b").model_dump_json(), encoding="utf-8")

    loaded = load_cases(tmp_path)
    assert sorted(c.case_id for c in loaded) == ["a1", "a2", "a3", "b1"]


def test_ac90_compute_metrics_reports_the_four_metrics():
    from waitless.evals.metrics import CaseRun, compute_metrics

    runs = [
        CaseRun(case_id="menu-1", kind="menu", menu_correct=True),
        CaseRun(case_id="menu-2", kind="menu", menu_correct=True),
        CaseRun(case_id="menu-3", kind="menu", menu_correct=True),
        CaseRun(case_id="menu-4", kind="menu", menu_correct=False),
        CaseRun(case_id="round-1", kind="round", auto_answered=True, auto_answer_correct=True),
        CaseRun(case_id="round-2", kind="round", auto_answered=True, auto_answer_correct=False),
        CaseRun(case_id="round-3", kind="round", auto_answered=True, auto_answer_correct=True),
        CaseRun(case_id="round-4", kind="round", auto_answered=True, auto_answer_correct=True),
        CaseRun(case_id="round-5", kind="round", auto_answered=False),  # 问了用户，不算自动作答
        CaseRun(case_id="baseline", kind="scenario", interrupts=1, completed=True),
        CaseRun(case_id="menu-reworded", kind="scenario", interrupts=1, completed=True),
        CaseRun(case_id="account-holder-required", kind="scenario", interrupts=2, completed=False),
    ]
    got = compute_metrics(runs)
    assert isinstance(got, m.EvalMetrics)
    assert got.menu_accuracy == pytest.approx(3 / 4)
    assert got.auto_answer_error_rate == pytest.approx(1 / 4)
    assert got.interrupts_per_call == pytest.approx(4 / 3)  # 每通电话（场景运行）平均
    assert got.interrupts_per_call_max == 2
    assert got.task_completion_rate == pytest.approx(2 / 3)


def _menu_variants(n: int = 15) -> list[m.MenuVariant]:
    out = []
    for i in range(n):
        right = str(i % 8 + 1)
        wrong = "9" if right != "9" else "8"
        out.append(
            m.MenuVariant(
                menu_id=f"fernbrook-{i:02d}",
                goal="Start electricity service",
                text=f"Fernbrook Power line {i}. To start new service, press {right}. Press {wrong} for billing questions.",
                correct_digits=right,
            )
        )
    return out


async def test_ac90_run_eval_loads_menus_and_all_cases_and_reports_four_metrics(tmp_path, mode_env, engine_config, skills_dir):
    """AC-90 原文后半句：eval 加载这些菜单和 6 个 skill 的全部 cases.jsonl，输出 4 项指标。

    run_eval 经 E-G-1 注入 FakeJev 与 FakeConverse（零密钥）。菜单用 tmp 里 15 个手写变体；skills 用仓库的 6 个 skill。
    从外面看：每个菜单的原文都作为一轮交给了 Jev（菜单确实被加载并逐个评测）；Jev 答对了全部菜单，所以 menu_accuracy=1；
    交给 Jev 的 call_round 轮数不少于「菜单数 + 全部 cases*.jsonl 里 round 类用例数」（每条用例都构造了一个决策请求）；
    返回值是带 4 项指标（外加最大打扰次数）的 EvalMetrics。"""
    from fakes import FakeConverse, FakeJev, jev_choice
    from e_support.integration import ScenarioOracle, read_scenario
    from waitless.evals.runner import run_eval

    menus_dir = tmp_path / "menus"
    menus_dir.mkdir()
    variants = _menu_variants()
    for v in variants:
        (menus_dir / f"{v.menu_id}.yaml").write_text(yaml.safe_dump(v.model_dump()), encoding="utf-8")
    by_text = {v.text: v.correct_digits for v in variants}
    menus_seen: set[str] = set()
    env = mode_env("text-sim", WAITLESS_DB_PATH=str(tmp_path / "eval.db"))
    settings = build_settings(env, engine_config)
    # 菜单之外：用例按它自己的 expect 作答（在用例 input 的文字里找到本轮原话）；baseline 那通电话按场景期望作答
    oracle = ScenarioOracle(read_scenario(settings.company_dir, "baseline"))
    case_expect: dict[str, m.EvalExpect] = {}
    for _, case in _case_lines(skills_dir):
        for text in _strings(case.input):
            if len(text) > 12:
                case_expect.setdefault(text.strip(), case.expect)

    def jev_answers(state, questions, point):
        last = state.transcript.lines[-1].text.strip() if state.transcript.lines else ""
        out = oracle.answer_jev(state, questions, point)
        if "menu_choice" in questions:
            crit = list(questions["menu_choice"].criteria or {})
            want = by_text.get(last)
            if want is not None:
                menus_seen.add(last)
            elif last in case_expect:
                want = case_expect[last].digits or case_expect[last].intent
            if want in crit:
                out["menu_choice"] = jev_choice(want, 0.95)
        return out

    jev = FakeJev(default=jev_answers)
    converse = FakeConverse(default=oracle.answer_converse)
    metrics = await run_eval(
        "text-sim", settings=settings, skills_dir=skills_dir, menus_dir=menus_dir,
        scenario="baseline", jev=jev, converse=converse,
    )

    assert isinstance(metrics, m.EvalMetrics)
    assert set(json.loads(metrics.model_dump_json())) == set(m.EvalMetrics.model_fields)
    assert menus_seen == set(by_text), f"menus never asked: {sorted(set(by_text) - menus_seen)}"
    assert metrics.menu_accuracy == pytest.approx(1.0)
    round_cases = [c for _, c in _case_lines(skills_dir) if c.kind == "round"]
    asked_rounds = [c for c in jev.calls if "menu_choice" in c.questions]
    assert len(asked_rounds) >= len(variants) + len(round_cases)


@pytest.mark.needs_keys
async def test_ac93_audio_eval_on_live_sim_reports_text_level_metrics(skills_dir, menus_dir):
    """〔需密钥〕I-3 经 scripts/load-keys-macos.sh 运行，公司进程与模拟运营商要先起来。缺密钥时 load_settings 抛
    ConfigError，测试失败（显式选中时不许跳过）。"""
    from waitless.config import load_settings
    from waitless.evals.runner import run_eval

    settings = load_settings(os.environ)
    assert settings.mode == "live-sim"
    metrics = await run_eval("live-sim", settings=settings, skills_dir=skills_dir, menus_dir=menus_dir, scenario="baseline", audio=True)
    assert isinstance(metrics, m.EvalMetrics)
    assert set(json.loads(metrics.model_dump_json())) == set(m.EvalMetrics.model_fields)
