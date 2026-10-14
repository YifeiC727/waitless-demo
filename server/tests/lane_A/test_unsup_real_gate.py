"""AU-1/AU-2: prepare_task handles the real decision policy's fallback choices."""

import shutil

import pytest

from a_support import AEnv, RecordingLlm, ScriptedGate, mcp_session
from fakes import FakeClock, FakeJev, FakeTrace
from g_support import PointChooser, prefer
from waitless.decision.gate import DecisionGate
from waitless.decision.policy import Policy
from waitless.decision.registry import load_registry
from waitless.interfaces import ToolFailure
from waitless.models import NONE_OF_THE_ABOVE
from waitless.skills.loader import load_skills


@pytest.fixture
def real_env(tmp_path, skills_dir):
    copied = shutil.copytree(skills_dir, tmp_path / "skills", ignore=shutil.ignore_patterns("hidden"))
    skills = load_skills(copied)
    registry = load_registry(skills).validate()
    policy = Policy.load(registry.policy_raw())
    clock = FakeClock()
    trace = FakeTrace(clock)
    jev = FakeJev(point_of=registry.point_of)
    env = AEnv(tmp_path, skills=skills)
    gate = DecisionGate(jev=jev, jev_model=env.settings.jev_model, registry=registry,
                        policy=policy, trace=trace, clock=clock)
    try:
        yield env, gate, jev
    finally:
        env.close()


async def _assert_unsupported(real_env, request):
    env, gate, jev = real_env
    jev.push(PointChooser(prefer(NONE_OF_THE_ABOVE), confidence=0.9))
    llm = RecordingLlm()
    out = await env.task_service(gate=gate, llm=llm).prepare(request)
    assert out.status == "unsupported"
    assert out.task_card is None
    assert out.message == "This company is not supported."
    assert len(out.decisions) == 1
    assert out.decisions[0].choice == "unsupported"
    row = env.store.get_task(out.task_id)
    assert row["status"] == "unsupported"
    assert row["company_id"] is None
    assert row["intent_id"] is None
    assert [call.point for call in jev.calls] == ["company_skill"]
    assert jev.remaining == 0
    assert llm.calls_for("extract_request") == [{"user_text": request}]
    with pytest.raises(ToolFailure) as exc:
        await env.call_manager(gate=gate, llm=llm).start(out.task_id, True, row["card_version"])
    assert exc.value.code == "unsupported"
    assert env.factory.sessions == []


async def test_au1_none_company_via_real_policy_is_unsupported(real_env):
    await _assert_unsupported(real_env, "Who are you?")


async def test_au1_chinese_small_talk_is_unsupported(real_env):
    await _assert_unsupported(real_env, "你是谁")


async def test_au_guard_undeclared_company_still_raises(tmp_path):
    env = AEnv(tmp_path)
    try:
        gate = ScriptedGate({"company_skill": "no-such-company"})
        with pytest.raises(ValueError, match="undeclared company skill"):
            await env.task_service(gate=gate, llm=RecordingLlm()).prepare("Who are you?")
    finally:
        env.close()


async def test_au_mcp_prepare_task_unsupported_is_not_an_error(real_env):
    env, gate, jev = real_env
    jev.push(PointChooser(prefer(NONE_OF_THE_ABOVE), confidence=0.9))
    async with mcp_session(env.app(gate=gate, llm=RecordingLlm())) as client:
        result = await client.call_tool("prepare_task", {"request": "Who are you?"})
    assert result.is_error is not True, result
    assert isinstance(result.structured_content, dict)
    assert result.structured_content["status"] == "unsupported"
    assert result.structured_content["task_card"] is None
    assert result.structured_content["decisions"][0]["choice"] == "unsupported"
    row = env.store.get_task(result.structured_content["task_id"])
    assert row["status"] == "unsupported"
    assert row["company_id"] is None
    assert row["intent_id"] is None
    assert [call.point for call in jev.calls] == ["company_skill"]
    assert jev.remaining == 0
