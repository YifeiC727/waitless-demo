"""B 线 · 文字部分（10/14 晚前）：公司与场景配置（tech §6.10；AC-81，以及 AC-82、AC-83 的数据前提）。

加载器测试把 B 写的真实文件复制到 tmp_path 再改一处，所以只依赖 §6.10 的结构，不依赖 B 的其他写法。
数据测试直接用冻结的 models 校验 YAML（不经 B 的加载器），加载器坏了也能单独看出数据对不对。
"""

import re
from pathlib import Path

import pytest

from waitless.company.config import ScenarioError, list_scenarios, load_company, load_scenario
from waitless.models import DEAD_END_TARGET, CompanyConfig, Scenario

from ._scenario_data import (
    COMPANY_ID,
    CONFIRMATION_PATTERN,
    copy_company_root,
    edit_scenario_yaml,
    intent_is_known,
    model_company,
    model_scenario,
    read_yaml,
    write_yaml,
)

LINE_NUMBER = "(312) 555-0100"  # §6.10（555 虚构号段）
TRADEMARKS = re.compile("|".join(["cost" + "co", "bank of " + "america", r"\b" + "bo" + "a" + r"\b"]), re.IGNORECASE)  # AC-17 的词表（拼接构造，仓库里不出现商标原词，避免 scripts/checks.sh trademarks 自命中）
AUDIO_EXT = re.compile(r"\.(wav|mp3|pcm|ulaw|mulaw|m4a|ogg|webm|flac)$", re.IGNORECASE)  # AC-100


# ---------------------------------------------------------------------------
# AC-81 场景加载器：max_call_seconds ≤ 300，校验错误包成 ScenarioError
# ---------------------------------------------------------------------------


def test_ac81_scenario_schema_rejects_over_300_seconds(company_dir: Path, tmp_path: Path) -> None:
    root = copy_company_root(company_dir, tmp_path)
    edit_scenario_yaml(root, "baseline", lambda d: d.update(max_call_seconds=301))
    with pytest.raises(ScenarioError):
        load_scenario(root, COMPANY_ID, "baseline")


def test_ac81_scenario_schema_accepts_exactly_300_seconds(company_dir: Path, tmp_path: Path) -> None:
    root = copy_company_root(company_dir, tmp_path)
    edit_scenario_yaml(root, "baseline", lambda d: d.update(max_call_seconds=300))
    scenario = load_scenario(root, COMPANY_ID, "baseline")
    assert isinstance(scenario, Scenario)
    assert scenario.max_call_seconds == 300


def test_ac81_scenario_schema_repo_baseline_loads_within_limit(company_dir: Path) -> None:
    scenario = load_scenario(company_dir, COMPANY_ID, "baseline")
    assert isinstance(scenario, Scenario)
    assert scenario.scenario_id == "baseline"
    assert scenario.company_id == COMPANY_ID
    assert 0 < scenario.max_call_seconds <= 300
    assert scenario == model_scenario(company_dir, "baseline"), "loader must return exactly what the file says"


def test_ac81_scenario_schema_allows_dead_end_target(company_dir: Path, tmp_path: Path) -> None:
    # A-17 定稿：options 和 next 都可以指向保留目标 dead_end（这里加一个新选项，原有步骤仍都可达）
    root = copy_company_root(company_dir, tmp_path)
    edit_scenario_yaml(root, "baseline", lambda d: d["steps"]["main_menu"]["options"].update({"4": "dead_end"}))
    scenario = load_scenario(root, COMPANY_ID, "baseline")
    assert scenario.steps["main_menu"].options is not None
    assert scenario.steps["main_menu"].options["4"] == "dead_end"


def test_ac81_scenario_schema_rejects_dangling_step_reference(company_dir: Path, tmp_path: Path) -> None:
    root = copy_company_root(company_dir, tmp_path)
    edit_scenario_yaml(root, "baseline", lambda d: d["steps"]["main_menu"]["options"].update({"1": "no_such_step"}))
    with pytest.raises(ScenarioError):
        load_scenario(root, COMPANY_ID, "baseline")


