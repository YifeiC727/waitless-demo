"""AC-27：Jev 的 company_skill 决策以 none_of_the_above 为最高项时，prepare_task 返回 status=unsupported，该任务不能开始通话。

机制（tech §7.1）：TaskService.prepare() 调 decide("company_skill")，结果是 none 时任务状态设为 unsupported；
CallManager.start() 检查状态，返回 unsupported。决策门用 ScriptedGate（G 线替身）。
"""

import pytest

from waitless import models as m
from waitless.interfaces import ToolFailure

from a_support import AEnv, Pick, RecordingLlm, ScriptedGate, candidate_ids

REQUEST = "Cancel my Northwind Gas account at my old place."
EXTRACTED = m.ExtractedRequest(company_name="Northwind Gas", intent_text="cancel gas account")


@pytest.fixture
def env(tmp_path):
    e = AEnv(tmp_path)
    yield e
    e.close()


async def test_ac27_none_of_the_above_makes_task_unsupported_and_unstartable(env):
    gate = ScriptedGate({"company_skill": Pick("none_of_the_above", confidence=0.91)})
    tasks = env.task_service(gate=gate, llm=RecordingLlm(extracted=EXTRACTED))
    out = await tasks.prepare(REQUEST, "Northwind Gas")
    assert out.status == "unsupported"
    assert out.task_card is None
    row = env.store.get_task(out.task_id)
    assert row is not None and row["status"] == "unsupported"

    calls = env.call_manager()
    with pytest.raises(ToolFailure) as err:
        await calls.start(out.task_id, True, row["card_version"])
    assert err.value.code == "unsupported"
    assert env.factory.specs == []
    assert env.store.list_calls(out.task_id) == []


async def test_ac27_company_candidates_are_the_company_skills(env):
    gate = ScriptedGate({"company_skill": Pick("none_of_the_above", confidence=0.91)})
    await env.task_service(gate=gate, llm=RecordingLlm(extracted=EXTRACTED)).prepare(REQUEST, None)
    [req] = gate.requests_for("company_skill")
    ids = candidate_ids(req)
    # r3 缺口 A-G-6（gate_appends）：调用方只放公司 skill，none_of_the_above 由 G 的 gate 补
    assert ids == ["company-lakeshore-energy"]
    assert m.NONE_OF_THE_ABOVE not in ids
