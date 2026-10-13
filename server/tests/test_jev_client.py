"""HttpJevClient：与 tools/jev/ask.mjs 同一契约（G 线；tech §6.6、事实 13；AC-47 的请求，AC-42/C15 的密钥不外露）。

第 3 步可见测试。不出网：httpx.MockTransport 接住请求。URL、模型名、测试密钥取自 conftest 的 mode_env，
不在测试里写死。
"""

import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from g_support import NEUTRAL_TEXT, line, standing_rules
from waitless.decision.jev import HttpJevClient, JevError, build_request, build_state
from waitless.models import JevQuestion, JevRequest, JevResponse


def _state():
    return build_state(
        task_goal="Start electric service",
        allowed_fields={"name": "Alex Example"},
        standing_rules=standing_rules(),
        prior_choices=[],
        transcript=[line("company", NEUTRAL_TEXT)],
    )


QUESTIONS = {
    "phase_q": JevQuestion(type="choice", instructions="Which phase is the call in?", criteria={"a": "A", "b": "B"}),
    "help_q": JevQuestion(type="noul", instructions="Does this need the account holder?"),
}
GOOD = {
    "answers": {
        "phase_q": {"type": "choice", "choice": "a", "confidence": 0.9, "probabilities": {"a": 0.9, "b": 0.1}},
        "help_q": {"type": "noul", "noul": 0.2},
    },
    "model": "test-jev-model",
    "usage": {"input_tokens": 10, "output_tokens": 2},
}


def _client(mode_env, handler):
    env = mode_env("text-sim")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = HttpJevClient(
        url=env["WAITLESS_JEV_URL"],
        model=env["WAITLESS_JEV_MODEL"],
        api_key=SecretStr(env["WAITLESS_JEV_API_KEY"]),
        timeout_s=5.0,
        http=http,
    )
    return env, client


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


async def test_ac47_posts_model_state_questions_with_bearer_key(mode_env) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=GOOD)

    env, client = _client(mode_env, handler)
    state = _state()
    response = await client.ask(state, QUESTIONS)

    assert isinstance(response, JevResponse)
    assert response.answers["phase_q"].choice == "a"
    assert response.answers["help_q"].noul == 0.2
    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == env["WAITLESS_JEV_URL"]
    assert request.headers["authorization"] == f"Bearer {env['WAITLESS_JEV_API_KEY']}"
    body = json.loads(request.content)
    assert set(body) == {"model", "state", "questions"}
    expected = JevRequest(model=env["WAITLESS_JEV_MODEL"], state=state, questions=QUESTIONS).model_dump(mode="json")
    assert _strip(body) == _strip(expected)


def test_ac47_build_request_matches_the_contract(mode_env) -> None:
    env = mode_env("text-sim")
    state = _state()
    request = build_request(env["WAITLESS_JEV_MODEL"], state, QUESTIONS)
    assert isinstance(request, JevRequest)
    assert request.model == env["WAITLESS_JEV_MODEL"]
    assert request.state == state
    assert dict(request.questions) == QUESTIONS


@pytest.mark.parametrize(
    "status, payload",
    [
        (500, {"error": "boom"}),
        (401, {"error": "unauthorized"}),
        (200, {"answers": {"phase_q": GOOD["answers"]["phase_q"]}}),  # 少了 help_q
        (200, {"answers": {**GOOD["answers"], "extra_q": {"type": "noul", "noul": 0.1}}}),  # 多了一题
        (200, {"answers": {"phase_q": {"type": "choice", "choice": "a"}, "help_q": {"type": "noul", "noul": 0.2}}}),
    ],
    ids=["http_500", "http_401", "missing_answer", "extra_answer", "malformed_answer"],
)
async def test_ac47_bad_responses_raise_jev_error(mode_env, caplog, status: int, payload: dict) -> None:
    env, client = _client(mode_env, lambda request: httpx.Response(status, json=payload))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(JevError) as err:
        await client.ask(_state(), QUESTIONS)
    key = env["WAITLESS_JEV_API_KEY"]
    assert key not in str(err.value) and key not in repr(err.value)
    assert key not in caplog.text
