"""I2A-2: trace and DP-C2 receive only released speech, never controls."""

from types import SimpleNamespace

import pytest

from call_engine.conftest import compiled, deps, engine, trace, spec, line_number, settings, gate, store
from call_engine._support import (
    FakeActions, FakeMediaGate, FakeTurnDetector, LiveCallHarness, make_result,
    record_decision, settle, patch_menu_parser,
)
from lane_D.test_guard_round_regressions import env, emit
from lane_D.test_r7_ivr_hold import register_speech
from fakes import SonicResponse
from waitless.call.live import TranscriptTap
from waitless import models as m


def event(text, *, response=1, role="ASSISTANT"):
    return m.ProviderTextEvent(role=role, stage="FINAL", text=text, response=response,
        t_ms=2074, stop_reason="END_TURN")


def tap_env(env, deps, *, with_guard=True):
    actions, detector, media = FakeActions(), FakeTurnDetector(), FakeMediaGate()
    kwargs = {"guard": env.guard} if with_guard else {}
    tap = TranscriptTap(deps=deps, spec=env.spec, turn_detector=detector,
        actions=actions, media_gate=media, **kwargs)
    return SimpleNamespace(tap=tap, actions=actions, detector=detector, media=media)


def utterances(env):
    return [e.transcript for e in env.trace.of_type("waitless_utterance")]


async def test_discarded_final_is_absent_from_trace_and_actions(env, deps):
    tap = tap_env(env, deps)
    async with env.chain:
        await register_speech(env)
        await emit(env)
        await env.guard.discard(2, "press")
        await tap.tap.on_provider_text(event("An unheard answer."))
        assert utterances(env) == []
        assert tap.actions.calls == []


@pytest.mark.parametrize("state,text", [
    ("injected", "Injected answer."), ("ending", "Goodbye."),
    ("conversation", "One moment please."),
])
async def test_allowed_final_is_recorded_and_checked(env, deps, state, text):
    tap = tap_env(env, deps)
    async with env.chain:
        env.guard.set_state(state)
        if state == "conversation":
            text = env.compiled.stall_phrases[0]
        await emit(env, text)
        ev = event(text)
        await tap.tap.on_provider_text(ev)
        assert utterances(env) == [text]
        assert tap.actions.calls == [("on_assistant_final", (ev,), {})]


@pytest.mark.parametrize("resolve", ["release", "discard", "timeout", "state_change"])
async def test_held_finals_wait_in_order_or_disappear(env, deps, resolve):
    tap = tap_env(env, deps)
    async with env.chain:
        await register_speech(env)
        await emit(env)
        finals = [event("First final fragment."), event("Second final fragment.")]
        for ev in finals:
            await tap.tap.on_provider_text(ev)
        assert utterances(env) == []
        assert tap.actions.calls == []
        if resolve == "release":
            env.guard.set_state("conversation")
            assert await env.guard.release(2)
        elif resolve == "discard":
            await env.guard.discard(2, "press")
        elif resolve == "timeout":
            await env.clock.advance(env.engine.guard.held_response_max_ms)
        else:
            env.guard.set_state("ivr")
        await settle()
        # E77: one released response is one utterance, preserving every character.
        combined = finals[-1].model_copy(update={"text": "".join(ev.text for ev in finals)})
        expected = [combined] if resolve == "release" else []
        assert utterances(env) == [ev.text for ev in expected]
        assert tap.actions.calls == [("on_assistant_final", (ev,), {}) for ev in expected]


async def test_final_after_release_records_once(env, deps):
    tap = tap_env(env, deps)
    async with env.chain:
        await register_speech(env)
        await emit(env)
        env.guard.set_state("conversation")
        await env.guard.release(2)
        await tap.tap.on_provider_text(event("Released answer."))
        assert not await env.guard.release(2)
        assert utterances(env) == ["Released answer."]
        assert len(tap.actions.calls) == 1


