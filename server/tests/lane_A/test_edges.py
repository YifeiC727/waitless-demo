import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from waitless import models as m
from waitless.interfaces import ToolFailure
from waitless.bedrock import CodexCliConverseClient, CodexCliError
from waitless.store import Store
from waitless.official_number import FixtureFetcher
from a_support import AEnv, StubHandlers, mcp_session, mcp_app_for, stub_services, make_settings, TOOL_ARGS, company_turn, plain, consequential, decision_event
from waitless.mcp_server import build_mcp


async def test_official_client_top_level_extra_is_ignored(tmp_path):
    handlers = StubHandlers()
    async with mcp_session(mcp_app_for(build_mcp(stub_services(make_settings(tmp_path)), handlers))) as client:
        result = await client.call_tool("update_task", {**TOOL_ARGS["update_task"], "fixture_unknown": "unused"})
    assert not result.is_error
    assert len(handlers.calls_for("update_task")) == 1


async def test_mcp_rejects_coerced_confirmation(tmp_path):
    handlers = StubHandlers()
    async with mcp_session(mcp_app_for(build_mcp(stub_services(make_settings(tmp_path)), handlers))) as client:
        result = await client.call_tool("start_assisted_call", {"task_id": "t", "confirmed": "true", "card_version": "2"})
    assert result.is_error
    assert handlers.calls_for("start_assisted_call") == []


async def test_concurrent_start_cannot_double_dial(tmp_path):
    env = AEnv(tmp_path)
    try:
        task_id, version = await env.ready_task()
        manager = env.call_manager()
        results = await asyncio.gather(manager.start(task_id, True, version), manager.start(task_id, True, version), return_exceptions=True)
        assert len(env.factory.sessions) == 1
        [failure] = [r for r in results if isinstance(r, ToolFailure)]
        assert failure.code == "call_in_progress"
    finally:
        env.close()


async def test_invalid_choice_update_has_no_profile_write(tmp_path):
    env = AEnv(tmp_path)
    try:
        task_id, _ = await env.ready_task()
        from a_support import default_prepare_gate
        tasks = env.task_service(gate=default_prepare_gate())
        before = env.store.get_profile()
        with pytest.raises(ToolFailure):
            await tasks.update(task_id, fields=m.UpdateTaskFields(name="Another Person"), decisions={"deposit_or_autopay": "unknown"})
        assert env.store.get_profile() == before
    finally:
        env.close()


def test_transcript_rejected_in_handoff_window(tmp_path):
    env = AEnv(tmp_path)
    try:
        env.trace.event(decision_event("t", "c", "d", turn_seq=1))
        env.trace.event(consequential("handoff_started", "t", "c", "d"))
        with pytest.raises(ValueError):
            env.trace.event(company_turn("t", "c", "private words"))
        assert all(e.transcript != "private words" for e in env.trace.events(call_id="c"))
        env.trace.event(plain("handoff_ended", "t", "c"))
        env.trace.event(company_turn("t", "c", "public words"))
    finally:
        env.close()


def test_fixture_url_cannot_escape_root(tmp_path):
    from urllib.parse import urlsplit, urlunsplit
    from a_support import LAKESHORE_URLS
    (tmp_path / "www").mkdir()
    (tmp_path / "outside.html").write_text("private")
    source = urlsplit(LAKESHORE_URLS[0])
    fetcher = FixtureFetcher({source.hostname: tmp_path / "www"})
    assert fetcher.fetch(urlunsplit((source.scheme, source.netloc, "/%2e%2e/outside.html", "", ""))) is None


async def test_codex_rejects_wrong_type_without_retry():
    calls = []
    async def runner(argv, text, timeout):
        calls.append(text)
        return 0, '{"value": 3}', ''
    from waitless.bedrock import build_forced_tool_config
    call = m.ConverseCall(task="extract_request", model_id="test", messages=[{"role": "user", "content": [{"text": "x"}]}],
           tool_config=build_forced_tool_config("extract", "Extract", {"type": "object", "properties": {"value": {"type": "string"}}}))
    with pytest.raises(CodexCliError):
        await CodexCliConverseClient(command=["fake"], timeout_s=1, runner=runner).converse(call)
    assert len(calls) == 1


