"""B 线测试用的场景数据工具与「理想的 Waitless」驱动器（第 3 步写，冻结）。只在测试里用。

- 场景与公司文件由 B 线写在仓库 company/lakeshore-energy/ 下（tech §6.10）；测试经 conftest 的
  company_dir fixture 找到它（歧义表 A-13：company_dir 指 company/ 根目录）。文件还没写时，
  require_* 用 pytest.fail 给出明确的「B 线数据未就绪」失败，而不是导入错误。
- 需要改参数（超时次数、时长上限、按键映射）的测试，把 B 的真实文件整份复制到 tmp_path 再改，
  这样不依赖 B 线自己的写法细节（例如 dead_end、确认号模板）。
- drive()：只看每句台词的 kind 与 expect（场景自己写的评测期望），像一个永远选对的 Waitless
  那样回应文本公司。E 线的 TextCall 走的是同一套接口（interfaces.CompanyEngine）。
"""

import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from waitless.models import (
    ANSWER_PRECALL_PREFIX,
    PRESS_FIELD_PREFIX,
    ROUND_INTENTS,
    CompanyConfig,
    CompanyCustomer,
    CompanyLine,
    Scenario,
)

COMPANY_ID = "lakeshore-energy"
SCENARIOS = ("baseline", "menu-reworded", "account-holder-required")
BASELINE_STEP_ORDER = ["greeting", "main_menu", "service_menu", "hold", "agent", "rep"]
CONFIRMATION_PATTERN = r"^LE-\d{5}$"


# ---------------------------------------------------------------------------
# 仓库数据
# ---------------------------------------------------------------------------


def company_root(company_dir: Path) -> Path:
    root = company_dir / COMPANY_ID
    if not (root / "company.yaml").is_file():
        pytest.fail(f"B lane data missing: {root / 'company.yaml'} (tech §6.10)")
    return root


def scenario_path(company_dir: Path, scenario_id: str) -> Path:
    path = company_root(company_dir) / "scenarios" / f"{scenario_id}.yaml"
    if not path.is_file():
        pytest.fail(f"B lane data missing: {path} (tech §6.10)")
    return path


def read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def raw_company(company_dir: Path) -> dict[str, Any]:
    return read_yaml(company_root(company_dir) / "company.yaml")


def raw_scenario(company_dir: Path, scenario_id: str) -> dict[str, Any]:
    return read_yaml(scenario_path(company_dir, scenario_id))


def model_company(company_dir: Path) -> CompanyConfig:
    """只经冻结的数据模型校验（不经 B 的加载器），用来单独检查数据本身。"""
    return CompanyConfig.model_validate(raw_company(company_dir))


def model_scenario(company_dir: Path, scenario_id: str) -> Scenario:
    return Scenario.model_validate(raw_scenario(company_dir, scenario_id))


def copy_company_root(company_dir: Path, tmp_path: Path) -> Path:
    """把整个 company/ 复制到 tmp_path/company，返回新的 company_dir（含 www 等全部文件）。"""
    company_root(company_dir)  # 不存在时明确失败
    dst = tmp_path / "company"
    shutil.copytree(company_dir, dst)
    return dst