@pytest.mark.parametrize("role", ["USER", "ASSISTANT"])
@pytest.mark.parametrize("text", ['{ "interrupted" : true }', '\n {"interrupted":true}  '])
async def test_control_final_never_routes_even_without_guard(env, deps, role, text):
    tap = tap_env(env, deps, with_guard=False)
    await tap.tap.on_provider_text(event(text, role=role, response=None))
    assert utterances(env) == []
    assert tap.actions.calls == []
    assert tap.detector.calls == []
    real = event("A real final.", response=None)
    await tap.tap.on_provider_text(real)
    assert utterances(env) == [real.text]
    assert tap.actions.calls == [("on_assistant_final", (real,), {})]


@pytest.mark.parametrize("text", ['{"interrupted":false}', '{"interrupted":true,"other":1}', '{"interrupted":1}'])
async def test_only_exact_boolean_control_object_is_filtered(env, deps, text):
    tap = tap_env(env, deps, with_guard=False)
    await tap.tap.on_provider_text(event(text, response=None))
    assert utterances(env) == [text]


@pytest.mark.parametrize("with_guard,response", [(False, 1), (True, None)])
async def test_legacy_final_without_guard_or_response_still_routes(env, deps, with_guard, response):
    tap = tap_env(env, deps, with_guard=with_guard)
    await tap.tap.on_provider_text(event("Legacy final.", response=response))
    assert utterances(env) == ["Legacy final."]
    assert len(tap.actions.calls) == 1


@pytest.mark.parametrize("state", ["ivr", "injected", "handoff"])
async def test_final_before_queued_response_frames_resolves_with_that_response(env, deps, state):
    tap = tap_env(env, deps)
    async with env.chain:
        await register_speech(env)
        env.guard.set_state(state)
        await tap.tap.on_provider_text(event("Early final."))
        assert utterances(env) == []
        await emit(env)
        if state == "ivr":
            assert utterances(env) == []
            env.guard.set_state("conversation")
            assert await env.guard.release(2)
        expected = [] if state == "handoff" else ["Early final."]
        assert utterances(env) == expected
        assert len(tap.actions.calls) == len(expected)


async def test_deferred_final_cannot_write_inside_handoff(env, deps):
    tap = tap_env(env, deps)
    async with env.chain:
        await register_speech(env)
        await emit(env)
        await tap.tap.on_provider_text(event("Held before handoff."))
        tap.media.in_handoff = True
        env.guard.set_state("conversation")
        assert await env.guard.release(2)
        assert utterances(env) == []
        assert tap.actions.calls == []


async def test_live_decision_transcript_excludes_unheard_and_control_text(
    monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store, engine,
):
    patch_menu_parser(monkeypatch)
    gate.round_script.extend([make_result("press", digits="1", call_phase="ivr_menu"),
        make_result("wait", call_phase="ai_agent")])
    h = LiveCallHarness(monkeypatch=monkeypatch, settings=settings, spec=spec,
        clock=fake_clock, trace=trace, gate=gate, sonic=make_fake_sonic(), carrier=fake_carrier, store=store)
    try:
        await h.start()
        await h.connect_line()
        await h.sonic_says(SonicResponse(text="Unheard menu answer.", audio_ms=120))
        await h.company_says("For billing press 1.", silence_ms=engine.turns.menu_end_silence_ms + engine.turns.transcript_grace_ms)
        await h.sonic._text_event("ASSISTANT", "FINAL", '{ "interrupted" : true }', "END_TURN", 1)
        await h.company_says("How can I help?", silence_ms=engine.turns.turn_end_silence_ms + engine.turns.transcript_grace_ms)
        assert len(gate.round_calls) == 2
        assert [line.speaker for line in gate.round_calls[-1][1].transcript] == ["company", "company"]
        assert trace.of_type("waitless_utterance") == []
        result = record_decision(trace, make_result("say", say_text="Please start service."),
            task_id=spec.task_id, call_id=spec.call_id, turn_seq=1)
        await h.call._actions.execute(result)
        await h.sonic._text_event("ASSISTANT", "FINAL", '{"interrupted":true}', "END_TURN", 1)
        assert not gate.said_requests()
        await h.sonic_says(SonicResponse(text="Please start service.", audio_ms=120))
        assert [r.transcript[-1].text for r in gate.said_requests()] == ["Please start service."]
    finally:
        await h.close()
