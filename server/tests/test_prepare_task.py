"""AC-29：prepare_task 一次返回全部缺失字段，以及 pre-call-questions.yaml 里的全部问题，每题都带 default；
电力任务卡含 deposit_or_autopay。

机制（tech §7.1）：TaskService.prepare() 列出缺失的可代说字段；把 pre-call-questions.yaml 的每一题带 default 写进卡。
r3 缺口 A-G-5（定稿 intent_required_fields）：missing = DP-P2 选中意图的 IvrIntent.required_fields 里档案还没有的字段
（account_number 看 Profile.accounts[company_id]）。空档案时就是该意图的全部 required_fields。
「一次列全」的断言：之后逐个补齐 missing 里的字段（update_task），missing 只减不增，补完即 ready。
"""

import pytest

from waitless import models as m

from a_support import LAKESHORE_INTENTS, LAKESHORE_QUESTIONS, LAKESHORE_REQUEST, PROFILE_VALUES, AEnv, default_prepare_gate


@pytest.fixture
def env(tmp_path):
    e = AEnv(tmp_path)
    yield e
    e.close()


async def test_ac29_all_missing_fields_and_all_precall_questions_at_once(env):
    tasks = env.task_service(gate=default_prepare_gate())
    out = await tasks.prepare(LAKESHORE_REQUEST, "Lakeshore Energy")
    keys = [f.key for f in out.missing]
    assert out.status == "setup_required"
    assert len(keys) == len(set(keys))
    assert set(keys) <= set(m.UPDATABLE_FIELD_KEYS)
    start_service = next(i for i in LAKESHORE_INTENTS if i["id"] == "start_service")
    assert set(keys) == set(start_service["required_fields"])

    card = out.task_card
    assert card is not None
    decisions = {d.question_id: d for d in card.precall_decisions}
    assert set(decisions) == {q["question_id"] for q in LAKESHORE_QUESTIONS}
    assert "deposit_or_autopay" in decisions
    for q in LAKESHORE_QUESTIONS:
        d = decisions[q["question_id"]]
        assert d.default == q["default"], q["question_id"]
        assert [o.id for o in d.options] == [o["id"] for o in q["options"]]

    remaining = set(keys)
    upd = None
    for key in keys:
        upd = await tasks.update(out.task_id, fields=m.UpdateTaskFields(**{key: PROFILE_VALUES[key]}))
        remaining.discard(key)
        assert {f.key for f in upd.missing} == remaining, f"missing grew or did not shrink after {key}"
    assert upd is not None and upd.status == "ready"
    assert upd.task_card is not None and upd.task_card.card_version > card.card_version
