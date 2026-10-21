"""文本级的对方公司：确定性台词（B 线；tech §6.10 text_script、§5.1 text-sim；AC-85、AC-87）。

公开接口（§7）：TextCompany（实现 interfaces.CompanyEngine）。不导入 Pipecat，不开任何连接。
"""

from waitless.models import CompanyConfig, CompanyLine, Scenario
from waitless.company.engine import ScenarioEngine, _text_scenario

__all__ = ["TextCompany"]


class TextCompany:
    """包住 ScenarioEngine；ai_agent 与 live_rep 步按 text_script 逐句说（每句带 expect，评测比对用）。"""

    def __init__(self, scenario: Scenario, company: CompanyConfig) -> None:
        self._scenario_id = scenario.scenario_id
        self._engine = ScenarioEngine(_text_scenario(scenario), company)

    @property
    def scenario_id(self) -> str:
        return self._scenario_id

    def start(self) -> list[CompanyLine]:
        return self._engine.start()

    def on_digits(self, digits: str) -> list[CompanyLine]:
        return self._engine.on_digits(digits)

    def on_speech(self, text: str) -> list[CompanyLine]:
        return self._engine.on_speech(text)

    def on_timeout(self) -> list[CompanyLine]:
        return self._engine.on_timeout()

    def on_handoff(self, joined: bool) -> list[CompanyLine]:
        """Private questions stay outside the decision gate and trace.

        本版新增、只限文本引擎、不证明本人同意：Leave fills unentered
        private answers for smoke runs; explicit refusals are never overwritten.
        """
        return self._engine.on_handoff(joined)

    def holder_input(self, speech=None, keys=None) -> list[CompanyLine]:
        return self._engine.holder_input(speech=speech, keys=keys)

    @property
    def ended(self) -> bool:
        return self._engine.ended

    def step_log(self) -> list[str]:
        return self._engine.step_log()
