import pytest

from a_support import (
    AEnv, LAKESHORE_QUESTIONS, LAKESHORE_REQUEST, PROFILE_VALUES,
    Pick, default_prepare_gate, precall_by_option,
)
from waitless.interfaces import ToolFailure


@pytest.fixture
def env(tmp_path):
    env = AEnv(tmp_path)
    yield env
    env.close()


@pytest.mark.parametrize("pick", [
    "keep_default",
    "ask_user",
    Pick("keep_default", choice="user_chose:autopay", confidence=0.80),
])
async def test_u84_unchosen_questions_stay_off_the_card(env, pick):
    env.store.save_profile(PROFILE_VALUES, company_id="lakeshore-energy")
    gate = default_prepare_gate(precall=pick)
    out = await env.task_service(gate=gate).prepare(LAKESHORE_REQUEST)
    assert out.status == "ready"
    assert out.missing == []
    assert out.task_card.precall_decisions == []
    assert len(gate.requests_for("precall_default")) == len(LAKESHORE_QUESTIONS)


async def test_u84_user_choice_goes_on_the_card(env):
    env.store.save_profile(PROFILE_VALUES, company_id="lakeshore-energy")
    gate = default_prepare_gate(precall=precall_by_option({
        "autopay": Pick("user_chose:autopay", confidence=0.93),
    }))
    out = await env.task_service(gate=gate).prepare(LAKESHORE_REQUEST)
    [decision] = out.task_card.precall_decisions
    assert (decision.question_id, decision.selected, decision.default, decision.needs_user) == (
        "deposit_or_autopay", "autopay", "deposit", False,
    )


async def test_u84_update_rejects_a_question_that_is_not_on_the_card(env):
    task_id, version = await env.ready_task()
    with pytest.raises(ToolFailure) as exc:
        await env.task_service(gate=default_prepare_gate()).update(
            task_id, decisions={"deposit_or_autopay": "autopay"},
        )
    assert exc.value.code == "invalid_option"
    assert env.store.get_task(task_id)["card_version"] == version
