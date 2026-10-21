import json
import pytest
import yaml
from a_support import RecordingLlm, ScriptedGate, Pick
from waitless.models import TaskCard
from waitless.utility_lookup import LiveLookup
from .test_u85_task_card import env, prepared, FakeLiveLookup, view
from .test_u85_verify import TEST_CFG
from .test_u85_lookup_config import LOOKUP

async def test_u85_3_dial_uses_the_simulated_line(env):
    out = await prepared(env, FakeLiveLookup(view()))
    await env.call_manager().start(out.task_id, confirmed=True, card_version=1)
    [spec] = env.factory.specs
    assert spec.to_number == env.store.get_company("lakeshore-energy")["line_number"]
    assert ''.join(c for c in spec.to_number if c.isdigit()) != "8005550199"
    assert spec.company_name == "Simulated utility line"

async def test_u85_3_missing_line_refuses_to_dial(env):
    from a_support import sample_card
    c = sample_card(1, company_id="missing-line").model_copy(update={"simulated_line": view().simulated_line})
    env.store.create_task(task_id="missing", company_id="missing-line", goal=c.goal, request_text="test",
        task_card=c.model_dump(mode="json"), card_version=1, status="ready")
    with pytest.raises(ValueError):
        await env.call_manager().start("missing", confirmed=True, card_version=1)
    assert env.factory.sessions == [] and env.store.get_task("missing")["status"] == "ready" and env.store.list_calls("missing") == []

async def test_u85_3_results_use_the_line_name(env):
    out = await prepared(env, FakeLiveLookup(view()))
    env.add_call(out.task_id, "call-u85", [("company", "Please follow up.")])
    llm = RecordingLlm(summary="The request was confirmed.")
    results = env.result_service(gate=ScriptedGate({"call_outcome": Pick("needs_follow_up", confidence=.97)}), llm=llm)
    result = await results.finalize("call-u85")
    assert results.case_result(out.task_id).company_name == "Simulated utility line"
    assert llm.calls_for("write_summary")[0]["result"].company_name == "Simulated utility line"
    assert result.summary.startswith("The call with Simulated utility line")

async def test_u85_3_without_simulated_line_names_are_unchanged(env):
    out = await prepared(env)
    manager = env.call_manager()
    await manager.start(out.task_id, confirmed=True, card_version=1)
    assert env.factory.specs[0].company_name == out.task_card.company.name
    assert manager.results.case_result(out.task_id).company_name == out.task_card.company.name

def test_u85_3_build_services_wires_lookup_only_when_on(mode_env, tmp_path, skills_dir, monkeypatch):
    import shutil
    import anthropic
    from fakes import FakeJev, FakeConverse
    from waitless.config import load_settings
    from waitless.app import build_services
    copied = tmp_path / "skills"
    shutil.copytree(skills_dir, copied)
    (copied / "find-official-number/references/live-lookup.yaml").write_text(yaml.safe_dump(TEST_CFG.model_dump()))
    calls = []
    def no_create(*a, **kw):
        calls.append(kw)
        pytest.fail("SDK called during construction")
    monkeypatch.setattr(anthropic.resources.messages.AsyncMessages, "create", no_create)
    for on in (False,True):
        s = load_settings(mode_env("text-sim", WAITLESS_SKILLS_DIR=str(copied), **(LOOKUP if on else {})))
        services, parts = build_services(s, jev=FakeJev(), converse=FakeConverse())
        try:
            assert isinstance(parts.tasks.lookup, LiveLookup) if on else parts.tasks.lookup is None
            assert calls == []
        finally:
            services.store.close()
