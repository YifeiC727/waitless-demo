"""I2E-4: structured outcomes govern L5 prompts and persisted summaries."""
import json

import pytest

from a_support import Pick
from fakes import FakeConverse, converse_text
from waitless import models as m
from waitless.llm_tasks import LlmTaskRunner

from .test_r12_confirmation import SUMMARY, env, finalize


async def test_r12_empty_l4_replaces_actual_contradicting_summary(env):
    statements = [m.ResultStatement(text="Please follow up about the deposit.", turn_seq=1),
                  m.ResultStatement(text="Please verify the start date.", turn_seq=2)]
    out, row, _ = await finalize(env, [], summary=SUMMARY, statements=statements)
    expected = "The call with Lakeshore Energy needs follow-up. There are 2 follow-up items."
    assert out.outcome == "needs_follow_up" and out.confirmation_number is None
    assert out.summary == json.loads(row["result_json"])["summary"] == expected
    assert "confirmed" not in out.summary.lower()
    assert "LE" not in out.summary


@pytest.mark.parametrize("outcome", ["needs_follow_up", "failed"])
@pytest.mark.parametrize("summary", [
    "The request is CONFIRMED.",
    "The CoNfIrMaTiOn NuMbEr will be available later.",
    "Reference: LE-65833.",
    "Reference: LE 65833.",
    "Reference: l e sixty five thousand eight hundred thirty three.",
    "Reference: l e six five eight three three.",
])
async def test_r12_nonconfirmed_contradiction_guard(env, outcome, summary):
    out, _, _ = await finalize(env, [], summary=summary, pick=Pick(outcome, confidence=0.97))
    status = "needs follow-up" if outcome == "needs_follow_up" else "failed"
    assert out.outcome == outcome
    assert out.summary == f"The call with Lakeshore Energy {status}. There are 0 follow-up items."


@pytest.mark.parametrize("numbers", [["LE-65833"], ["LE 65833"]])
async def test_r12_guard_preserves_confirmed_summary(env, numbers):
    out, _, _ = await finalize(env, numbers, summary=SUMMARY)
    assert (out.outcome, out.confirmation_number) == ("confirmed", "LE-65833")
    assert out.summary == SUMMARY


@pytest.mark.parametrize("summary", [
    "The company asked a question that still needs follow-up.",
    "A callback is needed about reference EL 12345.",
])
async def test_r12_guard_preserves_noncontradicting_summary(env, summary):
    out, _, _ = await finalize(env, [], summary=summary)
    assert out.outcome == "needs_follow_up"
    assert out.summary == summary


async def test_r12_l5_prompt_makes_structured_result_authoritative(env):
    _, _, llm = await finalize(env, [])
    [summary_input] = llm.calls_for("write_summary")
    converse = FakeConverse().add("write_summary", converse_text("Follow-up is needed."))
    runner = LlmTaskRunner(converse, model_id="test-planner-model")
    assert await runner.write_summary(**summary_input) == "Follow-up is needed."
    [call] = converse.calls_for("write_summary")
    system = " ".join(block["text"] for block in call.system)
    assert "result.outcome and result.confirmation_number are authoritative" in system
    assert "If result.outcome is not confirmed" in system
    assert "do not say the request is confirmed or give a confirmation number" in system
    assert "follow-up is needed" in system
    assert "reproduce result.confirmation_number exactly" in system
    assert "at most three sentences" in system and "Do not invent facts" in system
    payload = json.loads(call.messages[0]["content"][0]["text"])
    assert payload == {"result": summary_input["result"].model_dump(mode="json"),
                       "transcript": [line.model_dump(mode="json") for line in summary_input["transcript"]]}
