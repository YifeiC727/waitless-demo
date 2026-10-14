"""Deterministic company turns for live-sim; configuration and energy endpoints."""

from collections.abc import Mapping
from dataclasses import dataclass

from waitless.company.config import ScenarioError
from waitless.models import CompanyConfig, Scenario, ScenarioStep

AGENT_MODE_ENV = "WAITLESS_COMPANY_AGENT_MODE"
AGENT_MODES: tuple[str, ...] = ("sonic", "scripted")
PLACEHOLDER_SPEECH = "[caller speech]"


@dataclass(frozen=True)
class ScriptedStep:
    reply_end_silence_ms: int
    polly_voice: str


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


def check_scripted_steps(scenario: Scenario, company: CompanyConfig) -> None:
    for name, step in scenario.steps.items():
        if step.kind in {"ai_agent", "live_rep"}:
            try:
                scripted_step(step, company)
            except ScenarioError as exc:
                raise ScenarioError(f"Step {name}: {exc}") from exc