def test_ac81_load_company_returns_company_yaml(company_dir: Path) -> None:
    company = load_company(company_dir, COMPANY_ID)
    assert isinstance(company, CompanyConfig)
    assert company == model_company(company_dir)


def test_ac81_list_scenarios_follows_files(company_dir: Path, tmp_path: Path) -> None:
    root = copy_company_root(company_dir, tmp_path)
    scen_dir = root / COMPANY_ID / "scenarios"
    for p in scen_dir.glob("*.yaml"):
        if p.stem != "baseline":
            p.unlink()
    assert list_scenarios(root, COMPANY_ID) == ["baseline"]
    data = read_yaml(scen_dir / "baseline.yaml")
    data["scenario_id"] = "menu-reworded"
    write_yaml(scen_dir / "menu-reworded.yaml", data)
    assert sorted(list_scenarios(root, COMPANY_ID)) == ["baseline", "menu-reworded"]


# ---------------------------------------------------------------------------
# company.yaml 的内容（§6.10）
# ---------------------------------------------------------------------------


def test_ac82_company_yaml_identity_voices_and_site(company_dir: Path) -> None:
    c = model_company(company_dir)
    assert c.company_id == COMPANY_ID
    assert c.name == "Lakeshore Energy"
    assert c.line_number == LINE_NUMBER
    assert c.website.domain == "lakeshore-energy.example"
    assert c.website.dir == "www"
    assert (c.voices.ivr_polly, c.voices.agent_sonic, c.voices.rep_sonic) == ("Joanna", "tiffany", "matthew")
    assert c.voices.agent_sonic != c.voices.rep_sonic, "the representative must sound different from the AI agent"


def test_ac82_company_yaml_customers_are_fictional_and_keyable(company_dir: Path, test_profile, test_account) -> None:  # type: ignore[no-untyped-def]
    c = model_company(company_dir)
    assert c.customers, "at least one fictional customer is needed for verifies: account_number"
    first = c.customers[0]  # B-G-9 定稿：customers[0] 照抄 conftest 的 TEST_PROFILE 与 TEST_ACCOUNT
    assert (first.name, first.phone_number, first.address) == (
        test_profile["name"],
        test_profile["phone_number"],
        test_profile["address"],
    )
    assert first.account_number == test_account[COMPANY_ID]
    for cust in c.customers:
        assert re.fullmatch(r"[0-9]{4,}", cust.account_number), "account numbers are typed on a keypad (press_field)"
        assert re.search(r"555-01\d\d", cust.phone_number), "phone numbers stay in the fictional 555-01xx range"
        assert cust.name and cust.address


def test_ac83_company_menu_timeouts_from_spec(company_dir: Path) -> None:
    c = model_company(company_dir)
    assert (c.menu.timeout_sec, c.menu.max_timeouts) == (8, 2)  # R25：超时两次挂断


def test_ac82_company_yaml_has_dead_end_text(company_dir: Path) -> None:
    # A-17 定稿：company.yaml 必须有 dead_end_text（CompanyConfig 必填字段）
    c = model_company(company_dir)
    assert c.dead_end_text.strip(), "company.yaml needs dead_end_text (A-17)"


# ---------------------------------------------------------------------------
# baseline.yaml 的步骤图与台词（§6.10、§7.1 AC-82、§8）
# ---------------------------------------------------------------------------


def test_ac82_company_baseline_steps_graph(company_dir: Path) -> None:
    s = model_scenario(company_dir, "baseline")
    st = s.steps
    assert s.scenario_id == "baseline" and s.company_id == COMPANY_ID
    assert st["greeting"].kind == "prompt" and st["greeting"].next == "main_menu"
    assert st["main_menu"].kind == "menu"
    assert st["main_menu"].options is not None
    assert st["main_menu"].options["1"] == "service_menu"
    assert st["main_menu"].options.get("9") == "main_menu", "9 repeats the main menu (§6.10)"
    assert st["main_menu"].options.get("2") == st["main_menu"].options.get("3") == DEAD_END_TARGET
    assert st["service_menu"].kind == "menu"
    assert st["service_menu"].options is not None and st["service_menu"].options["1"] == "hold"
    assert st["service_menu"].options.get("2") == st["service_menu"].options.get("3") == DEAD_END_TARGET
    assert st["hold"].kind == "hold" and st["hold"].next == "agent"
    assert st["agent"].kind == "ai_agent"
    assert st["agent"].verifies == "account_number"
    assert st["agent"].on_done is not None and st["agent"].on_done.get("deposit_review") == "rep"
    assert st["rep"].kind == "live_rep"
    assert s.expected.menu_digits == ["1", "1"]
    assert s.expected.interrupts == 1
    assert s.expected.outcome == "confirmed"
    assert s.expected.confirmation_pattern == CONFIRMATION_PATTERN


