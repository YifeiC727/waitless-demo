"""D-s5: buffered turns must not write trace inside the private window."""

import asyncio

from call_engine.conftest import compiled, deps, engine, gate, line_number, settings, spec, store, trace
from call_engine._support import drive, handoff_round, record_decision, settle, wait_until
from call_engine.test_resume import harness


async def test_pending_turn_cannot_be_traced_by_handoff_zero_audio(
    monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store, engine,
):
    h = harness(monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store)
    try:
        await h.start()
        await h.connect_line()
        await h.speech(engine.vad.frame_ms)
        await h.sonic.emit_user_text("One more sentence before joining.")
        result = record_decision(trace, handoff_round(), task_id=spec.task_id, call_id=spec.call_id, turn_seq=0)
        task = asyncio.create_task(h.call._actions.execute(result))
        await wait_until(lambda: h.sonic.injections, what="handoff announcement")
        await h.sonic_plays_pending()
        await drive(task, fake_clock)
        assert h.call._media_gate.in_handoff
        before = trace.events(call_id=spec.call_id)
        await h.silence(engine.turns.turn_end_silence_ms + engine.vad.frame_ms)
        await settle()
        assert trace.events(call_id=spec.call_id) == before
        assert not gate.round_calls
    finally:
        await h.close()
