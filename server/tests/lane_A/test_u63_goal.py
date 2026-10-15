"""U63-3: missing L1 values never become None in task templates."""

import pytest

from a_support import AEnv, LAKESHORE_EXTRACTED, LAKESHORE_REQUEST, RecordingLlm, ScriptedGate
from waitless.skills.loader import load_skills


@pytest.fixture
def env(tmp_path, skills_dir):
    # The older AEnv fixture has different wording and no move_service intent.
    # Read the real templates without changing skills or shared test fixtures.
    instance = AEnv(tmp_path, skills=load_skills(skills_dir))
    try:
        yield instance
    finally:
        instance.close()


async def _prepare(env, *, intent="start_service", **missing):
    return await env.task_service(
        gate=ScriptedGate({"company_skill": "company-lakeshore-energy", "task_intent": intent,
                           "precall_default": "keep_default"}),
        llm=RecordingLlm(extracted=LAKESHORE_EXTRACTED.model_copy(update=missing)),
    ).prepare(LAKESHORE_REQUEST)


def _assert_no_none(env, out):
    card = out.task_card
    assert card is not None
    for text in (card.goal, card.task_summary, card.boundary, env.store.get_task(out.task_id)["goal"]):
        assert "None" not in text
    assert card.boundary == "Only complete this task: " + card.goal


async def test_u63_3_goal_omits_missing_address(env):
    out = await _prepare(env, service_address=None)
    assert out.task_card is not None
    assert out.task_card.goal == "Start electric service on November 1"
    _assert_no_none(env, out)


async def test_u63_3_goal_with_nothing_known(env):
    out = await _prepare(env, service_address=None, effective_date=None)
    assert out.task_card is not None
    assert out.task_card.goal == "Start electric service"
    _assert_no_none(env, out)


async def test_u63_3_move_service_without_address(env):
    out = await _prepare(env, intent="move_service", service_address=None)
    assert out.task_card is not None
    assert out.task_card.goal == "Move electric service on November 1"
    _assert_no_none(env, out)
