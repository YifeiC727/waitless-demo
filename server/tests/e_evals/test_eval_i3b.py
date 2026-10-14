"""I-3b 补测 · run_eval 的场景电话与计分（第 3 步可见组，冻结）：AC-85、AC-86、AC-90（test-gaps G01、G02、G12、G20、G23）。

按 I3B 裁决（RUN/lanes/i3b/i3b-decisions.md）：
- D8：task_completion_rate 的「完成」= 这通的 outcome 等于该场景 expected.outcome，且 interrupts_used 等于 expected.interrupts。
- D1：模拟用户的做法取场景 YAML 的 live_rep 步 simulated_user（choose 选项子串、handoff_exit 断开或点 Return）。
- D9：assistant、planning 用例各构造对应决策点的请求交给 DecisionGate.decide，按 round 计入 auto_answer_error_rate。
- D12：请求原话取 kind=scenario 用例行的 input.request；评测用测试自己的 WAITLESS_DB_PATH。

run_eval 经 E-G-1 注入 FakeJev 与 FakeConverse（零密钥零网络），从外面看：calls、turns 两张表与返回的 EvalMetrics。
"""

import pytest

from e_support.evalkit import (
    MultiOracle,
    RELAY_PREFIX,
    call_events,
    calls_rows,
    eval_with,
    fast_engine,
    round_case,
    scenario_case,
    text_settings,
    utterances,
    with_answers,
    write_menus,
)
from e_support.integration import SCENARIO_IDS, read_scenario
from waitless import models as m

WAIT_LINES = [
    "Please hold while I pull up the details for you.",
    "Thanks for your patience, I am still looking into this.",
    "Bear with me for a few seconds while the system loads.",
    "I am reviewing the request now, please stay on the line.",
]


def all_scenario_cases() -> list[m.EvalCase]:
    return [scenario_case(sid) for sid in SCENARIO_IDS]


async def test_ac85_i3b_run_eval_runs_each_scenario_and_matches_expected(tmp_path, mode_env, engine_config, company_dir, skills_dir):
    """tech §7.1 AC-85：run_eval(text-sim) 为每个场景建一通电话，读 calls.outcome、interrupts_used 与 expected 比较。"""
    oracle = MultiOracle(company_dir)
    menus, _ = write_menus(tmp_path / "menus")
    settings = text_settings(mode_env, fast_engine(engine_config), tmp_path / "all")
    metrics = await eval_with(oracle.answer_jev, oracle.answer_converse, mode="text-sim", settings=settings,
                              skills_dir=skills_dir, menus_dir=menus, cases=all_scenario_cases())
    rows = calls_rows(settings.db_path)
    assert sorted(rows) == sorted(SCENARIO_IDS), "one call per scenario, each with its own scenario_id"
    expected = {sid: read_scenario(company_dir, sid).expected for sid in SCENARIO_IDS}
    for sid, exp in expected.items():
        assert rows[sid] == (exp.outcome, exp.interrupts), sid
    ints = [e.interrupts for e in expected.values()]
    assert metrics.task_completion_rate == pytest.approx(1.0)
    assert metrics.interrupts_per_call == pytest.approx(sum(ints) / len(ints))
    assert metrics.interrupts_per_call_max == max(ints)


async def test_ac85_i3b_run_eval_counts_a_scenario_that_misses_its_expected_outcome(tmp_path, mode_env, engine_config, company_dir, skills_dir):
    """menu-reworded 挂断后 DP-E1 被答成 needs_follow_up：那一行 outcome 变了，完成率只剩 2/3。"""
    from fakes import jev_choice

    oracle = MultiOracle(company_dir)
    rew = read_scenario(company_dir, "menu-reworded")
    confirm = next(ln.say for st in rew.steps.values() for ln in (st.text_script or []) if ln.expect.intent == "confirm_and_end")
    marker = confirm.split("{")[0].strip()[:40]

    def jev(state, questions, point):
        out = oracle.answer_jev(state, questions, point)
        q = questions.get("outcome")
        if q is not None and "needs_follow_up" in (q.criteria or {}) and any(marker in ln.text for ln in state.transcript.lines):
            out["outcome"] = jev_choice("needs_follow_up", 0.95)
        return out

    menus, _ = write_menus(tmp_path / "menus")
    settings = text_settings(mode_env, fast_engine(engine_config), tmp_path / "miss")
    metrics = await eval_with(jev, oracle.answer_converse, mode="text-sim", settings=settings,
                              skills_dir=skills_dir, menus_dir=menus, cases=all_scenario_cases())
    rows = calls_rows(settings.db_path)
    assert rows["menu-reworded"][0] == "needs_follow_up"
    assert rows["baseline"][0] == "confirmed"
    assert metrics.task_completion_rate == pytest.approx(2 / 3)


