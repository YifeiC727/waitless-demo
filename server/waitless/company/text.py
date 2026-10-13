"""文本级的对方公司：确定性台词（B 线；tech §6.10 text_script、§5.1 text-sim；AC-85、AC-87）。

公开接口（§7）：TextCompany（实现 interfaces.CompanyEngine）。不导入 Pipecat，不开任何连接。
"""

from waitless.models import CompanyConfig, CompanyLine, Scenario

__all__ = ["TextCompany"]


class TextCompany:
    """包住 ScenarioEngine；ai_agent 与 live_rep 步按 text_script 逐句说（每句带 expect，评测比对用）。"""

    def __init__(self, scenario: Scenario, company: CompanyConfig) -> None:
        raise NotImplementedError

    @property
    def scenario_id(self) -> str:
        raise NotImplementedError

    def start(self) -> list[CompanyLine]:
        raise NotImplementedError

    def on_digits(self, digits: str) -> list[CompanyLine]:
        raise NotImplementedError

    def on_speech(self, text: str) -> list[CompanyLine]:
        raise NotImplementedError

    def on_timeout(self) -> list[CompanyLine]:
        raise NotImplementedError

    def on_handoff(self, joined: bool) -> list[CompanyLine]:
        """文本模式：账户持有人加入即视为本人验证通过（require_account_holder）。
        缺口 B-G-8（定稿 rep_lines_during_handoff）：joined=True 返回坐席对账户持有人说的几句，E 的 TextCall 只记进状态、
        不送决策门；joined=False 返回坐席接着对 Waitless 说的台词，以需要回应的一句结尾。
        每批台词的结尾规则同 ScenarioEngine（缺口 B-G-5，暂定）。"""
        raise NotImplementedError

    @property
    def ended(self) -> bool:
        raise NotImplementedError

    def step_log(self) -> list[str]:
        raise NotImplementedError
