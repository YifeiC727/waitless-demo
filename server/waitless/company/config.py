"""公司与场景配置加载（B 线；tech §6.10；AC-81）。

公开接口（§7）：load_company()、load_scenario()、ScenarioError。结构在 models.CompanyConfig、models.Scenario（冻结）。
"""

from pathlib import Path

import yaml
from pydantic import ValidationError

from waitless.models import DEAD_END_TARGET, CompanyConfig, Scenario

__all__ = ["SCENARIO_IDS", "ScenarioError", "list_scenarios", "load_company", "load_scenario"]

SCENARIO_IDS: tuple[str, ...] = ("baseline", "menu-reworded", "account-holder-required")
_SIMULATED_USER_KEYS = {"handoff_exit": ("return", "disconnect"), "choose": None}


class ScenarioError(Exception):
    """场景或公司文件不合规：max_call_seconds > 300（AC-81）、步骤引用不存在、company_id 不一致等。"""


def load_company(company_dir: Path, company_id: str) -> CompanyConfig:
    """读 company_dir/<company_id>/company.yaml（歧义表 A-13：WAITLESS_COMPANY_DIR 指 company/ 根目录）。
    company.yaml 必须有 dead_end_text（A-17）。
    缺口 B-G-9（定稿 conftest_test_account）：lakeshore-energy 的 customers[0] 照抄 tests/conftest.py 的 TEST_PROFILE
    （name、phone_number、address）与 TEST_ACCOUNT["lakeshore-energy"]（account_number），这位客户要能通过模拟坐席的核验。"""
    path = _child(company_dir, company_id) / "company.yaml"
    try:
        company = CompanyConfig.model_validate(_read(path))
    except ValidationError as exc:
        raise ScenarioError(f"Invalid company configuration: {path}") from exc
    if company.company_id != company_id:
        raise ScenarioError("company_id does not match the company directory")
    return company


def load_scenario(company_dir: Path, company_id: str, scenario_id: str) -> Scenario:
    """读 company_dir/<company_id>/scenarios/<scenario_id>.yaml；pydantic 校验错误包成 ScenarioError。"""
    directory = _child(company_dir, company_id) / "scenarios"
    name = _child(directory, scenario_id).name
    path = _child(directory, name + ".yaml")
    try:
        scenario = Scenario.model_validate(_read(path))
    except ValidationError as exc:
        raise ScenarioError(f"Invalid scenario configuration: {path}") from exc
    if scenario.company_id != company_id or scenario.scenario_id != scenario_id:
        raise ScenarioError("Scenario identifiers do not match its file location")
    if "greeting" not in scenario.steps:
        raise ScenarioError("Scenario needs an initial greeting step")
    for name, step in scenario.steps.items():
        _check_step_extras(name, step)
        targets = [step.next] if step.next is not None else []
        targets.extend((step.options or {}).values())
        targets.extend((step.on_done or {}).values())
        for target in targets:
            if target not in scenario.steps and target != DEAD_END_TARGET:
                raise ScenarioError(f"Step {name!r} points to missing step {target!r}")
    return scenario


def _check_step_extras(name: str, step) -> None:
    cues = getattr(step, "hold_cues", None)
    if hasattr(step, "hold_cues"):
        if not isinstance(cues, list) or not cues or not all(isinstance(c, str) and c.strip() for c in cues):
            raise ScenarioError(f"Step {name!r} hold_cues must be a non-empty list of non-empty strings")
    user = getattr(step, "simulated_user", None)
    if hasattr(step, "simulated_user"):
        if step.kind != "live_rep":
            raise ScenarioError(f"Step {name!r} simulated_user belongs on a live_rep step")
        if not isinstance(user, dict) or not user or set(user) - set(_SIMULATED_USER_KEYS):
            raise ScenarioError(f"Step {name!r} simulated_user accepts only handoff_exit and choose")
        exit_mode = user.get("handoff_exit")
        if "handoff_exit" in user and exit_mode not in _SIMULATED_USER_KEYS["handoff_exit"]:
            raise ScenarioError(f"Step {name!r} handoff_exit must be return or disconnect")
        choose = user.get("choose")
        if "choose" in user and not (isinstance(choose, str) and choose.strip()):
            raise ScenarioError(f"Step {name!r} choose must be a non-empty string")


def list_scenarios(company_dir: Path, company_id: str) -> list[str]:
    directory = _child(company_dir, company_id) / "scenarios"
    if not directory.is_dir():
        raise ScenarioError(f"Missing scenario directory: {directory}")
    return sorted(path.stem for path in directory.glob("*.yaml") if path.is_file())


def _child(root: Path, name: str) -> Path:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ScenarioError("Identifiers must be single path components")
    path = root / name
    try:
        contained = path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise ScenarioError("Cannot resolve configuration path") from exc
    if not contained:
        raise ScenarioError("Configuration path escapes its root")
    return path


def _read(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ScenarioError(f"Cannot read configuration: {path}") from exc