async def test_ac85_i3b_run_eval_user_disconnects_in_account_holder_required(tmp_path, mode_env, engine_config, company_dir, skills_dir):
    """simulated_user.handoff_exit=disconnect：run_eval 的模拟用户直接断开，转接满 resume_grace_ms 才接回；baseline 的用户立刻点 Return。"""
    oracle = MultiOracle(company_dir)
    menus, _ = write_menus(tmp_path / "menus")
    grace = 1500
    gaps = {}
    for sid in ("baseline", "account-holder-required"):
        settings = text_settings(mode_env, fast_engine(engine_config, grace_ms=grace), tmp_path / sid)
        await eval_with(oracle.answer_jev, oracle.answer_converse, mode="text-sim", settings=settings, skills_dir=skills_dir,
                        menus_dir=menus, cases=[scenario_case(sid)], scenario=sid)
        assert calls_rows(settings.db_path) == {sid: ("confirmed", 1)}
        events = call_events(settings.db_path, sid)
        t = {et: ms for et, ms, _, _ in events if et in ("handoff_started", "handoff_ended")}
        gaps[sid] = t["handoff_ended"] - t["handoff_started"]
        kinds = [et for et, _, _, _ in events]
        after = [a for et, _, a, _ in events[kinds.index("handoff_ended"):] if et == "injection_sent"]
        assert after and after[0].get("template") == "handoff.resume"
    assert gaps["baseline"] < grace, "the simulated user clicks Return at once in baseline"
    assert gaps["account-holder-required"] >= grace, "after a disconnect the call resumes only after resume_grace_ms"


async def test_ac86_i3b_run_eval_simulated_user_picks_the_option_named_in_the_scenario(tmp_path, mode_env, engine_config, company_dir, skills_dir):
    """menu-reworded 的 simulated_user.choose 只命中「不加钱」那一项（不是第一项）：坐席听到的是这一项。"""
    oracle = MultiOracle(company_dir)
    menus, _ = write_menus(tmp_path / "menus")
    settings = text_settings(mode_env, fast_engine(engine_config), tmp_path / "choose")
    metrics = await eval_with(oracle.answer_jev, oracle.answer_converse, mode="text-sim", settings=settings, skills_dir=skills_dir,
                              menus_dir=menus, cases=[scenario_case("menu-reworded")], scenario="menu-reworded")
    rep = next(st for st in read_scenario(company_dir, "menu-reworded").steps.values() if st.kind == "live_rep")
    choose = (getattr(rep, "simulated_user", None) or {})["choose"]
    relays = [u for u in utterances(settings.db_path, "menu-reworded") if u.startswith(RELAY_PREFIX)]
    assert len(relays) == 1, relays
    assert choose.lower() in relays[0].lower()
    assert "$" not in relays[0], "the first option (the paid one) was not chosen"
    assert calls_rows(settings.db_path) == {"menu-reworded": ("confirmed", 1)}
    assert metrics.task_completion_rate == pytest.approx(1.0) and metrics.interrupts_per_call == pytest.approx(1.0)


