"""场景引擎：按 steps 图推进并逐步记录（B 线；tech §6.10；AC-82、AC-83）。

公开接口（§7）：ScenarioEngine。文本公司（company/text.py）与公司 bot（company/bot.py）共用它。
"""

from waitless.models import CompanyConfig, CompanyLine, Scenario

__all__ = ["ScenarioEngine"]


class ScenarioEngine:
    """baseline 的步骤顺序：greeting → main_menu → service_menu → hold → agent → rep（AC-82）。

    缺口 B-G-5（定稿 same_batch，暂定）：文本模式里 hold 不占时间，hold 那句之后同一批接着给出下一步的台词；
    不需要回应的台词（expect.intent=wait）之后也在同一批继续，直到一句需要 Waitless 回应的台词或挂断为止
    （interfaces.CompanyEngine 的类说明同此）。
    """

    def __init__(self, scenario: Scenario, company: CompanyConfig) -> None:
        raise NotImplementedError

    @property
    def current_step(self) -> str:
        raise NotImplementedError

    @property
    def ended(self) -> bool:
        raise NotImplementedError

    def start(self) -> list[CompanyLine]:
        raise NotImplementedError

    def on_digits(self, digits: str) -> list[CompanyLine]:
        """菜单步按 options 跳转；ai_agent 的 verifies 字段（如 account_number）按 customers 核对（R25）。
        目标是 models.DEAD_END_TARGET 时（A-17）：先给一句 CompanyLine(step_id="dead_end", kind="prompt",
        role="ivr", text=company.dead_end_text)，再给 kind="hangup" 的一句，之后 ended=True；
        按错键的通话在评测里是 failed 或 needs_follow_up。next / on_done 指向它时同样处理。"""
        raise NotImplementedError

    def on_speech(self, text: str) -> list[CompanyLine]:
        raise NotImplementedError

    def on_timeout(self) -> list[CompanyLine]:
        """菜单等待 company.menu.timeout_sec；连续 max_timeouts 次超时 → kind=hangup（AC-83）。"""
        raise NotImplementedError

    def on_handoff(self, joined: bool) -> list[CompanyLine]:
        """缺口 B-G-8（定稿 rep_lines_during_handoff）：joined=True 返回坐席对账户持有人说的几句（如验证提问）；
        joined=False 返回坐席接着对 Waitless 说的台词，以需要回应的一句结尾。"""
        raise NotImplementedError

    def step_log(self) -> list[str]:
        raise NotImplementedError

    def confirmation_number(self) -> str | None:
        """confirm 动作按 confirmation_format（如 LE-{5d}）生成。缺口 B-G-7（定稿 seeded_by_scenario）：以 scenario_id 为种子
        确定性生成，同一场景每次运行都相同；确认那句台词用 {confirmation_number} 占位，号码原样出现在
        CompanyLine.text 里，同时放进 CompanyLine.confirmation_number。"""
        raise NotImplementedError
