"""Deterministic company turns for live-sim; configuration and energy endpoints."""

from collections.abc import Mapping
from dataclasses import dataclass
import logging

from waitless.company.config import ScenarioError
from waitless.audio.codec import pcm16_rms_dbfs
from waitless.company.engine import ScenarioEngine
from waitless.models import CompanyConfig, CompanyLine, EngineConfig, Scenario, ScenarioStep

AGENT_MODE_ENV = "WAITLESS_COMPANY_AGENT_MODE"
AGENT_MODES: tuple[str, ...] = ("sonic", "scripted")
PLACEHOLDER_SPEECH = "[caller speech]"


@dataclass(frozen=True)
class ScriptedStep:
    reply_end_silence_ms: int
    polly_voice: str


@dataclass(frozen=True)
class ScriptedHandoff:
    holder_prompt_ms: int
    holder_max_asks: int
    resume_wait_ms: int


def agent_mode_from_env(env: Mapping[str, str]) -> str:
    return check_agent_mode((env.get(AGENT_MODE_ENV) or "").strip() or "sonic")


def check_agent_mode(mode: str) -> str:
    if mode not in AGENT_MODES:
        raise ScenarioError(f"{AGENT_MODE_ENV} must be sonic or scripted")
    return mode


def scripted_step(step: ScenarioStep, company: CompanyConfig) -> ScriptedStep:
    fields = getattr(step, "scripted", None)
    if not isinstance(fields, Mapping):
        raise ScenarioError(f"Step {step.kind}: scripted must be a mapping")
    silence = fields.get("reply_end_silence_ms")
    if type(silence) is not int or silence <= 0:
        raise ScenarioError(f"Step {step.kind}: reply_end_silence_ms must be a positive integer")
    voice = fields.get("polly_voice", company.voices.ivr_polly)
    if not isinstance(voice, str) or not voice.strip():
        raise ScenarioError(f"Step {step.kind}: polly_voice must be a nonempty string")
    return ScriptedStep(silence, voice)


def scripted_handoff(step: ScenarioStep, company: CompanyConfig) -> ScriptedHandoff | None:
    if not any(line.expect is not None and (
            line.expect.intent == "request_handoff" or line.expect.action == "handoff")
            for line in step.text_script or []):
        return None
    fields = getattr(step, "scripted", None)
    values = []
    for name in ("holder_prompt_ms", "holder_max_asks", "resume_wait_ms"):
        value = fields.get(name) if isinstance(fields, Mapping) else None
        if type(value) is not int or value <= 0:
            raise ScenarioError(f"Step {step.kind}: {name} must be a positive integer")
        values.append(value)
    return ScriptedHandoff(*values)


def check_scripted_steps(scenario: Scenario, company: CompanyConfig) -> None:
    for name, step in scenario.steps.items():
        if step.kind in {"ai_agent", "live_rep"}:
            try:
                scripted_step(step, company)
                scripted_handoff(step, company)
                for action in step.actions or []:
                    if action.do != "require_account_holder":
                        continue
                    extra = action.model_extra or {}
                    if "keypad_digits" in extra:
                        digits = action.keypad_digits
                        if type(digits) is not int or digits <= 0:
                            raise ScenarioError("keypad_digits must be a positive integer")
                        ok_line = getattr(action, "keypad_ok_line", None)
                        if not isinstance(ok_line, str) or not ok_line.strip():
                            raise ScenarioError("keypad_ok_line must be a nonempty string")
                    if "keypad_choice" in extra:
                        choice = action.keypad_choice
                        lines = getattr(action, "choice_lines", None)
                        if (not isinstance(choice, Mapping) or not choice
                                or any(not isinstance(k, str) or len(k) != 1 or k not in "0123456789*#"
                                       for k in choice)):
                            raise ScenarioError("keypad_choice must map single keypad keys")
                        if (not isinstance(lines, Mapping) or not lines
                                or any(not isinstance(v, str) or not v.strip() for v in choice.values())
                                or len(set(choice.values())) != len(choice)
                                or set(choice.values()) != set(lines)
                                or any(not isinstance(v, str) or not v.strip() for v in lines.values())):
                            raise ScenarioError("keypad_choice and nonempty choice_lines must correspond one to one")
                        close = getattr(action, "not_agreed_close", None)
                        if (not isinstance(close, Mapping) or not isinstance(close.get("line"), str)
                                or not close["line"].strip() or close.get("outcome") != "needs_follow_up"):
                            raise ScenarioError("not_agreed_close needs a line and needs_follow_up outcome")
            except ScenarioError as exc:
                raise ScenarioError(f"Step {name}: {exc}") from exc


