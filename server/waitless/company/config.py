"""公司与场景配置加载（B 线；tech §6.10；AC-81）。

公开接口（§7）：load_company()、load_scenario()、ScenarioError。结构在 models.CompanyConfig、models.Scenario（冻结）。
"""

from pathlib import Path

import yaml
from pydantic import ValidationError

from waitless.models import DEAD_END_TARGET, CompanyConfig, Scenario

__all__ = ["SCENARIO_IDS", "ScenarioError", "list_scenarios", "load_company", "load_scenario"]

SCENARIO_IDS: tuple[str, ...] = ("baseline", "menu-reworded", "account-holder-required")


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
        targets = [step.next] if step.next is not None else []
        targets.extend((step.options or {}).values())
        targets.extend((step.on_done or {}).values())
        for target in targets:
            if target not in scenario.steps and target != DEAD_END_TARGET:
                raise ScenarioError(f"Step {name!r} points to missing step {target!r}")
    return scenario


def list_scenarios(company_dir: Path, company_id: str) -> list[str]:
    directory = _child(company_dir, company_id) / "scenarios"
    if not directory.is_dir():
        raise ScenarioError(f"Missing scenario directory: {directory}")
    return sorted(path.stem for path in directory.glob("*.yaml") if path.is_file())


def _child(root: Path, name: str) -> Path:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ScenarioError("Identifiers must be single path components")
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()):
        raise ScenarioError("Configuration path escapes its root")
    return path


def _read(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ScenarioError(f"Cannot read configuration: {path}") from exc
