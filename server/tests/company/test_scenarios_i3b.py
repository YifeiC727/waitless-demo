"""B 线 · I-3b 补测（第 3 步可见组，冻结）：AC-85、AC-86 中 B 的部分，按 I3B 裁决（RUN/lanes/i3b/i3b-decisions.md）加强。

补的是 test-gaps.md 里现有可见组抓不到的行为：
- G09（AC-86）：Waitless 先说「请稍等，我去问账户持有人」时坐席不往下说；收到转达的选择或 safe_default 后才报确认号。
  停顿词写在场景文件 live_rep 步的 hold_cues 里（I3B-D2）。
- G03（AC-85）：account-holder-required 坐席的第一句就要求账户持有人上线，并带出生日期说法，不被身份规则抢先（I3B-D3）。
- G04、G06（AC-85）：三个场景的菜单都能被 navigate-ivr 的 press_patterns 解析出全部选项；wait 台词不含 consequential 词；
  收费选项那句不踩别的规则。
- G02、G12（AC-85、AC-86）：live_rep 步的 simulated_user 写明模拟用户的做法（I3B-D1），加载器校验取值。
- G08（AC-85）：两个新场景的确认号每次相同、与 baseline 不同。

只读 skills/ 下 G 的 YAML（词表、话术模板、停顿语、ivr-rules）核对台词；菜单解析用 G 的公开函数 parse_menu（TextCall 用的就是它）。
"""

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from waitless.company.config import ScenarioError, load_company, load_scenario
from waitless.company.text import TextCompany
from waitless.decision.menu_parse import parse_menu

from ._scenario_data import COMPANY_ID, SCENARIOS, is_ask_user_line, is_handoff_line, read_yaml, write_yaml

NEW = ("menu-reworded", "account-holder-required")
BASELINE_SHARED_STEPS = ("greeting", "main_menu", "service_menu", "hold", "agent")


def _ref(skills_dir: Path, *parts: str) -> Any:
    return read_yaml(skills_dir.joinpath(*parts))


def _lexicons(skills_dir: Path) -> dict[str, list[str]]:
    return _ref(skills_dir, "decide-option", "references", "lexicons.yaml")


def _say(skills_dir: Path) -> dict[str, str]:
    return _ref(skills_dir, "decide-option", "references", "say-templates.yaml")["say_templates"]


def _handoff_say(skills_dir: Path) -> dict[str, str]:
    return _ref(skills_dir, "handoff-to-user", "references", "handoff.yaml")["say_templates"]


def _stall_phrases(skills_dir: Path) -> list[str]:
    return list(_ref(skills_dir, "decide-option", "references", "stall-phrases.yaml")["phrases"])


def _precall_tags(skills_dir: Path) -> list[str]:
    data = _ref(skills_dir, "company-lakeshore-energy", "references", "pre-call-questions.yaml")
    return [t for q in data["questions"] for t in q.get("tags", [])]


def _hits(text: str, phrases: Any) -> list[str]:
    low = text.lower()
    return [p for p in phrases if str(p).lower() in low]


def _script(scenario: Any) -> list[tuple[str, Any]]:
    return [(sid, ln) for sid, st in scenario.steps.items() for ln in (st.text_script or [])]


def _rep(scenario: Any) -> Any:
    (step,) = [st for st in scenario.steps.values() if st.kind == "live_rep"]
    return step


def _offer_expect(e: Any) -> bool:
    return e is not None and (bool(e.ask_user) or e.intent == "ask_user" or e.action == "ask_user")


def _handoff_expect(e: Any) -> bool:
    return e is not None and (e.intent == "request_handoff" or e.action == "handoff")


def _load(company_dir: Path, sid: str) -> tuple[Any, Any]:
    return load_scenario(company_dir, COMPANY_ID, sid), load_company(company_dir, COMPANY_ID)


def walk_to(tc: TextCompany, scenario: Any, company: Any, stop: Any) -> list[Any]:
    """像一个总是答对的 Waitless 那样往下走，直到最后一句满足 stop。转接时：说明 → 加入 → 离开（与 Return、断开对公司侧一样）。"""
    digits = list(scenario.expected.menu_digits)
    heard = list(tc.start())
    for _ in range(40):
        last = heard[-1]
        if stop(last):
            return heard
        assert last.kind != "hangup" and not tc.ended, [ln.text for ln in heard]
        intent = last.expect.intent if last.expect else None
        if last.kind == "menu":
            heard += tc.on_digits(digits.pop(0))
        elif intent and intent.startswith("press_field:"):
            field = intent.removeprefix("press_field:")  # v5.14 E90: key whatever field the line asks for
            heard += tc.on_digits(re.sub(r"\D", "", str(getattr(company.customers[0], field))) + "#")
        elif is_handoff_line(last):
            assert tc.on_speech("I will bring the account holder in.") == []
            assert tc.on_handoff(True)
            heard += tc.on_handoff(False)
        else:
            heard += tc.on_speech("Here is the information you asked for.")
    raise AssertionError("target line never reached")


