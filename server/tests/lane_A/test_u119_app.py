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
    raw['questions'][0]['human_only'] = True
    path.write_text(yaml.safe_dump(raw))
    return raw['questions'][0]


def task_service(e, gate):
    service = e.task_service(gate=gate)
    service.mode = e.settings.mode
    return service


async def test_live_prepare_skips_marked_question_before_decision(env):
    mark_questions(env)
    env.store.save_profile(PROFILE_VALUES, company_id='lakeshore-energy')
    gate = default_prepare_gate(precall=Pick('user_chose:deposit'))
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


async def test_remote_failure_ends_locally_and_retries_same_target(env):
    from unittest.mock import AsyncMock
    from waitless.call.live import LiveCall
    env.add_task('t')
    manager=env.call_manager()
    # Real LiveCall.hangup, with a memory carrier and local finish (D1 not merged).
    session=object.__new__(LiveCall)
    session.call_id='c'; session.task_id='t'; session._call_sid='sid'
    session._ended_event=asyncio.Event(); session._finish_reason=None
    session.carrier=SimpleNamespace(hangup=AsyncMock(side_effect=[RuntimeError('offline'), None]))
    async def finish(call_id): session._ended_event.set()
    session._finish=finish
    manager._sessions['c']=session
    out=await manager.hangup('t')
    assert out.status=='hung_up' and out.remote_hangup_pending and session.ended
    out=await manager.hangup('t')
    assert out.status=='already_ended' and not out.remote_hangup_pending
    assert session.carrier.hangup.await_count==2


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
    # Reload from disk rather than relying on a session/token/trace phase.
    result=await results.report('t','c','confirmed',None)
    assert result.result_source=='user' and result.confirmation_source is None
    assert result.verified==[] and not result.awaiting_user_result
    assert env.store.get_task('t')['status']=='completed'
    assert results.case_result('t').result_source=='user'
    assert (await results.finalize('c'))==result
    audits=[e for e in env.trace.events(call_id='c') if (e.action or {}).get('name')=='report_call_result']
    assert len(audits)==1 and audits[0].speaker=='user' and audits[0].action['result_source']=='user'


@pytest.mark.parametrize('outcome,number', [('confirmed',None),('confirmed','LE-48291'),('failed',None),('needs_follow_up',None)])
async def test_result_retries_conflict_and_sources(env,outcome,number):
    ended_call(env)
    manager=env.call_manager()
    await manager.handoff_connected('c')
    first=await manager.report_result('t','c',outcome,number)
    repeated=await asyncio.gather(*(manager.report_result('t','c',outcome,number) for _ in range(4)))
    assert all(r==first for r in repeated)
    assert first.confirmation_source==('user' if number else None)
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
    calls=SimpleNamespace(hangup=AsyncMock(return_value='hung'),report_result=AsyncMock(return_value='reported'))
    handlers=ToolHandlers(None,SimpleNamespace(calls=calls))
    assert await handlers.hangup_call('t')=='hung'
    assert await handlers.report_call_result('t','c','confirmed',None)=='reported'
