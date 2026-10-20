"""v5.27 application contract; D1 calls handoff_connected only after valid hello/ready."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
import yaml
from a_support import (AEnv, RecordingLlm, ScriptedGate, Pick, default_prepare_gate,
                       PROFILE_VALUES, LAKESHORE_REQUEST, sample_card)
from waitless import models as m
from waitless.interfaces import ToolFailure
from waitless.mcp_server import ToolHandlers
from waitless.tasks import TaskService


@pytest.fixture
def env(tmp_path):
    e = AEnv(tmp_path, mode="live-sim")
    yield e
    e.close()


def mark_questions(e):
    path = e.skills['company-lakeshore-energy'].path / 'references/pre-call-questions.yaml'
    raw = yaml.safe_load(path.read_text())
    raw['questions'][0]['tags'].append('human_decision')
    path.write_text(yaml.safe_dump(raw))
    return raw['questions'][0]


def test_precall_marker_serialization_preserves_unmarked_text_data(env):
    path=env.skills['company-lakeshore-energy'].path/'references/pre-call-questions.yaml'
    raw=yaml.safe_load(path.read_text())
    assert m.PrecallQuestionFile.model_validate(raw).model_dump()['questions']==raw['questions']
    marked=mark_questions(env)
    assert 'human_decision' in m.PrecallQuestion.model_validate(marked).model_dump()['tags']


def task_service(e, gate):
    service = e.task_service(gate=gate)
    service.mode = e.settings.mode
    return service


async def test_live_prepare_skips_marked_question_before_decision(env):
    mark_questions(env)
    env.store.save_profile(PROFILE_VALUES, company_id='lakeshore-energy')
    gate = default_prepare_gate(precall=Pick('user_chose:paperless'))
    out = await task_service(env, gate).prepare(LAKESHORE_REQUEST)
    assert out.status == 'ready'
    assert not any(q.question_id == 'deposit_or_autopay' for q in out.task_card.precall_decisions)
    assert len(gate.requests_for('precall_default')) == 1


@pytest.mark.parametrize('mode,selected', [('live-sim','deposit'),('live-sim',None),('text-sim','deposit'),('text-sim',None)])
async def test_old_card_view_readiness_and_compiler_authorization(env, mode, selected):
    question = mark_questions(env)
    env.settings = env.settings.model_copy(update={'mode': mode})
    card = sample_card()
    card.precall_decisions = [m.PrecallDecision(**{k:question[k] for k in ('question_id','question','options','default')},
                                            selected=selected, needs_user=selected is None)]
    env.add_task('t', version=1)
    env.store.update_task('t', task_card=card.model_dump(mode='json'))
    service = task_service(env, default_prepare_gate())
    shown = service.get_card('t')
    assert len(shown.precall_decisions) == (0 if mode == 'live-sim' else 1)
    manager = env.call_manager()
    if mode == 'text-sim' and selected is None:
        with pytest.raises(ToolFailure, match='task_not_ready'):
            await manager.start('t', True, 1)
        return
    await manager.start('t', True, 1)
    compiled_card = env.factory.specs[-1].compiled.task_card
    assert len(compiled_card.precall_decisions) == (0 if mode == 'live-sim' else 1)
    assert shown.card_version == 1


async def test_live_keeps_required_facts_confirmation_and_version(env):
    mark_questions(env)
    out = await task_service(env, default_prepare_gate()).prepare(LAKESHORE_REQUEST)
    assert out.status == 'setup_required' and out.missing
    manager = env.call_manager()
    for confirmed, version, code in [(False,1,'start_not_confirmed'),(True,2,'card_version_mismatch'),(True,1,'task_not_ready')]:
        with pytest.raises(ToolFailure, match=code):
            await manager.start(out.task_id, confirmed, version)


async def test_live_does_not_drop_unmarked_legacy_required_question(env):
    mark_questions(env)
    card = sample_card()
    card.precall_decisions = [m.PrecallDecision(question_id='legacy-required', question='Required fact?',
        options=[m.Option(id='a',label='A',detail='A'),m.Option(id='b',label='B',detail='B')], default=None,
        selected=None,needs_user=True)]
    env.add_task('t',version=1)
    env.store.update_task('t',task_card=card.model_dump(mode='json'))
    assert task_service(env,default_prepare_gate()).get_card('t').precall_decisions
    with pytest.raises(ToolFailure,match='task_not_ready'):
        await env.call_manager().start('t',True,1)


async def test_live_passes_mode_to_mode_aware_g1_compiler(env):
    from a_support import fake_compiled
    env.add_task('t',version=1)
    manager=env.call_manager()
    seen=[]
    def compile_mode(card,skills,profile,rules,prefs,*,mode):
        seen.append(mode)
        return fake_compiled(card)
    manager.compile_call=compile_mode
    await manager.start('t',True,1)
    assert seen==['live-sim']


@pytest.mark.parametrize('phase', ['hold','handoff_ready','in_handoff','rejoin_grace','ended'])
async def test_hangup_idempotent_in_all_phases(env, phase):
    env.add_task('t')
    manager = env.call_manager()
    assert (await manager.hangup('t')).status == 'no_active_call'
    env.store.create_call(call_id='c',task_id='t',scenario_id=None,mode='live-sim',started_at=env.clock.now_iso(),skill_commits=[])
    class Session:
        call_id='c'
        task_id='t'
        ended=phase=='ended'
        async def hangup(self, reason):
            assert reason == 'user_hangup'
            self.ended=True
    manager._sessions['c']=Session()
    first = await manager.hangup('t')
    assert first.status == ('already_ended' if phase=='ended' else 'hung_up')
    assert first.call_id == 'c' and first.remote_hangup_pending is False
    assert (await manager.hangup('t')).status == 'already_ended'


async def real_live_session(e, *, carrier=None):
    from unittest.mock import AsyncMock
    from waitless.call.live import LiveCall
    e.add_task('t', version=1)
    manager = e.call_manager(gate=ScriptedGate({'call_outcome': Pick('failed')}))
    started = await manager.start('t', True, 1)
    carrier = carrier or SimpleNamespace(
        hangup=AsyncMock(side_effect=[RuntimeError('offline'), None]), aclose=AsyncMock())
    session = LiveCall(e.factory.specs[-1], manager.deps, carrier=carrier)
    session._call_sid = 'offline-original-sid'
    manager._sessions[started.call_id] = session
    return manager, session


async def test_remote_failure_ends_locally_and_retries_same_target(env):
    manager, session = await real_live_session(env)
    out = await manager.hangup('t')
    assert out.status == 'hung_up' and out.remote_hangup_pending and session.ended
    assert out.call_id == session.call_id
    out = await manager.hangup('t')
    assert out.status == 'already_ended' and not out.remote_hangup_pending
    assert out.call_id == session.call_id
    assert session.carrier.hangup.await_count == 2
    assert all(call.args == ('offline-original-sid',) for call in session.carrier.hangup.await_args_list)


def ended_call(e, *, mode='live-sim', ended=True):
    e.add_task('t',status='in_call')
    e.store.create_call(call_id='c',task_id='t',scenario_id=None,mode=mode,started_at=e.clock.now_iso(),skill_commits=[])
    e.trace.event(m.TraceEvent(task_id='t',call_id='c',event_type='call_started'))
    if ended:
        e.trace.event(m.TraceEvent(task_id='t',call_id='c',event_type='call_ended',action={'reason':'company_hangup'}))
        e.store.update_call('c',ended_at=e.clock.now_iso())


async def test_hello_fact_persists_and_company_hangup_awaits_user_without_llm(env):
    ended_call(env)
    llm=RecordingLlm()
    results=env.result_service(gate=ScriptedGate(),llm=llm)
    manager=env.call_manager(results=results)
    await manager.handoff_connected('c')
    out=await results.finalize('c')
    assert out.outcome=='needs_follow_up' and out.awaiting_user_result
    assert not llm.calls
    # Reopen the DB rather than relying on a session/token/trace phase.
    from waitless.store import Store
    env.store.close()
    env.store = Store(env.settings.db_path)
    env.trace.store = env.store
    results = env.result_service(gate=ScriptedGate(), llm=llm)
    result=await results.report('t','c','confirmed',None)
    assert result.result_source=='user' and result.confirmation_source is None
    assert result.verified==[] and not result.awaiting_user_result
    assert env.store.get_task('t')['status']=='completed'
    assert results.case_result('t').result_source=='user'
    assert (await results.finalize('c'))==result
    audits=[e for e in env.trace.events(call_id='c') if (e.action or {}).get('name')=='report_call_result']
    assert len(audits)==1 and audits[0].speaker=='user' and audits[0].action['result_source']=='user'


async def test_live_update_hides_legacy_manual_choice_without_weakening_facts(env):
    question = mark_questions(env)
    env.store.save_profile(PROFILE_VALUES, company_id='lakeshore-energy')
    env.add_task('t', version=1)
    card = sample_card()
    card.precall_decisions = [m.PrecallDecision(**{k:question[k] for k in ('question_id','question','options','default')},
                                              selected=None, needs_user=True)]
    env.store.update_task('t', task_card=card.model_dump(mode='json'), intent_id='start_service')
    out = await task_service(env, default_prepare_gate()).update('t')
    assert out.status == 'ready' and out.missing == []
    assert out.task_card.card_version == 2 and out.task_card.precall_decisions == []


async def test_delayed_l4_cannot_overwrite_user_submission(env):
    ended_call(env)
    entered, resume = asyncio.Event(), asyncio.Event()
    class DelayedLlm(RecordingLlm):
        async def extract_result_items(self, transcript):
            entered.set()
            await resume.wait()
            return m.ResultItems(confirmation_numbers=[], statements=[])
    results = env.result_service(gate=ScriptedGate({'call_outcome':Pick('failed')}), llm=DelayedLlm())
    manager = env.call_manager(results=results)
    finalizing = asyncio.create_task(results.finalize('c'))
    await entered.wait()
    await manager.handoff_connected('c')
    submitted = await manager.report_result('t','c','confirmed')
    resume.set()
    assert await finalizing == submitted
    assert env.store.get_task('t')['status'] == 'completed'


async def test_failed_l4_callback_cannot_downgrade_submitted_user_result(env):
    ended_call(env)
    entered, resume = asyncio.Event(), asyncio.Event()
    class FailingLlm(RecordingLlm):
        async def extract_result_items(self, transcript):
            entered.set()
            await resume.wait()
            raise RuntimeError('offline L4 failure')
    results = env.result_service(gate=ScriptedGate(), llm=FailingLlm())
    manager = env.call_manager(results=results)
    manager._sessions['c'] = SimpleNamespace(task_id='t')
    finalizing = asyncio.create_task(manager.on_ended('c'))
    await entered.wait()
    await manager.handoff_connected('c')
    submitted = await manager.report_result('t','c','confirmed')
    resume.set()
    try:
        await finalizing
    except RuntimeError:
        pass
    assert m.CallResult.model_validate_json(env.store.list_calls('t')[0]['result_json']) == submitted
    assert env.store.get_task('t')['status'] == 'completed'


async def test_latest_check_inside_transaction_rejects_a_concurrent_dial(env, monkeypatch):
    ended_call(env)
    manager = env.call_manager()
    await manager.handoff_connected('c')
    commit = env.store.commit_user_result
    def competing_dial(*args, **kwargs):
        from waitless.store import Store
        other = Store(env.settings.db_path)
        try:
            other.create_call(call_id='new',task_id='t',scenario_id=None,mode='live-sim',
                              started_at=env.clock.now_iso(),skill_commits=[])
        finally:
            other.close()
        return commit(*args, **kwargs)
    monkeypatch.setattr(env.store,'commit_user_result',competing_dial)
    with pytest.raises(ToolFailure,match='result_not_allowed'):
        await manager.report_result('t','c','confirmed')
    assert not env.store.list_calls('t')[0]['result_json']


@pytest.mark.parametrize('outcome,number', [('confirmed',None),('confirmed','LE-48291'),('failed',None),('needs_follow_up',None),('needs_follow_up','LE-48291')])
async def test_result_retries_conflict_and_sources(env,outcome,number):
    ended_call(env)
    manager=env.call_manager()
    await manager.handoff_connected('c')
    first=await manager.report_result('t','c',outcome,number)
    repeated=await asyncio.gather(*(manager.report_result('t','c',outcome,number) for _ in range(4)))
    assert all(r==first for r in repeated)
    assert first.confirmation_source==('user' if number else None)
    assert manager.results.case_result('t').confirmation_number == number
    with pytest.raises(ToolFailure,match='result_conflict'):
        await manager.report_result('t','c','failed' if outcome!='failed' else 'confirmed',number)
    assert len([e for e in env.trace.events(call_id='c') if (e.action or {}).get('name')=='report_call_result'])==1


@pytest.mark.parametrize('mode,ended,hello', [('live-sim',False,True),('live-sim',True,False),('text-sim',True,True)])
async def test_result_qualification_is_not_token_or_handoff_started(env,mode,ended,hello):
    ended_call(env,mode=mode,ended=ended)
    manager=env.call_manager()
    env.store.insert_turn(m.TraceEvent(task_id='t',call_id='c',event_type='handoff_started',decision_id='legacy'))
    if hello: await manager.handoff_connected('c')
    with pytest.raises(ToolFailure,match='result_not_allowed'):
        await manager.report_result('t','c','confirmed')


async def test_latest_call_format_and_task_ownership(env):
    ended_call(env)
    manager=env.call_manager()
    await manager.handoff_connected('c')
    with pytest.raises(ToolFailure,match='invalid_option'):
        await manager.report_result('t','c','confirmed','BH-12345')
    env.store.create_call(call_id='new',task_id='t',scenario_id=None,mode='live-sim',started_at=env.clock.now_iso(),skill_commits=[])
    env.store.update_task('t',status='in_call')
    with pytest.raises(ToolFailure,match='result_not_allowed'):
        await manager.report_result('t','c','confirmed')
    env.add_task('other')
    with pytest.raises(ToolFailure,match='result_not_allowed'):
        await manager.report_result('other','c','confirmed')
    assert env.store.get_task('t')['status']=='in_call'


@pytest.mark.parametrize('target', ['tasks','turns'])
async def test_result_transaction_rolls_back_all_writes(env,target):
    ended_call(env)
    manager=env.call_manager()
    await manager.handoff_connected('c')
    before=env.store.list_calls('t')[0]
    operation='UPDATE' if target=='tasks' else 'INSERT'
    env.store._con.execute(f"CREATE TRIGGER reject_result BEFORE {operation} ON {target} BEGIN SELECT RAISE(ABORT, 'rollback probe'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await manager.report_result('t','c','confirmed','LE-48291')
    assert env.store.list_calls('t')[0]==before
    assert env.store.get_task('t')['status']=='in_call'
    assert not any((e.action or {}).get('name')=='report_call_result' for e in env.trace.events(call_id='c'))


async def test_unjoined_canonical_number_must_be_in_company_words(env):
    env.add_task('t')
    env.add_call('t','c',[('company','Your request is still under review.'),('waitless','Confirmation LE-48291.')])
    llm=RecordingLlm(result_items=m.ResultItems(confirmation_numbers=['LE-48291'],statements=[]))
    out=await env.result_service(gate=ScriptedGate({'call_outcome':Pick('confirmed')}),llm=llm).finalize('c')
    assert out.outcome=='needs_follow_up' and out.confirmation_number is None


async def test_tool_handlers_route_user_actions(env):
    from unittest.mock import AsyncMock
    reported=m.CallResult(call_id='c',outcome='confirmed',confirmation_number=None,result_source='user',
                          verified=[],follow_ups=[],summary='User reported confirmed.')
    calls=SimpleNamespace(hangup=AsyncMock(return_value='hung'),report_result=AsyncMock(return_value=reported))
    handlers=ToolHandlers(None,SimpleNamespace(calls=calls))
    assert await handlers.hangup_call('t')=='hung'
    out=await handlers.report_call_result('t','c','confirmed',None)
    assert isinstance(out,m.ReportCallResultOut) and out.model_dump()==reported.model_dump()


async def test_real_mcp_user_result_success_retry_and_conflict(env):
    from a_support import mcp_session, mcp_app_for
    from waitless.mcp_server import build_mcp
    ended_call(env)
    manager = env.call_manager()
    await manager.handoff_connected('c')
    handlers = ToolHandlers(None, SimpleNamespace(calls=manager))
    args = dict(task_id='t',call_id='c',outcome='confirmed')
    async with mcp_session(mcp_app_for(build_mcp(None,handlers))) as client:
        first = await client.call_tool('report_call_result',args)
        assert not first.is_error
        assert first.structured_content['result_source']=='user'
        again = await client.call_tool('report_call_result',args)
        assert again.structured_content == first.structured_content
        conflict = await client.call_tool('report_call_result',dict(args,outcome='failed'))
        assert conflict.is_error and json.loads(conflict.content[0].text)['code']=='result_conflict'


@pytest.mark.parametrize('mode', ['live-sim', 'text-sim'])
@pytest.mark.parametrize('selected', [False, True])
async def test_real_human_tags_cross_task_card_and_compiler(env, skills_dir, mode, selected):
    from waitless.skills.loader import load_skills
    from waitless.skills.compiler import compile_call
    from waitless.call.keypad import KeypadDecider
    source = skills_dir / 'company-lakeshore-energy/references/pre-call-questions.yaml'
    target = env.skills['company-lakeshore-energy'].path / 'references/pre-call-questions.yaml'
    target.write_text(source.read_text())
    raw = yaml.safe_load(source.read_text())['questions']
    assert {q['question_id'] for q in raw} == {'deposit_consent', 'start_date'}
    env.settings = env.settings.model_copy(update={'mode': mode})
    env.store.save_profile(PROFILE_VALUES, company_id='lakeshore-energy')
    gate = default_prepare_gate(precall=lambda request: Pick(request.candidates[0].id))
    service = task_service(env, gate)
    out = await service.prepare(LAKESHORE_REQUEST)
    live = mode == 'live-sim'
    assert len(gate.requests_for('precall_default')) == (0 if live else 2)
    assert len(out.task_card.precall_decisions) == (0 if live else 2)
    card = out.task_card.model_copy(update={'precall_decisions': [m.PrecallDecision(
        **{k: q[k] for k in ('question_id', 'question', 'options', 'default')},
        selected=q['options'][0]['id'] if selected else None, needs_user=not selected) for q in raw]})
    env.store.update_task(out.task_id, task_card=card.model_dump(mode='json'))
    assert len(service.get_card(out.task_id).precall_decisions) == (0 if live else 2)
    manager = env.call_manager()
    if live or selected:
        await manager.start(out.task_id, True, 1)
        assert len(env.factory.specs[-1].compiled.task_card.precall_decisions) == (0 if live else 2)
    else:
        with pytest.raises(ToolFailure, match='task_not_ready'):
            await manager.start(out.task_id, True, 1)
    compiled = compile_call(card, load_skills(skills_dir), env.store.get_profile(), [], [], mode=mode)
    decider = KeypadDecider(spec=SimpleNamespace(compiled=compiled, call_id='tag-check'),
        deps=SimpleNamespace(trace=SimpleNamespace(events=lambda **kw: []), store=env.store),
        pipeline_task=None, media_gate=None, injector=None, config={})
    assert len(decider._state().prior_choices) == (0 if live or not selected else 2)
    assert len(compiled.policy['precall_questions']) == (0 if live else 2)


@pytest.mark.parametrize('wrapped', [False, True])
async def test_live_compiler_mode_survives_kwargs_forwarder(env, skills_dir, wrapped):
    from waitless.skills.compiler import compile_call
    from waitless.skills.loader import load_skills
    env.skills = load_skills(skills_dir)
    env.add_task('t', version=1)
    manager = env.call_manager()
    forwarded = []
    def forwarding(*args, **kwargs):
        forwarded.append(kwargs)
        return compile_call(*args, **kwargs)
    manager.compile_call = forwarding if wrapped else compile_call
    await manager.start('t', True, 1)
    compiled = env.factory.specs[-1].compiled
    assert compiled.agent_tool_descriptions == {}
    assert compiled.task_card.precall_decisions == []
    assert compiled.policy['precall_questions'] == []
    if wrapped:
        assert forwarded == [{'mode': 'live-sim'}]
