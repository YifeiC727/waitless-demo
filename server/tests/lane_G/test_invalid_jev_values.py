"""Invalid Jev values fail at the Jev boundary, before policy or trace."""

import json

import httpx
import pytest
from pydantic import SecretStr

from fakes import round_answers
from g_support import build_kit, profile, speech_turn
from waitless.decision.jev import HttpJevClient, JevError, build_state
from waitless.models import JevQuestion


INVALID = [("menu_choice", {}), ("call_phase", {}), ("stakes", 4), ("stakes", float("inf"))]


@pytest.mark.parametrize("qid,value", INVALID)
async def test_gate_rejects_unusable_answers_before_policy(
    skills_dir, test_profile, fake_jev, fake_clock, qid, value
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    response = round_answers("open_answer", call_phase="live_rep")
    if qid == "stakes":
        response[qid]["score"] = value
    else:
        response[qid]["probabilities"] = value
    fake_jev.push(response)
    with pytest.raises(JevError):
        await kit.round(speech_turn("How can I help you today?"))
    assert len(fake_jev.calls) == 1
    assert kit.trace.decisions() == []


@pytest.mark.parametrize("kind,value", [("choice", {}), ("score", 4), ("score", float("inf"))])
async def test_http_client_rejects_unusable_answers(mode_env, kind, value):
    questions = {"q": JevQuestion(
        type=kind, instructions="Select a response", criteria={"a": "A", "b": "B"}
        if kind == "choice" else ["0: none", "1: minor", "2: service", "3: money"])}
    answer = ({"type": "choice", "choice": "a", "confidence": 0.9, "probabilities": value}
              if kind == "choice" else {"type": "score", "score": value, "confidence": 0.9})
    env = mode_env("text-sim")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=json.dumps({"answers": {"q": answer}})))) as http:
        client = HttpJevClient(url=env["WAITLESS_JEV_URL"], model=env["WAITLESS_JEV_MODEL"],
                               api_key=SecretStr(env["WAITLESS_JEV_API_KEY"]), timeout_s=5, http=http)
        state = build_state(task_goal="Start service", allowed_fields={}, standing_rules=[],
                            prior_choices=[], transcript=[])
        with pytest.raises(JevError):
            await client.ask(state, questions)