# ---------------------------------------------------------------------------
# AC-85 原文的 B 侧：三个场景文件都在，照台词的 expect 回应时走到 expected 写的 outcome 与 interrupts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sid", SCENARIOS)
def test_ac85_i3b_scenario_file_exists_and_reaches_expected_outcome_and_interrupts(company_dir: Path, sid: str) -> None:
    """AC-85：「三个场景文件都存在，text-sim 各跑到 expected 里写的 outcome 和 interrupts」中 B 负责的部分（tech §7.1：
    B 写好 3 个场景 YAML，各带 text_script 和 expected）：文件在、能加载、每个说话的步骤都有 text_script；一个永远选对的
    Waitless 照台词的 expect 回应时，打扰次数（转接与临时问用户）等于 expected.interrupts，最后那句台词的 expect.outcome
    等于 expected.outcome，确认号说出口且匹配 confirmation_pattern。"""
    from ._scenario_data import drive

    path = company_dir / COMPANY_ID / "scenarios" / f"{sid}.yaml"
    assert path.is_file(), f"missing scenario file {path}"
    s, company = _load(company_dir, sid)
    for step_id, st in s.steps.items():
        if st.kind in ("ai_agent", "live_rep"):
            assert st.text_script, f"{sid}/{step_id} has no text_script"
    run = drive(TextCompany(s, company), menu_digits=s.expected.menu_digits, customer=company.customers[0])
    assert run.problem is None and run.stalled_on is None, (run.problem, run.stalled_on)
    assert run.interrupts == s.expected.interrupts
    assert run.confirm_line is not None and run.confirm_line.expect is not None
    assert run.confirm_line.expect.outcome == s.expected.outcome
    assert re.fullmatch(s.expected.confirmation_pattern, run.confirmation or "")
    assert run.confirmation in run.confirm_line.text


# ---------------------------------------------------------------------------
# G04、G06：菜单可解析、台词不踩别的规则
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sid", SCENARIOS)
def test_ac85_i3b_every_menu_option_is_parsed_by_ivr_rules(company_dir: Path, skills_dir: Path, sid: str) -> None:
    """TextCall 用 navigate-ivr 的 press_patterns 解析菜单给 Jev 候选；解析漏掉选项，真实 Jev 就可能选不到正确键。"""
    rules = _ref(skills_dir, "navigate-ivr", "references", "ivr-rules.yaml")
    s, _ = _load(company_dir, sid)
    for step_id, st in s.steps.items():
        if st.kind == "menu":
            keys = {o.digits for o in parse_menu(st.text, rules["press_patterns"], digit_words=rules.get("digit_words"))}
            assert keys == set(st.options or {}), f"{sid}/{step_id}: parsed {sorted(keys)} from {st.text!r}"


def test_ac85_i3b_menu_reworded_rewords_both_menus_and_changes_the_keys(company_dir: Path) -> None:
    base, _ = _load(company_dir, "baseline")
    rew, _ = _load(company_dir, "menu-reworded")
    for step_id in ("main_menu", "service_menu"):
        assert rew.steps[step_id].kind == "menu"
        assert rew.steps[step_id].text != base.steps[step_id].text, f"{step_id} wording is unchanged"
    assert rew.expected.menu_digits == ["3", "2"]
    assert base.expected.menu_digits == ["1", "1"]


@pytest.mark.parametrize("sid", SCENARIOS)
def test_ac85_i3b_wait_lines_carry_no_consequential_marker_and_budget_fits(company_dir: Path, skills_dir: Path, sid: str) -> None:
    lex = _lexicons(skills_dir)
    budget = _ref(skills_dir, "decide-option", "policy.yaml")["global"]["interrupt_budget"]
    s, _ = _load(company_dir, sid)
    assert s.expected.interrupts <= budget
    for step_id, ln in _script(s):
        if ln.expect.intent == "wait":
            assert not _hits(ln.say, lex["consequential"]), f"{sid}/{step_id}: {ln.say!r}"