async def test_ac90_i3b_run_eval_scores_wrong_menu_digits_and_wrong_auto_answers(tmp_path, mode_env, engine_config, company_dir, skills_dir):
    """tech §7.1 AC-90：每条构造决策请求、把结果与 expect 比较。15 个菜单答错 3 个；4 条 round 用例答错 2 条、
    1 条 assistant 用例答错（D9：按 round 计）；场景电话不进这两项分母。"""
    oracle = MultiOracle(company_dir)
    menus, answers = write_menus(tmp_path / "menus", wrong={0, 1, 2})
    rounds = [round_case(f"wait-{i}", text, {"intent": "wait"}) for i, text in enumerate(WAIT_LINES)]
    assistant = m.EvalCase(case_id="assistant-start", skill="assisted-call", kind="assistant",
                           input={"text": "Please help start electric service with Lakeshore Energy.", "candidates": ["prepare_task"]},
                           expect={"intent": "prepare_task"})
    by_text = {**answers, WAIT_LINES[0]: "open_answer", WAIT_LINES[1]: "wait", WAIT_LINES[2]: "open_answer", WAIT_LINES[3]: "wait"}
    jev = with_answers(oracle.answer_jev, by_text=by_text, by_question={"next_action": "reply_only"})
    settings = text_settings(mode_env, fast_engine(engine_config), tmp_path / "score")
    metrics = await eval_with(jev, oracle.answer_converse, mode="text-sim", settings=settings, skills_dir=skills_dir, menus_dir=menus,
                              cases=[*rounds, assistant, scenario_case("baseline")], scenario="baseline")
    assert metrics.menu_accuracy == pytest.approx(12 / 15)
    assert metrics.auto_answer_error_rate == pytest.approx(3 / 5)
    assert metrics.task_completion_rate == pytest.approx(1.0)


async def test_ac90_i3b_run_eval_all_right_scores_zero_errors(tmp_path, mode_env, engine_config, company_dir, skills_dir):
    """同一组用例全部答对：auto_answer_error_rate=0、menu_accuracy=1（对照上一条，证明分数来自比较而不是常数）。"""
    oracle = MultiOracle(company_dir)
    menus, answers = write_menus(tmp_path / "menus")
    rounds = [round_case(f"wait-{i}", text, {"intent": "wait"}) for i, text in enumerate(WAIT_LINES)]
    assistant = m.EvalCase(case_id="assistant-start", skill="assisted-call", kind="assistant",
                           input={"text": "Please help start electric service with Lakeshore Energy.", "candidates": ["prepare_task"]},
                           expect={"intent": "prepare_task"})
    jev = with_answers(oracle.answer_jev, by_text={**answers, **{t: "wait" for t in WAIT_LINES}}, by_question={"next_action": "prepare_task"})
    settings = text_settings(mode_env, fast_engine(engine_config), tmp_path / "right")
    metrics = await eval_with(jev, oracle.answer_converse, mode="text-sim", settings=settings, skills_dir=skills_dir, menus_dir=menus,
                              cases=[*rounds, assistant, scenario_case("baseline")], scenario="baseline")
    assert metrics.menu_accuracy == pytest.approx(1.0)
    assert metrics.auto_answer_error_rate == pytest.approx(0.0)


def test_ac85_i3b_repo_scenario_rows_carry_a_request_for_every_scenario(skills_dir, company_dir):
    """D12：请求原话来自 kind=scenario 用例行的 input.request；仓库里每个场景都有一行（直接读文件，不经被测的 load_cases）。"""
    import json

    scenarios = sorted(p.stem for p in (company_dir / "lakeshore-energy" / "scenarios").glob("*.yaml"))
    rows = []
    for path in sorted(skills_dir.glob("*/evals/cases*.jsonl")):
        rows += [m.EvalCase.model_validate(json.loads(raw)) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]
    with_request = {c.input.get("scenario_id") for c in rows if c.kind == "scenario" and (c.input.get("request") or "").strip()}
    assert set(scenarios) <= with_request, f"scenarios without a request row: {sorted(set(scenarios) - with_request)}"
    assert set(SCENARIO_IDS) <= set(scenarios)


async def test_ac85_i3b_run_eval_refuses_a_scenario_without_a_request_row(tmp_path, mode_env, engine_config, company_dir, skills_dir):
    oracle = MultiOracle(company_dir)
    menus, _ = write_menus(tmp_path / "menus")
    settings = text_settings(mode_env, fast_engine(engine_config), tmp_path / "norow")
    with pytest.raises(ValueError, match="baseline"):
        await eval_with(oracle.answer_jev, oracle.answer_converse, mode="text-sim", settings=settings, skills_dir=skills_dir,
                        menus_dir=menus, cases=[scenario_case("menu-reworded")], scenario="baseline")
    assert not settings.db_path.exists() or calls_rows(settings.db_path) == {}
