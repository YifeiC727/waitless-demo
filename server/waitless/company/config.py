"""公司与场景配置加载（B 线；tech §6.10；AC-81）。

公开接口（§7）：load_company()、load_scenario()、ScenarioError。结构在 models.CompanyConfig、models.Scenario（冻结）。
"""

from pathlib import Path

from waitless.models import CompanyConfig, Scenario

__all__ = ["SCENARIO_IDS", "ScenarioError", "list_scenarios", "load_company", "load_scenario"]

SCENARIO_IDS: tuple[str, ...] = ("baseline", "menu-reworded", "account-holder-required")


class ScenarioError(Exception):
    """场景或公司文件不合规：max_call_seconds > 300（AC-81）、步骤引用不存在、company_id 不一致等。"""


def load_company(company_dir: Path, company_id: str) -> CompanyConfig:
    """读 company_dir/<company_id>/company.yaml（歧义表 A-13：WAITLESS_COMPANY_DIR 指 company/ 根目录）。
    company.yaml 必须有 dead_end_text（A-17）。
    缺口 B-G-9（定稿 conftest_test_account）：lakeshore-energy 的 customers[0] 照抄 tests/conftest.py 的 TEST_PROFILE
    （name、phone_number、address）与 TEST_ACCOUNT["lakeshore-energy"]（account_number），这位客户要能通过模拟坐席的核验。"""
    raise NotImplementedError


def load_scenario(company_dir: Path, company_id: str, scenario_id: str) -> Scenario:
    """读 company_dir/<company_id>/scenarios/<scenario_id>.yaml；pydantic 校验错误包成 ScenarioError。"""
    # todo(B): 校验 steps 图：next / options 的值 / on_done 的值指向的步骤都存在，或是保留目标
    #          models.DEAD_END_TARGET（A-17，定稿 reserved_hangup_target）；不存在的目标抛 ScenarioError。
    #          Scenario 模型已拒绝把 dead_end 定义成步骤。
    raise NotImplementedError


def list_scenarios(company_dir: Path, company_id: str) -> list[str]:
    raise NotImplementedError
