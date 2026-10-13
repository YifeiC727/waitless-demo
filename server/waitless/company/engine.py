"""场景引擎：按 steps 图推进并逐步记录（B 线；tech §6.10；AC-82、AC-83）。

公开接口（§7）：ScenarioEngine。文本公司（company/text.py）与公司 bot（company/bot.py）共用它。
"""

import hashlib
import random
import re

from waitless.company.config import ScenarioError
from waitless.models import DEAD_END_TARGET, CompanyConfig, CompanyLine, Scenario

__all__ = ["ScenarioEngine"]


class ScenarioEngine:
    """baseline 的步骤顺序：greeting → main_menu → service_menu → hold → agent → rep（AC-82）。

    缺口 B-G-5（定稿 same_batch，暂定）：文本模式里 hold 不占时间，hold 那句之后同一批接着给出下一步的台词；
    不需要回应的台词（expect.intent=wait）之后也在同一批继续，直到一句需要 Waitless 回应的台词或挂断为止
    （interfaces.CompanyEngine 的类说明同此）。
    """

    def __init__(self, scenario: Scenario, company: CompanyConfig) -> None:
        if scenario.company_id != company.company_id:
            raise ScenarioError("Scenario and company identifiers differ")
        self._scenario = scenario
        self._company = company
        self._step = ""
        self._ended = False
        self._log: list[str] = []
        self._index = 0
        self._pending: CompanyLine | None = None
        self._timeouts = 0
        self._digits = ""
        self._handoff_joined = False
        self._confirmation: str | None = None

    @property
    def current_step(self) -> str:
        return self._step

    @property
    def ended(self) -> bool:
        return self._ended

    def start(self) -> list[CompanyLine]:
        if self._step or self._ended:
            return []
        return self._enter("greeting")

    def on_digits(self, digits: str) -> list[CompanyLine]:
        """菜单步按 options 跳转；ai_agent 的 verifies 字段（如 account_number）按 customers 核对（R25）。
        目标是 models.DEAD_END_TARGET 时（A-17）：先给一句 CompanyLine(step_id="dead_end", kind="prompt",
        role="ivr", text=company.dead_end_text)，再给 kind="hangup" 的一句，之后 ended=True；
        按错键的通话在评测里是 failed 或 needs_follow_up。next / on_done 指向它时同样处理。"""
        if self._ended or not self._step or not digits:
            return []
        step = self._scenario.steps[self._step]
        if step.kind == "menu":
            target = (step.options or {}).get(digits)
            if target is None:
                return [self._menu_line()]
            self._timeouts = 0
            return self._enter(target)
        pending = self._pending
        intent = pending.expect.intent if pending and pending.expect else None
        if not intent or not intent.startswith("press_field:"):
            return []
        field = intent.removeprefix("press_field:")
        self._digits += digits
        terminated = "#" in self._digits
        value = self._digits.split("#", 1)[0]
        candidates = [str(getattr(c, field, "")) for c in self._company.customers]
        if value in candidates and (terminated or "pound" not in pending.text.lower()):
            self._digits = ""
            return self._advance()
        if terminated or not any(c.startswith(value) for c in candidates):
            self._digits = ""
            return [pending]
        return []

    def on_speech(self, text: str) -> list[CompanyLine]:
        if self._ended or self._pending is None or not text.strip():
            return []
        expect = self._pending.expect
        if expect is None or (expect.intent or "").startswith("press_field:"):
            return []
        if expect.intent == "request_handoff" or expect.action == "handoff":
            # Announcement alone cannot verify the holder or resume the script.
            return []
        return self._advance()

    def on_timeout(self) -> list[CompanyLine]:
        """菜单等待 company.menu.timeout_sec；连续 max_timeouts 次超时 → kind=hangup（AC-83）。"""
        if self._ended or not self._step or self._scenario.steps[self._step].kind != "menu":
            return []
        self._timeouts += 1
        if self._timeouts >= self._company.menu.max_timeouts:
            return self._finish()
        return [self._menu_line()]

    def on_handoff(self, joined: bool) -> list[CompanyLine]:
        """缺口 B-G-8（定稿 rep_lines_during_handoff）：joined=True 返回坐席对账户持有人说的几句（如验证提问）；
        joined=False 返回坐席接着对 Waitless 说的台词，以需要回应的一句结尾。"""
        if self._ended or self._pending is None:
            return []
        expect = self._pending.expect
        if expect is None or not (expect.intent == "request_handoff" or expect.action == "handoff"):
            return []
        if joined:
            if self._handoff_joined:
                return []
            self._handoff_joined = True
            step = self._scenario.steps[self._step]
            actions = [a for a in step.actions or [] if a.do == "require_account_holder"]
            return [CompanyLine(step_id=self._step, kind="speech", role="rep",
                                text=a.line or f"Please confirm your {a.verification or 'identity'}.")
                    for a in actions] or [CompanyLine(step_id=self._step, kind="speech", role="rep",
                                                     text="Please verify your identity.")]
        if not self._handoff_joined:
            return []
        self._handoff_joined = False
        return self._advance()

    def step_log(self) -> list[str]:
        return list(self._log)

    def confirmation_number(self) -> str | None:
        """confirm 动作按 confirmation_format（如 LE-{5d}）生成。缺口 B-G-7（定稿 seeded_by_scenario）：以 scenario_id 为种子
        确定性生成，同一场景每次运行都相同；确认那句台词用 {confirmation_number} 占位，号码原样出现在
        CompanyLine.text 里，同时放进 CompanyLine.confirmation_number。"""
        return self._confirmation

    def _finish(self) -> list[CompanyLine]:
        self._ended = True
        self._pending = None
        return [CompanyLine(step_id=self._step, kind="hangup", role="ivr", text="")]

    def _menu_line(self) -> CompanyLine:
        return CompanyLine(step_id=self._step, kind="menu", role="ivr",
                           text=self._scenario.steps[self._step].text or "")

    def _advance(self) -> list[CompanyLine]:
        self._pending = None
        return self._drain()

    def _enter(self, target: str) -> list[CompanyLine]:
        return self._drain(target)

    def _drain(self, target: str | None = None) -> list[CompanyLine]:
        lines: list[CompanyLine] = []
        automatic: set[tuple[str, int]] = set()
        while not self._ended:
            if target is not None:
                self._step = target
                self._log.append(target)
                self._index = 0
                self._digits = ""
                self._pending = None
                if target == DEAD_END_TARGET:
                    lines.append(CompanyLine(step_id=target, kind="prompt", role="ivr",
                                             text=self._company.dead_end_text))
                    return lines + self._finish()
                if target not in self._scenario.steps:
                    raise ScenarioError(f"Missing target {target!r}")
                target = None
            cursor = (self._step, self._index)
            if cursor in automatic:
                raise ScenarioError("Scenario contains a cycle without waiting for input")
            automatic.add(cursor)
            step = self._scenario.steps[self._step]
            if step.kind == "menu":
                return lines + [self._menu_line()]
            if step.kind in {"prompt", "hold"}:
                lines.append(CompanyLine(step_id=self._step, kind=step.kind, role="ivr", text=step.text or ""))
                target = step.next
                continue
            script = step.text_script or []
            if self._index >= len(script):
                target = step.next
                if target is None and step.on_done:
                    event = getattr(step, "done_event", None)
                    if event is not None:
                        target = step.on_done.get(event)
                        if target is None:
                            raise ScenarioError("done_event has no on_done transition")
                    elif len(step.on_done) == 1:
                        target = next(iter(step.on_done.values()))
                    else:
                        raise ScenarioError("Multiple on_done transitions need done_event")
                if target is None:
                    return lines + self._finish()
                continue
            entry = script[self._index]
            self._index += 1
            confirmation = None
            text = entry.say
            if "{confirmation_number}" in text:
                confirmation = self._make_confirmation()
                text = text.replace("{confirmation_number}", confirmation)
            line = CompanyLine(step_id=self._step, kind="speech",
                               role="agent" if step.kind == "ai_agent" else "rep", text=text,
                               expect=entry.expect, confirmation_number=confirmation)
            lines.append(line)
            if entry.expect.intent != "wait":
                self._pending = line
                return lines
        return lines

    def _make_confirmation(self) -> str:
        if self._confirmation is not None:
            return self._confirmation
        formats = [a.confirmation_format for a in self._scenario.steps[self._step].actions or []
                   if a.do == "confirm" and a.confirmation_format]
        if len(formats) != 1:
            raise ScenarioError("Confirmation needs one confirm action with confirmation_format")
        seed = int.from_bytes(hashlib.sha256(self._scenario.scenario_id.encode()).digest(), "big")
        rng = random.Random(seed)
        def digits(match: re.Match[str]) -> str:
            width = int(match.group(1))
            if not 1 <= width <= 32:
                raise ScenarioError("Confirmation digit width must be between 1 and 32")
            return f"{rng.randrange(10 ** width):0{width}d}"
        self._confirmation = re.sub(r"\{(\d+)d\}", digits, formats[0])
        return self._confirmation