@pytest.mark.parametrize("step_id", ["main_menu", "service_menu"])
def test_ac82_company_baseline_menus_announce_every_option(company_dir: Path, step_id: str) -> None:
    step = model_scenario(company_dir, "baseline").steps[step_id]
    assert step.text and step.options
    for key in step.options:
        assert re.search(rf"\bpress {re.escape(key)}\b", step.text, re.IGNORECASE), (
            f"{step_id} offers {key!r} but its prompt never says 'press {key}'"
        )


def test_ac82_company_baseline_agent_and_rep_scripts(company_dir: Path) -> None:
    s = model_scenario(company_dir, "baseline")
    agent, rep = s.steps["agent"], s.steps["rep"]
    assert agent.persona and rep.persona, "live-sim bots need a persona for each side"
    agent_intents = [ln.expect.intent for ln in agent.text_script or []]
    assert "open_answer" in agent_intents
    assert "press_field:account_number" in agent_intents
    assert agent_intents[-1] == "wait", "the agent's last line is the transfer to a representative"
    dos = [a.do for a in rep.actions or []]
    order = ["ask_identity", "require_account_holder", "offer", "confirm"]
    positions = [dos.index(d) if d in dos else -1 for d in order]
    assert -1 not in positions and positions == sorted(positions), f"rep actions {dos} must include {order} in order"
    require = (rep.actions or [])[dos.index("require_account_holder")]
    assert require.verification == "date_of_birth"
    confirm = (rep.actions or [])[dos.index("confirm")]
    assert confirm.confirmation_format == "LE-{5d}"
    rep_lines = rep.text_script or []

    def first(pred) -> int:  # type: ignore[no-untyped-def]
        for i, ln in enumerate(rep_lines):
            if pred(ln.expect):
                return i
        return -1

    i_identity = first(lambda e: e.intent == "state_ai_identity")
    i_handoff = first(lambda e: e.intent == "request_handoff" or e.action == "handoff")
    i_precall = first(lambda e: (e.intent or "").startswith("answer_precall:"))
    i_confirm = first(lambda e: e.intent == "confirm_and_end")
    assert -1 not in (i_identity, i_handoff, i_precall, i_confirm), [ln.expect for ln in rep_lines]
    assert i_identity < i_handoff < i_precall < i_confirm, "§8 main line: identity → handoff → deposit → confirmation"


def test_ac82_company_baseline_script_intents_are_round_intents(company_dir: Path) -> None:
    s = model_scenario(company_dir, "baseline")
    for step_id in ("agent", "rep"):
        for ln in s.steps[step_id].text_script or []:
            assert intent_is_known(ln.expect.intent), f"{step_id}: {ln.say!r} expects unknown intent {ln.expect.intent!r}"


# ---------------------------------------------------------------------------
# 虚构品牌、没有音频文件（AC-17、AC-100 在 company/ 下的部分）
# ---------------------------------------------------------------------------


def test_ac17_ac100_company_files_fictional_and_audio_free(company_dir: Path) -> None:
    model_company(company_dir)  # 数据未就绪时明确失败
    files = [p for p in (company_dir).rglob("*") if p.is_file()]
    assert files
    for p in files:
        rel = p.relative_to(company_dir)
        assert not AUDIO_EXT.search(p.name), f"audio file in repo: company/{rel}"
        if p.suffix.lower() in {".yaml", ".yml", ".html", ".md", ".txt", ".json"}:
            hit = TRADEMARKS.search(p.read_text(encoding="utf-8"))
            assert hit is None, f"third-party mark {hit.group(0)!r} in company/{rel}"
