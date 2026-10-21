"""AC-28：Jev 的 task_intent 决策选出公司 skill 声明的意图，任务卡的 goal 和 task_summary 按该意图的模板生成。

机制（tech §7.1，A 调用 G 线已有的 DecisionGate.decide()）：DP-P1 选定公司 skill 后，TaskService.prepare() 调
decide("task_intent", request)，request.candidates 取该公司 skill 的 references/ivr-map.yaml 里的 intents
（none_of_the_above 由 G 的 gate 补，r3 缺口 A-G-6）；返回的 DecisionResult.intent 交给 build_task_card()，按该意图的 goal / task_summary 模板
（str.format，填 L1 抽出的字段）生成卡片，写进 tasks 表的 goal 与 task_card_json，并随 prepare_task 返回。
决策门用 ScriptedGate（假 gate）；技能夹具见 a_support.LAKESHORE_INTENTS。
"""

import pytest

from waitless import models as m

from a_support import LAKESHORE_EXTRACTED, LAKESHORE_INTENTS, LAKESHORE_REQUEST, AEnv, ScriptedGate, candidate_ids


@pytest.fixture
def env(tmp_path):
    e = AEnv(tmp_path)
    yield e
    e.close()


def _render(intent_id: str, key: str) -> str:
    template = next(i for i in LAKESHORE_INTENTS if i["id"] == intent_id)[key]
    return template.format(**LAKESHORE_EXTRACTED.model_dump())


async def test_ac28_card_goal_and_summary_follow_the_chosen_intent_template(env):
    gate = ScriptedGate({"company_skill": "company-lakeshore-energy", "task_intent": "start_service",
                         "precall_default": "keep_default"})
    out = await env.task_service(gate=gate).prepare(LAKESHORE_REQUEST, "Lakeshore Energy")
    card = out.task_card
    assert card is not None
    assert card.goal == _render("start_service", "goal")
    assert card.task_summary == _render("start_service", "task_summary")
    assert card.company.company_id == "lakeshore-energy"
    # 写进 tasks 表，且 get_card 读回同一张卡
    assert env.store.get_task(out.task_id)["goal"] == card.goal
    assert env.task_service(gate=gate).get_card(out.task_id) == card


async def test_ac28_intent_candidates_come_from_the_company_skill(env):
    gate = ScriptedGate({"company_skill": "company-lakeshore-energy", "task_intent": "start_service",
                         "precall_default": "keep_default"})
    await env.task_service(gate=gate).prepare(LAKESHORE_REQUEST, None)
    [req] = gate.requests_for("task_intent")
    declared = {i["id"] for i in LAKESHORE_INTENTS}
    ids = candidate_ids(req)
    assert set(ids) == declared and len(ids) == len(declared)
    assert m.NONE_OF_THE_ABOVE not in ids
    by_id = {c.id: c for c in req.candidates}
    for intent in LAKESHORE_INTENTS:
        assert by_id[intent["id"]].description == intent["description"]
    # DP-P1 先于 DP-P2
    points = [p for p, _ in gate.requests]
    assert points.index("company_skill") < points.index("task_intent")
