"""A prior FINAL cannot cause a delayed decision inside the private handoff."""
from lane_D.agent_support import *


async def test_pending_turn_cannot_be_traced_by_handoff_zero_audio(agent_call):
    h = agent_call
    await h.boot()
    await h.sonic.emit_user_text('One more sentence before joining.')
    await h.tool('request_handoff', {'reason': 'Verification'})
    before = h.events()
    await h.silence(h.settings.engine.turns.turn_end_silence_ms + h.settings.engine.vad.frame_ms)
    await h.sonic.emit_user_text('Private verification details.')
    assert h.events() == before
    assert not h.events('decision') or all(e.decision_point == 'agent_tool' for e in h.events('decision'))