def test_ac86_i3b_offer_line_is_the_only_choice_and_avoids_other_rules(company_dir: Path, skills_dir: Path) -> None:
    lex = _lexicons(skills_dir)
    s, _ = _load(company_dir, "menu-reworded")
    offers = [(sid, ln) for sid, ln in _script(s) if _offer_expect(ln.expect)]
    assert len(offers) == 1
    step_id, offer = offers[0]
    assert s.steps[step_id].kind == "live_rep"
    assert re.search(r"\$\d", offer.say) and " or " in offer.say
    assert _hits(offer.say, lex["consequential"]), "the fee line must read as consequential"
    assert not _hits(offer.say, _precall_tags(skills_dir)), "a pre-call tag would turn it into a task-card answer"
    for key in ("identity", "ssn", "card_number", "date_of_birth"):
        assert not _hits(offer.say, lex[key]), key
    assert not any(_handoff_expect(ln.expect) for _, ln in _script(s)), "the scheduling desk never asks for the account holder"


def test_ac85_i3b_account_holder_required_first_rep_line_demands_the_holder(company_dir: Path, skills_dir: Path) -> None:
    lex = _lexicons(skills_dir)
    s, _ = _load(company_dir, "account-holder-required")
    rep = _rep(s)
    first = rep.text_script[0]
    assert first.expect.intent == "request_handoff" and first.expect.action == "handoff"
    assert _hits(first.say, lex["date_of_birth"]), "policy hands off on date-of-birth requests (forbidden_info)"
    assert not _hits(first.say, lex["identity"]), "an identity phrase would be answered as an AI disclosure instead"
    assert not _hits(first.say, lex["consequential"])
    assert not any(ln.expect.intent == "state_ai_identity" for ln in rep.text_script)
    base, _ = _load(company_dir, "baseline")
    for step_id in BASELINE_SHARED_STEPS:
        assert s.steps[step_id].model_dump() == base.steps[step_id].model_dump(), step_id


# ---------------------------------------------------------------------------
# G02、G12：simulated_user（I3B-D1）
# ---------------------------------------------------------------------------


def test_ac85_i3b_simulated_user_names_one_option_and_the_disconnect(company_dir: Path) -> None:
    rew_rep = _rep(_load(company_dir, "menu-reworded")[0])
    ahr_rep = _rep(_load(company_dir, "account-holder-required")[0])
    assert getattr(ahr_rep, "simulated_user", None) == {"handoff_exit": "disconnect"}
    user = getattr(rew_rep, "simulated_user", None)
    assert isinstance(user, dict) and set(user) <= {"choose", "handoff_exit"}
    choose = user["choose"]
    offer = next(ln.say for ln in rew_rep.text_script if _offer_expect(ln.expect))
    parts = re.split(r",?\s+or\s+", offer)
    assert len(parts) >= 2
    assert sum(choose.lower() in p.lower() for p in parts) == 1, f"{choose!r} must name exactly one option of {offer!r}"
    base_rep = _rep(_load(company_dir, "baseline")[0])
    assert getattr(base_rep, "simulated_user", None) in (None, {"handoff_exit": "return"}), "baseline users click Return"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hold_cues", "hold"),
        ("hold_cues", ["hold", ""]),
        ("simulated_user", {"handoff_exit": "hang_up"}),
        ("simulated_user", {"pick": "no charge"}),
        ("simulated_user", {"choose": ""}),
    ],
)
def test_ac85_i3b_loader_rejects_malformed_step_extras(company_dir: Path, tmp_path: Path, field: str, value: Any) -> None:
    root = tmp_path / "company"
    shutil.copytree(company_dir, root)
    path = root / COMPANY_ID / "scenarios" / "menu-reworded.yaml"
    data = read_yaml(path)
    data["steps"]["rep"][field] = value
    write_yaml(path, data)
    with pytest.raises(ScenarioError):
        load_scenario(root, COMPANY_ID, "menu-reworded")


def test_ac85_i3b_loader_rejects_simulated_user_outside_live_rep(company_dir: Path, tmp_path: Path) -> None:
    root = tmp_path / "company"
    shutil.copytree(company_dir, root)
    path = root / COMPANY_ID / "scenarios" / "account-holder-required.yaml"
    data = read_yaml(path)
    data["steps"]["agent"]["simulated_user"] = {"handoff_exit": "disconnect"}
    write_yaml(path, data)
    with pytest.raises(ScenarioError):
        load_scenario(root, COMPANY_ID, "account-holder-required")


