"""I2E-2: cancellation must not erase hangup, and finish precedes results."""
import asyncio
from dataclasses import replace

import pytest
from call_engine.conftest import compiled, deps, engine, gate, line_number, settings, spec, store, trace
from call_engine._support import MiniCall, make_result, record_decision, settle, wait_until
from fakes import SonicResponse
from waitless.call.live import LiveCall


def ending_events(trace):
    return [e for e in trace.events() if e.event_type in ("hangup", "remote_hangup", "call_ended")]


def bind_call(mc, spec, deps, trace):
    snapshots = []
    original = deps.on_ended
    async def on_ended(call_id):
        snapshots.append([e.event_type for e in ending_events(trace)])
        await original(call_id)
    call = LiveCall(spec, replace(deps, on_ended=on_ended), carrier=mc.carrier)
    call._actions, call._call_sid = mc.actions, mc.call_sid
    call.cancel_snapshots = []
    class PipelineTask:
        async def cancel(self):
            call.cancel_snapshots.append([e.event_type for e in ending_events(trace)])
    call._task = PipelineTask()
    mc.actions.deps = replace(deps, on_ended=call._finish)
    return call, snapshots


@pytest.mark.parametrize("disconnect_during_request", [True, False])
async def test_r12_end_writes_hangup_then_ended_before_results(disconnect_during_request,
        spec, deps, engine, trace, make_fake_sonic):
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        mc.tap.guard = mc.guard
        call, snapshots = bind_call(mc, spec, deps, trace)
        original = mc.carrier.hangup
        async def hangup(call_sid):
            mc.carrier._log("hangup", call_sid)
            await call._on_line_disconnected(None, None)
            await asyncio.Event().wait()
        if disconnect_during_request:
            mc.carrier.hangup = hangup
        result = record_decision(trace, make_result("end", call_phase="ended"),
            task_id=spec.task_id, call_id=spec.call_id, turn_seq=8)
        task = asyncio.create_task(mc.actions.execute(result))
        call._background.add(task)
        try:
            await wait_until(lambda: mc.sonic.injections)
            await mc.sonic.emit_response(SonicResponse(text="Thank you. Goodbye.", audio_ms=200))
            await settle()
            await mc.clock.advance(engine.handoff.announce_idle_ms + 100)
            await wait_until(lambda: snapshots, what="results callback")
            outcome = await asyncio.gather(task, return_exceptions=True)
            if disconnect_during_request:
                assert isinstance(outcome[0], asyncio.CancelledError)
            else:
                assert outcome[0].ok
            assert [e.event_type for e in ending_events(trace)] == ["hangup", "call_ended"]
            hangup_event, ended_event = ending_events(trace)
            assert hangup_event.decision_id == result.decision_id
            assert trace.has_decision(hangup_event.decision_id)
            assert ended_event.action == {"reason": "hangup"}
            assert snapshots == [["hangup", "call_ended"]]
            assert call.cancel_snapshots == snapshots
            assert deps.on_ended.calls == [spec.call_id]
            await call._finish(spec.call_id)
            await call._on_line_disconnected(None, None)
            assert len(ending_events(trace)) == 2
            assert deps.on_ended.calls == [spec.call_id]
        finally:
            task.cancel()
            await asyncio.gather(task, *call._background, return_exceptions=True)
            mc.carrier.hangup = original


async def test_r12_remote_disconnect_records_company_hangup(spec, deps, engine, trace, make_fake_sonic):
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        call, snapshots = bind_call(mc, spec, deps, trace)
        await call._on_line_disconnected(None, None)
        await wait_until(lambda: snapshots)
        assert [e.event_type for e in ending_events(trace)] == ["remote_hangup", "call_ended"]
        assert ending_events(trace)[-1].action == {"reason": "company_hangup"}
        assert snapshots == [["remote_hangup", "call_ended"]]
        assert call.cancel_snapshots == snapshots
        assert mc.carrier.names() == []
        await call._finish(spec.call_id)
        assert deps.on_ended.calls == [spec.call_id]


async def test_r12_shutdown_records_explicit_reason_without_hangup(spec, deps, engine, trace, make_fake_sonic):
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        call, snapshots = bind_call(mc, spec, deps, trace)
        # Even a carrier that closes its socket during shutdown preserves shutdown's reason.
        async def hangup(call_sid):
            mc.carrier._log("hangup", call_sid)
            await call._on_line_disconnected(None, None)
            await settle()
        mc.carrier.hangup = hangup
        await call.hangup("shutdown")
        assert [e.event_type for e in ending_events(trace)] == ["call_ended"]
        assert ending_events(trace)[0].action == {"reason": "shutdown"}
        assert snapshots == [["call_ended"]]
        assert call.cancel_snapshots == snapshots
        assert deps.on_ended.calls == [spec.call_id]


async def test_r12_finish_reentry_guardrail(spec, deps, engine, trace, make_fake_sonic):
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        call, snapshots = bind_call(mc, spec, deps, trace)
        await asyncio.gather(call._finish(spec.call_id), call._finish(spec.call_id))
        assert deps.on_ended.calls == [spec.call_id]
        assert snapshots == [[e.event_type for e in ending_events(trace)]]
        assert all(sum(e.event_type == kind for e in ending_events(trace)) <= 1
                   for kind in ("hangup", "remote_hangup", "call_ended"))