def edit_company_yaml(root_dir: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    path = root_dir / COMPANY_ID / "company.yaml"
    data = read_yaml(path)
    edit(data)
    write_yaml(path, data)


def edit_scenario_yaml(root_dir: Path, scenario_id: str, edit: Callable[[dict[str, Any]], None]) -> None:
    path = root_dir / COMPANY_ID / "scenarios" / f"{scenario_id}.yaml"
    data = read_yaml(path)
    edit(data)
    write_yaml(path, data)


# ---------------------------------------------------------------------------
# 理想的 Waitless 驱动器
# ---------------------------------------------------------------------------

DEFAULT_ANSWERS: dict[str, str] = {
    "open_answer": "The service address is 1200 W Example St, Apt 4B, and service should start on November 1.",
    "identity": "I'm an AI assistant calling on behalf of the account holder.",
    "handoff_announce": "One moment please, I'm bringing the account holder onto this call.",
    "precall": "The account holder has decided to go with the option on the task card.",
    "ask_user": "The account holder chose this: {offer}",
    "decline": "No, thank you.",
    "repeat": "Sorry, could you repeat that?",
    "goodbye": "Thank you, goodbye.",
}

NO_INPUT_INTENTS = frozenset({"wait"})


def intent_of(line: CompanyLine) -> str | None:
    return line.expect.intent if line.expect is not None else None


def is_handoff_line(line: CompanyLine) -> bool:
    e = line.expect
    return e is not None and (e.intent == "request_handoff" or e.action == "handoff")


def is_ask_user_line(line: CompanyLine) -> bool:
    e = line.expect
    return e is not None and not is_handoff_line(line) and (e.ask_user is True or e.intent == "ask_user" or e.action == "ask_user")


def is_confirm_line(line: CompanyLine) -> bool:
    e = line.expect
    return e is not None and (e.intent == "confirm_and_end" or e.action == "end")


def needs_response(line: CompanyLine) -> bool:
    """这句话之后，公司是否在等 Waitless 的输入。"""
    if line.kind == "menu":
        return True
    if line.kind == "hangup":
        return False
    intent = intent_of(line)
    if line.expect is None:
        return False
    return intent not in NO_INPUT_INTENTS


def intent_is_known(intent: str | None) -> bool:
    if intent is None:
        return True
    return intent in ROUND_INTENTS or (
        intent.startswith(PRESS_FIELD_PREFIX) and len(intent) > len(PRESS_FIELD_PREFIX)
    ) or (intent.startswith(ANSWER_PRECALL_PREFIX) and len(intent) > len(ANSWER_PRECALL_PREFIX))


@dataclass
class DriverRun:
    transcript: list[CompanyLine] = field(default_factory=list)
    batches: list[tuple[str, list[CompanyLine]]] = field(default_factory=list)  # (动作, 公司随后说的话)
    menu_presses: list[str] = field(default_factory=list)
    interrupts: int = 0
    confirmation: str | None = None
    confirm_line: CompanyLine | None = None
    offer_lines: list[CompanyLine] = field(default_factory=list)
    handoff_lines: list[CompanyLine] = field(default_factory=list)  # on_handoff(True) 对账户持有人说的话，不由 Waitless 回应
    stalled_on: CompanyLine | None = None
    problem: str | None = None
    hung_up: bool = False

    def texts(self) -> list[str]:
        return [ln.text for ln in self.transcript]


def _field_digits(customer: CompanyCustomer, field_name: str) -> str:
    value = getattr(customer, field_name, None)
    if value is None:
        raise AssertionError(f"press_field:{field_name} has no value on the fictional customer")
    digits = re.sub(r"[^0-9]", "", str(value))
    if not digits:
        raise AssertionError(f"press_field:{field_name} value {value!r} has no keypad digits")
    return digits


def drive(
    company: Any,
    *,
    menu_digits: list[str],
    customer: CompanyCustomer,
    answers: Mapping[str, str] | None = None,
    stop_at_confirmation: bool = True,
    max_actions: int = 60,
    menu_timeouts_before_press: int = 0,
) -> DriverRun:
    """像一个永远选对的 Waitless 那样驱动文本公司，返回整通对话的记录。

    规则：只回应「最后一句」；menu 按 menu_digits 依次按键；其余按 expect.intent / action 回应。
    转接（B-G-8 定稿）：说明话术 → on_handoff(True)（坐席对账户持有人说的话，必须非空，Waitless 不回应）→
    on_handoff(False)（坐席接着对 Waitless 说，必须以需要回应的一句结尾）。
    一批台词以「不需要回应」的句子结尾（例如 hold、expect.intent=wait 或没有 expect 的话）而公司又没挂断，
    记为 stalled_on——文本模式里没有时间流逝，公司必须在同一批里自己接着往下说。
    menu_timeouts_before_press：每个菜单先让它超时几次再按键（检验「连续」超时才挂断）。
    """
    ans = dict(DEFAULT_ANSWERS)
    if answers:
        ans.update(answers)
    run = DriverRun()
    digits_left = list(menu_digits)

    def take(action: str, lines: list[CompanyLine]) -> None:
        assert isinstance(lines, list), f"{action} must return a list of CompanyLine"
        for ln in lines:
            assert isinstance(ln, CompanyLine), f"{action} returned {type(ln).__name__}, not CompanyLine"
            run.transcript.append(ln)
            if ln.confirmation_number and run.confirmation is None:
                run.confirmation = ln.confirmation_number
            if ln.kind == "hangup":
                run.hung_up = True
        run.batches.append((action, list(lines)))

    take("start", company.start())
    answered = -1
    for _ in range(max_actions):
        if run.hung_up or company.ended:
            return run
        if not run.transcript:
            run.problem = "start() returned no lines"
            return run
        idx = len(run.transcript) - 1
        last = run.transcript[idx]
        if idx == answered or not needs_response(last):
            run.stalled_on = last
            return run
        answered = idx
        if is_confirm_line(last):
            run.confirm_line = last
            if run.confirmation is None:
                run.confirmation = last.confirmation_number
            if stop_at_confirmation:
                return run
            take("speech:goodbye", company.on_speech(ans["goodbye"]))
            continue
        if last.kind == "menu":
            for _t in range(menu_timeouts_before_press):
                take("timeout", company.on_timeout())
                if run.hung_up or company.ended:
                    return run
            answered = len(run.transcript) - 1
            if not digits_left:
                run.problem = f"menu {last.step_id!r} asked for more keys than expected.menu_digits={menu_digits}"
                return run
            d = digits_left.pop(0)
            run.menu_presses.append(d)
            take(f"digits:{d}", company.on_digits(d))
            continue
        intent = intent_of(last)
        if not intent_is_known(intent):
            run.problem = f"unknown expect.intent {intent!r} on line {last.text!r}"
            return run
        if is_handoff_line(last):
            run.interrupts += 1
            take("speech:announce", company.on_speech(ans["handoff_announce"]))
            joined = company.on_handoff(True)
            take("handoff:join", joined)
            if not joined:
                run.problem = "on_handoff(True) must return what the representative says to the account holder (B-G-8)"
                return run
            run.handoff_lines.extend(joined)
            left = company.on_handoff(False)
            take("handoff:leave", left)
            if run.hung_up or company.ended:
                continue
            if not left or not needs_response(left[-1]):
                run.problem = "on_handoff(False) must end with a line Waitless has to answer (B-G-8)"
                return run
            continue
        if is_ask_user_line(last):
            run.interrupts += 1
            run.offer_lines.append(last)
            take("speech:user_choice", company.on_speech(ans["ask_user"].format(offer=last.text)))
            continue
        assert intent is not None
        if intent.startswith(PRESS_FIELD_PREFIX):
            field_name = intent[len(PRESS_FIELD_PREFIX) :]
            digits = _field_digits(customer, field_name)
            if "pound" in last.text.lower() or "#" in last.text:
                digits += "#"
            take(f"digits:{digits}", company.on_digits(digits))
        elif intent == "open_answer":
            take("speech:open_answer", company.on_speech(ans["open_answer"]))
        elif intent == "state_ai_identity":
            take("speech:identity", company.on_speech(ans["identity"]))
        elif intent.startswith(ANSWER_PRECALL_PREFIX):
            take("speech:precall", company.on_speech(ans["precall"]))
        elif intent == "decline_offer":
            take("speech:decline", company.on_speech(ans["decline"]))
        elif intent == "ask_repeat":
            take("speech:repeat", company.on_speech(ans["repeat"]))
        else:
            run.problem = f"line {last.text!r} expects {intent!r}, which needs no reply but ended the batch"
            return run
    run.problem = f"no confirmation after {max_actions} actions"
    return run


def script_lines(scenario: Scenario) -> list[tuple[str, Any]]:
    """场景里所有 text_script 台词：(step_id, TextScriptLine)。"""
    out = []
    for step_id, step in scenario.steps.items():
        for line in step.text_script or []:
            out.append((step_id, line))
    return out


def menu_steps(scenario: Scenario) -> dict[str, Any]:
    return {sid: st for sid, st in scenario.steps.items() if st.kind == "menu"}