# ---------------------------------------------------------------------------
# G09（AC-86）：坐席等用户的选择
# ---------------------------------------------------------------------------


def _at_offer(company_dir: Path) -> tuple[TextCompany, Any]:
    s, company = _load(company_dir, "menu-reworded")
    tc = TextCompany(s, company)
    walk_to(tc, s, company, is_ask_user_line)
    return tc, s


def test_ac86_i3b_rep_waits_while_waitless_asks_the_account_holder(company_dir: Path, skills_dir: Path) -> None:
    tc, s = _at_offer(company_dir)
    for text in [_handoff_say(skills_dir)["handoff.ask_user"], *_stall_phrases(skills_dir)]:
        assert tc.on_speech(text) == [], f"the representative moved on after {text!r}"
        assert not tc.ended
    relay = _say(skills_dir)["pending.answer"].format(option_label="on November 3 at no charge", option_detail="")
    after = tc.on_speech(relay)
    assert after and after[-1].expect is not None and after[-1].expect.intent == "confirm_and_end"
    assert re.fullmatch(s.expected.confirmation_pattern, after[-1].confirmation_number or "")
    assert after[-1].confirmation_number in after[-1].text


def test_ac86_i3b_safe_default_also_lets_the_rep_move_on(company_dir: Path, skills_dir: Path) -> None:
    tc, _ = _at_offer(company_dir)
    assert tc.on_speech(_handoff_say(skills_dir)["handoff.ask_user"]) == []
    after = tc.on_speech(_say(skills_dir)["pending.safe_default"])
    assert after and after[-1].expect is not None and after[-1].expect.intent == "confirm_and_end"


def test_ac85_i3b_baseline_rep_is_unchanged_by_the_hold_rule(company_dir: Path, skills_dir: Path) -> None:
    """baseline 不写 hold_cues：坐席问押金时，任何回答都照旧让它往下说（停顿词规则只在写了 hold_cues 的 ask_user 那句生效）。"""
    s, company = _load(company_dir, "baseline")
    assert getattr(_rep(s), "hold_cues", None) is None
    tc = TextCompany(s, company)
    heard = walk_to(tc, s, company, lambda ln: (ln.expect is not None and (ln.expect.intent or "").startswith("answer_precall:")))
    assert heard[-1].role == "rep"
    after = tc.on_speech(_stall_phrases(skills_dir)[0])
    assert after and after[-1].expect is not None and after[-1].expect.intent == "confirm_and_end"


# ---------------------------------------------------------------------------
# account-holder-required 公司侧：转接 → 接回后押金 → 确认号；G08 确认号
# ---------------------------------------------------------------------------


def test_ac85_i3b_account_holder_required_handoff_then_precall_then_confirmation(company_dir: Path, skills_dir: Path) -> None:
    s, company = _load(company_dir, "account-holder-required")
    tc = TextCompany(s, company)
    heard = walk_to(tc, s, company, is_handoff_line)
    assert [ln.role for ln in heard].count("rep") == 1, "the demand is the first thing the representative says"
    assert tc.on_speech(_handoff_say(skills_dir)["handoff.announce"]) == []
    joined = tc.on_handoff(True)
    assert joined and all(ln.role == "rep" and ln.expect is None for ln in joined)
    left = tc.on_handoff(False)
    assert left[-1].expect is not None and left[-1].expect.intent == "answer_precall:deposit_or_autopay"
    done = tc.on_speech(_say(skills_dir)["answer_precall"].format(option_label="Pay a security deposit"))
    assert done[-1].expect is not None and done[-1].expect.intent == "confirm_and_end"
    assert re.fullmatch(s.expected.confirmation_pattern, done[-1].confirmation_number or "")
    assert tc.step_log()[-1] == "rep"


@pytest.mark.parametrize("sid", NEW)
def test_ac85_i3b_new_scenarios_reach_a_stable_confirmation_unlike_baseline(company_dir: Path, sid: str) -> None:
    def number(scenario_id: str) -> str:
        s, company = _load(company_dir, scenario_id)
        heard = walk_to(TextCompany(s, company), s, company, lambda ln: bool(ln.confirmation_number))
        return heard[-1].confirmation_number

    first, second = number(sid), number(sid)
    assert first == second
    assert re.fullmatch(r"LE-\d{5}", first)
    assert first != number("baseline")