async def test_build_services_uses_injected_clients_and_mode_factory(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fakes import FakeClock, FakeConverse, FakeJev, converse_tool_use
    from a_support import build_skills, ScriptedGate, default_prepare_gate, FakeHandoffTokens, FakePending, FakeSessionFactory, RecordingCompile, DictFetcher, lakeshore_pages, LAKESHORE_REQUEST, PROFILE_VALUES
    import waitless.app as app_module
    import waitless.skills.loader as loader
    import waitless.skills.compiler as compiler
    import waitless.decision.registry as registry_module
    import waitless.decision.policy as policy_module
    import waitless.decision.gate as gate_module
    import waitless.decision.pending as pending_module
    import waitless.call.handoff as handoff_module
    import waitless.call.textcall as text_module
    skills = build_skills(tmp_path / "skills")
    seen = {}
    monkeypatch.setattr(loader, "load_skills", lambda path: skills)
    monkeypatch.setattr(registry_module, "load_registry", lambda skills: SimpleNamespace(validate=lambda: None, policy_raw=lambda: {}))
    monkeypatch.setattr(policy_module.Policy, "load", lambda value: object())
    def gate_factory(**kwargs):
        seen.update(kwargs)
        return default_prepare_gate(trace=kwargs['trace'])
    monkeypatch.setattr(gate_module, "DecisionGate", gate_factory)
    monkeypatch.setattr(pending_module, "PendingRegistry", lambda clock: FakePending())
    monkeypatch.setattr(handoff_module, "HandoffTokens", lambda clock, ttl_s: FakeHandoffTokens())
    factory = FakeSessionFactory()
    monkeypatch.setattr(text_module, "create_text_call", factory)
    monkeypatch.setattr(compiler, "compile_call", RecordingCompile())
    monkeypatch.setattr(app_module.FixtureFetcher, "from_company_dir", lambda path: DictFetcher(lakeshore_pages()))
    settings = make_settings(tmp_path / "env", mode="text-sim")
    jev, converse, clock = FakeJev(), FakeConverse(), FakeClock()
    converse.add("extract_request", converse_tool_use("extract_request", {
        "company_name": "Lakeshore Energy", "intent_text": "start", "service_address": "fixture address",
        "effective_date": "November 1", "stated_preferences": []}))
    services, parts = app_module.build_services(settings, jev=jev, converse=converse, clock=clock)
    try:
        assert seen['jev'] is jev and seen['clock'] is clock
        assert parts.tasks.llm.converse is converse
        services.store.save_profile(PROFILE_VALUES, company_id="lakeshore-energy")
        prepared = await parts.tasks.prepare(LAKESHORE_REQUEST)
        started = await parts.calls.start(prepared.task_id, True, prepared.task_card.card_version)
        assert factory.specs[0].call_id == started.call_id
        assert parts.calls.deps.on_ended == parts.calls.on_ended
    finally:
        services.store.close()


async def test_assistant_update_uses_only_current_user_fields(tmp_path):
    from a_support import RecordingLlm, ScriptedGate, LAKESHORE_EXTRACTED
    env = AEnv(tmp_path)
    try:
        svc = env.assistant(gate=ScriptedGate({'assistant_next_action': 'update_task'}),
                            llm=RecordingLlm(extracted=LAKESHORE_EXTRACTED))
        out = await svc.turn(m.AssistantTurnIn(conversation_id='c', task_id='t',
                 user_text='name: Taylor Example; account_number: 70012345',
                 tools=[m.ToolInfo(name='update_task', description='Update the card')]))
        assert out.next.arguments['fields']['name'] == 'Taylor Example'
        assert out.next.arguments['fields']['account_number'] == '70012345'
        assert 'ssn' not in out.next.arguments['fields']
    finally:
        env.close()
