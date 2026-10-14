"""Execution boundaries absent from the shared visible group."""
import pytest

from e_support import (
    ScriptedCompany, StubLlm, When, build_settings, line, make_rig,
    queue_reactions, run_until, spec_ask_user, spec_end, spec_release,
    spec_say, with_pace,
)
from waitless import models as m
from waitless.interfaces import ToolFailure


def rig_for(mode_env, engine_config, fake_clock, company, script, **kwargs):
    return make_rig(settings=build_settings(mode_env("text-sim"), engine_config),
                    clock=fake_clock, company=company, script=script,
                    allowed_fields={"address": "Test address"}, **kwargs)


@pytest.mark.parametrize("question,expected", [
    ("What start date would you like?", "Please start service on 2026-11-01."),
    ("What is the service address?", "The service address is Test address."),
])
async def test_release_uses_card_date_and_whitelisted_address(question, expected, mode_env, engine_config, fake_clock):
    company = ScriptedCompany([line(question)], queue_reactions(
        When("speech", None, [line("Goodbye", kind="hangup")])) )
    rig = rig_for(mode_env, engine_config, fake_clock, company, [(question, spec_release())])
    session = await rig.start()
    await run_until(fake_clock, lambda: session.ended)
    assert company.heard() == [expected]
    assert rig.llm.offer_calls == rig.llm.other_calls == []


async def test_pending_timeout_uses_safe_template_and_original_decision(mode_env, engine_config, fake_clock):
    company = ScriptedCompany([line("Which offer?")], queue_reactions(
        When("speech", "not available", [line("Goodbye", kind="hangup")])) )
    rig = rig_for(mode_env, engine_config, fake_clock, company,
                  [("Which offer", spec_ask_user())], timeout_sec=2,
                  llm=StubLlm(offer_options=[{"label": "Later", "detail": "Start later"}]))
    session = await rig.start()
    await run_until(fake_clock, lambda: session.status().pending_choice is not None)
    choice = session.status().pending_choice
    await run_until(fake_clock, lambda: session.ended)
    assert rig.pending.get(choice.choice_id).status == "expired"
    assert any("not available" in text for text in company.heard())
    expired = await session.answer(choice.choice_id, choice.options[0].id)
    assert expired.status == "expired"
    assert rig.events("injection_sent")[-1].decision_id == rig.gate.result_for("ask_user").decision_id


async def test_pending_extraction_failure_and_invalid_answer(mode_env, engine_config, fake_clock):
    company = ScriptedCompany([line("Which offer?")], queue_reactions(
        When("speech", "account holder chose", [line("Goodbye", kind="hangup")])) )
    rig = rig_for(mode_env, engine_config, fake_clock, company,
                  [("Which offer", spec_ask_user())], llm=StubLlm(offer_error=ValueError("extract failed")))
    session = await rig.start()
    await run_until(fake_clock, lambda: session.status().pending_choice is not None)
    choice = session.status().pending_choice
    assert [o.id for o in choice.options] == ["accept", "decline", "decide_later"]
    with pytest.raises(ToolFailure) as err:
        await session.answer(choice.choice_id, "invalid")
    assert err.value.code == "invalid_option"
    await session.answer(choice.choice_id, "decide_later")
    await run_until(fake_clock, lambda: session.ended)


async def test_said_check_retry_then_policy_end(mode_env, engine_config, fake_clock):
    def points(point, req):
        assert point == "said_as_decided"
        if req.attempt == 0:
            return {"action": "say", "intent": "reinject", "say_text": req.template_vars[m.SAID_SUMMARY_VAR]}
        return {"action": "end", "intent": "safe_default", "reason_code": "say_mismatch"}

    company = ScriptedCompany([line("Any offers?")])
    rig = rig_for(mode_env, engine_config, fake_clock, company,
                  [("offers", spec_say("Please skip the offer."))], points=points)
    session = await rig.start()
    await run_until(fake_clock, lambda: session.ended)
    assert [req.attempt for _, req in rig.gate.point_calls] == [0, 1]
    assert company.heard()[:2] == ["Please skip the offer."] * 2
    assert len(rig.events("say_mismatch")) == 1
    assert rig.events("hangup")[0].decision_id == rig.gate.results[-1].decision_id


async def test_pacing_uses_dependency_clock(mode_env, engine_config, fake_clock):
    engine = with_pace(engine_config, 500)
    company = ScriptedCompany([line("Done.")])
    rig = rig_for(mode_env, engine, fake_clock, company, [("Done.", spec_end())])
    session = await rig.start()
    await fake_clock.advance(0)
    assert rig.gate.round_calls == []
    await run_until(fake_clock, lambda: session.ended)
    assert 0.5 in fake_clock.sleep_calls
    assert rig.gate.turns()[0].t_end_ms - rig.gate.turns()[0].t_start_ms >= 500


async def test_debug_projects_skill_refs_to_commits(mode_env, engine_config, fake_clock):
    company = ScriptedCompany([line("Done.")])
    rig = rig_for(mode_env, engine_config, fake_clock, company, [("Done.", spec_end())])
    rig.compiled.skill_versions.append(m.SkillRef(name="example", commit="unknown", content_sha256="0" * 64))
    session = await rig.start()
    await run_until(fake_clock, lambda: session.ended)
    debug = session.status(debug=True).debug
    assert debug.skills == [m.SkillCommit(name="example", commit="unknown")]
    assert debug.last_decision.decision_id == rig.gate.results[-1].decision_id


async def test_gate_failure_is_reported_and_wait_ended_raises(mode_env, engine_config, fake_clock):
    def fail(turn, ctx):
        raise RuntimeError("decision service unavailable")

    company = ScriptedCompany([line("Any offers?")])
    rig = rig_for(mode_env, engine_config, fake_clock, company, fail)
    session = await rig.start()
    await run_until(fake_clock, lambda: session.ended)
    assert len(rig.events("error")) == 1
    with pytest.raises(RuntimeError, match="decision service unavailable"):
        await session.wait_ended()
    assert rig.ended_calls == [rig.spec.call_id]


async def test_timeout_is_traced_once(mode_env, engine_config, fake_clock):
    company = ScriptedCompany([line("Which offer?")], queue_reactions(
        When("speech", "not available", [line("Goodbye", kind="hangup")])) )
    rig = rig_for(mode_env, engine_config, fake_clock, company,
                  [("Which offer", spec_ask_user())], timeout_sec=1)
    session = await rig.start()
    await run_until(fake_clock, lambda: session.ended)
    (expired,) = rig.events("pending_expired")
    assert expired.decision_id == rig.gate.result_for("ask_user").decision_id
    assert expired.action["choice_id"] == rig.pending.created[0]["choice"].choice_id
