"""场景引擎：按 steps 图推进并逐步记录（B 线；tech §6.10；AC-82、AC-83）。

公开接口（§7）：ScenarioEngine。文本公司（company/text.py）与公司 bot（company/bot.py）共用它。
"""

import hashlib
import logging
import random
import re

from waitless.company.config import ScenarioError
from waitless.models import DEAD_END_TARGET, CompanyConfig, CompanyCustomer, CompanyLine, EvalExpect, Scenario

__all__ = ["ScenarioEngine"]


def _digits_only(text: str) -> str:
    return re.sub(r"[^0-9]", "", text)


def _us_phone(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{10}|1[0-9]{10}", value))


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
        self._holder_strict = False  # ScriptedTurns opts out of the text smoke shortcut.
        self._holder_actions = []
        self._holder_questions: list[CompanyLine] = []
        self._holder_index = 0
        self._holder_results: dict[int, str] = {}
        self._holder_invalid: dict[int, int] = {}
        self._holder_touched: set[int] = set()
        self._holder_prompt_open = False
        self._holder_ignore_pound = False
        self._confirmation: str | None = None
        # Live bot state is separate from the deterministic text-script cursor.
        self._bot_details: dict[str, str] = {}
        self._bot_account_verified = False
        self._bot_entry_received = False
        self._bot_holder_requested = False
        self._bot_holder_verified = False
        self._bot_offers: set[str] = set()
        self._bot_answers: dict[str, str] = {}
        self._bot_digits = ""

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
            self._timeouts = 0
            target = (step.options or {}).get(digits)
            if target is None:
                return [self._menu_line()]
            return self._enter(target)
        pending = self._pending
        if self._handoff_joined:
            return self.holder_input(keys=digits)
        intent = pending.expect.intent if pending and pending.expect else None
        if not intent or not intent.startswith("press_field:"):
            return []
        if step.kind == "ai_agent":
            # The script marks when to wait for keys; verifies selects the field.
            field = step.verifies
            if field not in CompanyCustomer.model_fields:
                raise ScenarioError("AI agent verifies must name a customer field")
        else:
            field = intent.removeprefix("press_field:")
        self._digits += digits
        terminated = "#" in self._digits
        value = self._digits.split("#", 1)[0]
        needs_terminator = "pound" in pending.text.lower() or "#" in pending.text
        if value and self._keypad_match(field, value, terminated) and (terminated or not needs_terminator):
            self._digits = ""
            return self._advance()
        if terminated or not self._keypad_may_continue(field, value):
            self._digits = ""
            return [pending]
        return []

    def _keypad_match(self, field: str, value: str, terminated: bool) -> bool:
        return bool(value) and (any(
            value == _digits_only(str(getattr(c, field, ""))) for c in self._company.customers
        ) or (field == "phone_number" and terminated and _us_phone(value)))

    def _keypad_may_continue(self, field: str, value: str) -> bool:
        return any(
            _digits_only(str(getattr(c, field, ""))).startswith(value) for c in self._company.customers
        ) or (field == "phone_number" and bool(re.fullmatch(r"[0-9]{0,11}", value)))

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
        """Return only the current private question, then resume after the holder leaves."""
        if self._ended or self._pending is None:
            return []
        expect = self._pending.expect
        if expect is None or not (expect.intent == "request_handoff" or expect.action == "handoff"):
            return []
        if joined:
            if self._handoff_joined:
                return []
            self._handoff_joined = True
            self._digits = ""
            self._holder_actions = [a for a in self._scenario.steps[self._step].actions or []
                                    if a.do == "require_account_holder"]
            self._holder_questions = [self._holder_question(a) for a in self._holder_actions]
            self._holder_index = 0
            self._holder_results = {}
            self._holder_invalid = {}
            self._holder_touched = set()
            self._holder_prompt_open = not self._holder_strict
            self._holder_ignore_pound = False
            return self._holder_questions[:1] or [CompanyLine(
                step_id=self._step, kind="speech", role="rep", text="Please verify your identity.")]
        if not self._handoff_joined:
            return []
        self._handoff_joined = False
        self._digits = ""
        self._holder_prompt_open = False
        if not self._holder_strict:
            # 本版新增、只限文本引擎、不证明本人同意：Join/Leave smoke
            # assumes unentered private answers are complete. Explicit input wins.
            for i, action in enumerate(self._holder_actions):
                if i not in self._holder_results and i not in self._holder_touched:
                    self._holder_results[i] = "agree" if getattr(action, "keypad_choice", None) else "complete"
        for i, action in enumerate(self._holder_actions):
            if getattr(action, "keypad_choice", None) and self._holder_results.get(i) != "agree":
                self._holder_results.setdefault(i, "unanswered")
                close = action.not_agreed_close
                line = CompanyLine(step_id=self._step, kind="speech", role="rep", text=close["line"],
                                   expect=EvalExpect(intent="wait", outcome=close["outcome"]))
                return [line, *self._finish()]
        return self._advance()

    @property
    def _holder_action(self):
        if self._holder_index < len(self._holder_actions):
            return self._holder_actions[self._holder_index]
        return None

    def _holder_line_started(self, line: CompanyLine) -> None:
        if self._handoff_joined and self._holder_action is not None and (
                line is self._holder_questions[self._holder_index]):
            self._holder_prompt_open = True

    def _holder_complete(self, value: str) -> list[CompanyLine]:
        action = self._holder_action
        self._holder_results[self._holder_index] = value
        self._holder_index += 1
        self._digits = ""
        self._holder_prompt_open = not self._holder_strict
        lines = []
        choice = getattr(action, "keypad_choice", None)
        text = action.choice_lines[value] if choice else getattr(action, "keypad_ok_line", None)
        if text:
            lines.append(CompanyLine(step_id=self._step, kind="speech", role="rep", text=text))
        if self._holder_action is not None:
            lines.append(self._holder_questions[self._holder_index])
        return lines

    def _holder_unanswered(self) -> None:
        for i in range(self._holder_index, len(self._holder_actions)):
            self._holder_results.setdefault(i, "unanswered")
        self._holder_index = len(self._holder_actions)
        self._digits = ""
        self._holder_prompt_open = False

    def holder_input(self, speech=None, keys=None) -> list[CompanyLine]:
        """Consume private holder input only inside the joined window; never retain its content."""
        action = self._holder_action
        if self._ended or not self._handoff_joined or action is None or not self._holder_prompt_open:
            return []
        choice = getattr(action, "keypad_choice", None)
        if choice:
            if keys == "#" and self._holder_ignore_pound:
                self._holder_ignore_pound = False
                return []
            if keys or (speech and speech.strip()):
                self._holder_touched.add(self._holder_index)
            if not keys:
                return []  # Speech/volume cannot answer a keypad choice.
            if len(keys) == 1 and keys in choice:
                logging.getLogger(__name__).info("scripted holder choice recorded")
                return self._holder_complete(choice[keys])
            count = self._holder_invalid.get(self._holder_index, 0) + 1
            self._holder_invalid[self._holder_index] = count
            if count >= 2:
                self._holder_unanswered()
                return []
            self._holder_prompt_open = not self._holder_strict
            return [self._holder_questions[self._holder_index]]
        if speech and speech.strip():
            self._holder_touched.add(self._holder_index)
            return self._holder_complete("complete")
        size = getattr(action, "keypad_digits", None)
        if not keys or size is None:
            return []
        for digit in keys:
            if digit in "0123456789":
                self._holder_touched.add(self._holder_index)
                self._digits += digit
                if len(self._digits) == size:
                    self._holder_ignore_pound = True
                    logging.getLogger(__name__).info("scripted holder keypad entry complete")
                    # Ignore the remainder of this input, including the DOB terminator.
                    return self._holder_complete("complete")
            elif digit == "#":
                self._digits = ""
                logging.getLogger(__name__).info("scripted holder keypad wrong length")
                return [self._holder_questions[self._holder_index]]
        return []

    def _holder_question(self, action) -> CompanyLine:
        return CompanyLine(step_id=self._step, kind="speech", role="rep",
                           text=action.line or f"Please confirm your {action.verification or 'identity'}.")

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

    def _bot_on_digits(self, digits: str) -> bool:
        if self._ended or not self._step:
            return False
        step = self._scenario.steps[self._step]
        if step.kind != "ai_agent" or not step.verifies:
            return False
        if step.verifies not in CompanyCustomer.model_fields:
            raise ScenarioError("AI agent verifies must name a customer field")
        completed = False
        for digit in digits:
            if digit == "#":
                completed = True
                self._bot_entry_received = True
                self._bot_account_verified = bool(self._bot_digits) and self._keypad_match(
                    step.verifies, self._bot_digits, True)
                self._bot_digits = ""
            elif digit in "0123456789*":
                self._bot_digits = (self._bot_digits + digit)[-64:]
        return completed

    def _bot_tool(self, name: str, arguments: dict) -> dict:
        """Apply live tools with scenario checks; never consume text-script entries."""
        if self._ended or not self._step:
            return {"ok": False, "error": "call_inactive"}
        step = self._scenario.steps[self._step]
        def error(reason: str) -> dict:
            return {"ok": False, "error": reason}
        def nonempty(key: str) -> str | None:
            value = arguments.get(key)
            return value.strip() if isinstance(value, str) and value.strip() else None
        if name == "record_detail":
            field, value = nonempty("field"), nonempty("value")
            if step.kind != "ai_agent" or field not in (step.collects or []) or not value:
                return error("invalid_detail")
            self._bot_details[field] = value
        elif name == "verify_account":
            if step.kind != "ai_agent":
                return error("account_not_verified_by_keypad")
            if not self._bot_account_verified:
                result = error("account_not_verified_by_keypad")
                if self._bot_entry_received:
                    return {**result, "status": "mismatch", "message":
                            f"The keypad entry did not match; ask the caller to re-enter the {step.verifies.replace('_', ' ')} "
                            "ending with the pound key."}
                return {**result, "status": "awaiting_keypad_entry", "message":
                        "No keypad entry ending with the pound key has been received; ask the "
                        f"caller to enter the {step.verifies.replace('_', ' ')} ending with the pound key, then wait "
                        "for the phone system keypad notice before checking verification and "
                        "do not describe verification as failed."}
        elif name == "transfer_to_representative":
            if step.kind != "ai_agent":
                return error("wrong_step")
            event = nonempty("event") or getattr(step, "done_event", None)
            if event is None and len(step.on_done or {}) == 1:
                event = next(iter(step.on_done))
            target = (step.on_done or {}).get(event)
            if not target:
                return error("unknown_transition")
            if any(f not in self._bot_details for f in step.collects or []) or (
                step.verifies and not self._bot_account_verified):
                return error("agent_requirements_incomplete")
            self._enter(target)
        elif name in {"request_account_holder", "account_holder_verified", "present_offer",
                      "record_answer", "issue_confirmation"}:
            if step.kind != "live_rep":
                return error("wrong_step")
            actions = step.actions or []
            requires_holder = any(a.do == "require_account_holder" for a in actions)
            if name == "request_account_holder":
                if not requires_holder:
                    return error("holder_not_required")
                self._bot_holder_requested = True
            elif name == "account_holder_verified":
                if not self._bot_holder_requested:
                    return error("holder_not_requested")
                self._bot_holder_verified = True
            else:
                if requires_holder and not self._bot_holder_verified:
                    return error("holder_not_verified")
                tags = {a.tag for a in actions if a.do == "offer" and a.tag}
                if name in {"present_offer", "record_answer"}:
                    tag = nonempty("tag")
                    if tag not in tags:
                        return error("unknown_offer")
                    if name == "present_offer":
                        self._bot_offers.add(tag)
                    else:
                        answer = nonempty("answer")
                        if tag not in self._bot_offers or not answer:
                            return error("offer_not_answered")
                        self._bot_answers[tag] = answer
                else:
                    if not tags.issubset(self._bot_answers):
                        return error("offers_incomplete")
                    return {"ok": True, "confirmation_number": self._make_confirmation()}
        elif name == "end_call":
            self._finish()
        else:
            return error("unknown_tool")
        return {"ok": True, "step": self._step}
