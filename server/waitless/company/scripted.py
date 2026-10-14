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

    def voice_for(self, line: CompanyLine) -> str:
        if line.kind == "speech":
            return scripted_step(self._scenario.steps[line.step_id], self._company).polly_voice
        return self._company.voices.ivr_polly

    def line_started(self, line: CompanyLine) -> None:
        self.lines_replaced()

    def line_finished(self, line: CompanyLine, now_ms: int) -> None:
        self.lines_replaced()
        expect = line.expect
        if (self._engine.ended or line.kind != "speech" or expect is None
                or expect.intent == "wait" or (expect.intent or "").startswith("press_field:")):
            return
        self._step_id = line.step_id
        self._intent = expect.intent
        if expect.intent == "request_handoff" or expect.action == "handoff":
            self._phase = "announce"
            silence = self._config.handoff.announce_idle_ms
        else:
            self._phase = "reply"
            silence = scripted_step(self._scenario.steps[line.step_id], self._company).reply_end_silence_ms
        self._waiter.arm(end_silence_ms=silence, now_ms=now_ms)

    def lines_replaced(self) -> None:
        self._waiter.disarm()
        self._phase = "idle"
        self._in_handoff_window = False
        self._step_id = ""
        self._intent = None

    def feed(self, pcm16: bytes, now_ms: int) -> bool:
        return self._waiter.feed(pcm16, now_ms)

    def poll(self, now_ms: int) -> list[CompanyLine]:
        if not self._waiter.due(now_ms):
            return []
        logger = logging.getLogger(__name__)
        if self._phase == "announce":
            self._in_handoff_window = True
            self._phase = "window"
            self._waiter.arm(end_silence_ms=scripted_step(
                self._scenario.steps[self._step_id], self._company).reply_end_silence_ms, now_ms=now_ms)
            logger.info("scripted handoff window opened step=%s", self._step_id)
            return []
        phase, step_id, intent = self._phase, self._step_id, self._intent
        self.lines_replaced()
        if phase == "window":
            logger.info("scripted handoff window closed step=%s", step_id)
            self._engine.on_handoff(True)  # H1: holder verification lines are not played.
            return self._engine.on_handoff(False)
        logger.info("scripted reply end step=%s intent=%s", step_id, intent)
        return self._engine.on_speech(PLACEHOLDER_SPEECH)

    @property
    def in_handoff_window(self) -> bool:
        return self._in_handoff_window

    @property
    def phase(self) -> str:
        return self._phase
