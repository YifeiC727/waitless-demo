"""I-19 F7: persisted eligibility and explicit system-handoff scope before L4."""
import pytest
from a_support import RecordingLlm, ScriptedGate, Pick
from lane_A.test_u119_app import env, ended_call
from lane_A.test_i19_confirmation import ready_call, saved
from waitless import models as m
from waitless.interfaces import ToolFailure


@pytest.mark.parametrize('reason,joined,phrase', [('join_timeout',False,'坐席已接起，你未及时加入'),
    ('rejoin_timeout',True,'重新加入'), ('company_hangup',False,'坐席在你加入前挂断'),
    ('company_hangup',True,'report')])
async def test_system_handoff_reason_results_never_call_models(env, reason, joined, phrase):
    ended_call(env)
    env.trace.event(m.TraceEvent(task_id='t',call_id='c',event_type='call_ended',
        action={'reason':reason,'live':True,'system_handoff':True,'handoff_joined':not joined}))
    if joined: env.store.mark_handoff_connected('c',env.clock.now_iso())
    llm = RecordingLlm()
    gate = ScriptedGate()
    results = env.result_service(gate=gate,llm=llm)
    result = await results.finalize('c')
    assert result.outcome == 'needs_follow_up' and result.awaiting_user_result is joined
    assert phrase in result.summary
    assert not llm.calls and not gate.requests
    assert env.store.get_task('t')['status'] == 'needs_follow_up'
    assert results.case_result('t').awaiting_user_result is joined
    if not joined:
        with pytest.raises(ToolFailure,match='result_not_allowed'):
            await results.report('t','c','confirmed')
    else:
        assert (await results.report('t','c','confirmed')).result_source == 'user'


@pytest.mark.parametrize('mode,system', [('live-sim',False),('text-sim',True),('replay',True)])
async def test_other_modes_and_ordinary_live_keep_evidence_rules(env, mode, system):
    ended_call(env,mode=mode)
    env.trace.event(m.TraceEvent(task_id='t',call_id='c',event_type='call_ended',
        action={'reason':'join_timeout','live':True,'system_handoff':system,'handoff_joined':True}))
    llm = RecordingLlm()
    gate = ScriptedGate({'call_outcome':Pick('failed')})
    result = await env.result_service(gate=gate,llm=llm).finalize('c')
    assert result.outcome == 'failed' and not result.awaiting_user_result
    assert [name for name,_ in llm.calls] == ['extract_result_items','write_summary']
    assert len(gate.requests_for('call_outcome')) == 1


async def test_finish_snapshots_system_handoff_before_ready_notification(env):
    manager, session = await ready_call(env)
    session._handoff_id = None
    session._phase = 'hold'
    session._toolbox.action_phase = None
    assert not any(e.event_type == 'handoff_ready' for e in env.trace.events(call_id=session.call_id))
    await session._finish(session.call_id)
    ended = [e for e in env.trace.events(call_id=session.call_id) if e.event_type == 'call_ended'][-1]
    assert ended.action['system_handoff'] is True
    result = m.CallResult.model_validate_json(saved(env,session)['result_json'])
    assert result.outcome == 'needs_follow_up' and not result.awaiting_user_result
    assert '坐席在你加入前挂断' in result.summary
    assert not manager.results.llm.calls


async def test_submitted_user_result_survives_late_finalize_and_confirmation(env):
    ended_call(env)
    env.trace.event(m.TraceEvent(task_id='t',call_id='c',event_type='call_ended',
        action={'reason':'rejoin_timeout','live':True,'system_handoff':True,'handoff_joined':True}))
    manager = env.call_manager()
    await manager.handoff_connected('c')
    first = env.store.list_calls('t')[0]['user_joined_at']
    initial = await manager.results.finalize('c')
    assert initial.awaiting_user_result
    submitted = await manager.report_result('t','c','confirmed')
    await env.clock.advance(1000)
    await manager.handoff_connected('c')
    assert await manager.results.finalize('c') == submitted
    assert env.store.list_calls('t')[0]['user_joined_at'] == first
    assert env.store.get_task('t')['status'] == 'completed'
    assert not manager.results.llm.calls