class ReplyWaiter:
    """Arm after playback; require new voiced input before a silence endpoint."""

    def __init__(self, *, threshold_dbfs: float) -> None:
        self._threshold_dbfs = threshold_dbfs
        self._armed = False
        self._armed_at = 0
        self._last_voice: int | None = None
        self._end_silence_ms = 0

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def heard(self) -> bool:
        return self._armed and self._last_voice is not None

    def arm(self, *, end_silence_ms: int, now_ms: int) -> None:
        self._armed = True
        self._armed_at = now_ms
        self._end_silence_ms = end_silence_ms
        self._last_voice = None

    def disarm(self) -> None:
        self._armed = False
        self._last_voice = None

    def feed(self, pcm16: bytes, now_ms: int) -> bool:
        voiced = pcm16_rms_dbfs(pcm16) > self._threshold_dbfs
        if voiced and self._armed and now_ms >= self._armed_at:
            self._last_voice = now_ms
        return voiced

    def due(self, now_ms: int) -> bool:
        return (self._armed and self._last_voice is not None
                and now_ms - self._last_voice >= self._end_silence_ms)


class ScriptedTurns:
    """Translate completed playback and inbound energy into ScenarioEngine input."""

    def __init__(self, *, engine: ScenarioEngine, scenario: Scenario, company: CompanyConfig,
                 engine_config: EngineConfig) -> None:
        self._engine = engine
        self._scenario = scenario
        self._company = company
        self._config = engine_config
        self._waiter = ReplyWaiter(threshold_dbfs=engine_config.vad.threshold_dbfs)
        self._phase = "idle"
        self._in_handoff_window = False
        self._step_id = ""
        self._intent: str | None = None
        self._handoff: ScriptedHandoff | None = None
        self._phase_started_ms = 0
        self._questions: list[CompanyLine] | None = None
        self._ask_count = 0
        self._action_index = 0
        self._now_ms = 0
        self._deferred_lines: list[CompanyLine] = []
        self._holder_lines: list[CompanyLine] = []
        engine._holder_strict = True

    def voice_for(self, line: CompanyLine) -> str:
        if line.kind == "speech":
            return scripted_step(self._scenario.steps[line.step_id], self._company).polly_voice
        return self._company.voices.ivr_polly

    def line_started(self, line: CompanyLine) -> None:
        if self._is_question(line):
            self._waiter.disarm()
            self._engine._holder_line_started(line)
            return
        self.lines_replaced()

    def line_finished(self, line: CompanyLine, now_ms: int) -> None:
        self._now_ms = now_ms
        if self._is_question(line):
            # Every private line may finish separately. Only the final line arms
            # input; an OK line must not close the window before the next question.
            if line is not self._holder_lines[-1]:
                return
            self._enter_window_phase("resume_wait" if self._engine._holder_action is None
                                     else "holder_answer", now_ms)
            return
        self.lines_replaced()
        expect = line.expect
        if (self._engine.ended or line.kind != "speech" or expect is None
                or expect.intent == "wait" or (expect.intent or "").startswith("press_field:")):
            return
        self._step_id = line.step_id
        self._intent = expect.intent
        if expect.intent == "request_handoff" or expect.action == "handoff":
            self._handoff = scripted_handoff(self._scenario.steps[line.step_id], self._company)
            if self._handoff is None:
                raise ScenarioError(f"Step {line.step_id}: missing scripted handoff configuration")
            self._phase = "announce"
            silence = self._config.handoff.announce_idle_ms
        else:
            self._phase = "reply"
            silence = scripted_step(self._scenario.steps[line.step_id], self._company).reply_end_silence_ms
        self._waiter.arm(end_silence_ms=silence, now_ms=now_ms)

    def lines_replaced(self) -> None:
        if self._in_handoff_window:
            # Discarding private playback also revokes its input window.
            self._engine._holder_prompt_open = False
        self._waiter.disarm()
        self._phase = "idle"
        self._in_handoff_window = False
        self._step_id = ""
        self._intent = None
        self._handoff = None
        self._phase_started_ms = 0
        self._questions = None
        self._ask_count = 0
        self._holder_lines = []
        self._deferred_lines = []
        self._action_index = 0

    def _is_question(self, line: CompanyLine) -> bool:
        return self._phase == "asking" and any(line is q for q in self._holder_lines)

    def holder_keypad_lines(self, lines: list[CompanyLine]) -> None:
        """Keep questions and feedback in the same private playback window."""
        if not self._in_handoff_window or not self.counts_holder_audio:
            return
        if not lines:
            if self._engine._holder_action is None and self._phase != "asking":
                self._enter_window_phase("resume_wait", self._now_ms)
            return
        if self._engine._holder_index != self._action_index and self._engine._holder_action is not None:
            self._action_index = self._engine._holder_index
            self._questions = [self._engine._holder_questions[self._action_index]]
            self._ask_count = 1
            logging.getLogger(__name__).info("scripted handoff ask n=%s step=%s", self._ask_count, self._step_id)
        self._holder_lines = list(lines)
        self._waiter.disarm()
        self._phase = "asking"

    def feed_keypad_tone(self, now_ms: int) -> None:
        """Exclude keypad PCM from speech and allow time for the ongoing entry."""
        self._now_ms = now_ms
        if self._in_handoff_window and self._phase in {"holder_wait", "holder_answer"}:
            self._enter_window_phase(self._phase, now_ms)

    def _enter_window_phase(self, phase: str, now_ms: int) -> None:
        self._phase = phase
        self._now_ms = now_ms
        self._phase_started_ms = now_ms
        self._waiter.arm(end_silence_ms=scripted_step(
            self._scenario.steps[self._step_id], self._company).reply_end_silence_ms, now_ms=now_ms)

    def _ask(self, now_ms: int) -> list[CompanyLine]:
        if self._questions is None:
            self._questions = self._engine.on_handoff(True)
        self._holder_lines = list(self._questions)
        self._ask_count += 1
        self._waiter.disarm()
        self._phase = "asking"
        logging.getLogger(__name__).info("scripted handoff ask n=%s step=%s", self._ask_count, self._step_id)
        if not self._questions:
            self._enter_window_phase("holder_answer", now_ms)
        return list(self._questions)

    def feed(self, pcm16: bytes, now_ms: int) -> bool:
        self._now_ms = now_ms
        if self._phase == "holder_answer":
            action = self._engine._holder_action
            if action is not None and getattr(action, "keypad_choice", None):
                # Voice cannot consent or postpone the choice's no-input timer.
                return pcm16_rms_dbfs(pcm16) > self._config.vad.threshold_dbfs
            if self._waiter.due(now_ms):
                # Preserve a boundary frame for the next phase without treating
                # it as an answer to a question that has not played yet.
                self._deferred_lines.extend(self._complete_answer(now_ms))
        return self._waiter.feed(pcm16, now_ms)

    def _complete_answer(self, now_ms: int) -> list[CompanyLine]:
        logging.getLogger(__name__).info("scripted holder answered")
        lines = self._engine.holder_input(speech=PLACEHOLDER_SPEECH)
        if lines:
            self.holder_keypad_lines(lines)
        else:
            self._enter_window_phase("resume_wait", now_ms)
        return lines

    def poll(self, now_ms: int) -> list[CompanyLine]:
        self._now_ms = now_ms
        if self._deferred_lines:
            lines, self._deferred_lines = self._deferred_lines, []
            return lines
        if self._phase in {"idle", "asking"}:
            return []
        logger = logging.getLogger(__name__)
        if self._phase == "announce":
            if not self._waiter.due(now_ms):
                return []
            self._in_handoff_window = True
            # Joining belongs to the window, even if Return arrives before
            # synthesis or the first private question has started.
            self._questions = self._engine.on_handoff(True)
            self._enter_window_phase("holder_wait", now_ms)
            logger.info("scripted handoff window opened step=%s", self._step_id)
            return []
        if self._phase in {"holder_wait", "holder_answer", "resume_wait"}:
            handoff = self._handoff
            if handoff is None:
                raise ScenarioError(f"Step {self._step_id}: missing scripted handoff configuration")
            due = self._waiter.due(now_ms)
            quiet = not self._waiter.heard
            elapsed = now_ms - self._phase_started_ms
            if self._phase == "holder_wait":
                if due or (quiet and elapsed >= handoff.holder_prompt_ms):
                    return self._ask(now_ms)
                return []
            if self._phase == "holder_answer":
                if due:
                    return self._complete_answer(now_ms)
                elif quiet and elapsed >= handoff.holder_prompt_ms:
                    if self._ask_count < handoff.holder_max_asks:
                        return self._ask(now_ms)
                    logger.info("scripted holder no answer")
                    self._engine._holder_unanswered()
                else:
                    return []
                self._enter_window_phase("resume_wait", now_ms)
                return []
            if due or (quiet and elapsed >= handoff.resume_wait_ms):
                step_id = self._step_id
                self.lines_replaced()
                logger.info("scripted handoff window closed step=%s reason=%s", step_id,
                            "resume" if due else "idle")
                return self._engine.on_handoff(False)
            return []
        if not self._waiter.due(now_ms):
            return []
        step_id, intent = self._step_id, self._intent
        self.lines_replaced()
        logger.info("scripted reply end step=%s intent=%s", step_id, intent)
        return self._engine.on_speech(PLACEHOLDER_SPEECH)

    @property
    def in_handoff_window(self) -> bool:
        return self._in_handoff_window

    @property
    def counts_holder_audio(self) -> bool:
        return self._phase in {"holder_wait", "asking", "holder_answer"}

    @property
    def phase(self) -> str:
        return self._phase
