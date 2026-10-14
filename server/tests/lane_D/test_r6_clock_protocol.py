"""I1-5: handoff and live pending timers use only the frozen Clock protocol."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from call_engine.conftest import compiled, line_number, spec
from call_engine._support import FakeGuard, FakeInjector
from waitless import models as m
from waitless.call.actions import LiveActionBackend
from waitless.call.handoff import HandoffTokens
from waitless.interfaces import Clock


class ProtocolClock:
    """Wall time can jump independently of elapsed time; there is no now()."""

    def __init__(self):
        self.elapsed_ms = 9300
        self.wall = datetime(2026, 10, 14, 12, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        self.sleeps = []

    def monotonic_ms(self) -> int:
        return self.elapsed_ms

    def now_iso(self) -> str:
        return self.wall.isoformat()

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed_ms += round(seconds * 1000)
        self.wall += timedelta(seconds=seconds)


def test_handoff_issue_expiry_binding_and_single_use_with_protocol_clock():
    clock = ProtocolClock()
    assert isinstance(clock, Clock)
    assert not hasattr(clock, "now")
    tokens = HandoffTokens(clock, ttl_s=120)
    token, expires_at = tokens.issue("call-clock", "handoff-clock")
    assert datetime.fromisoformat(expires_at) == clock.wall + timedelta(seconds=120)
    assert tokens.consume("other-handoff", token) is None
    assert tokens.consume("handoff-clock", "invalid-token") is None
    clock.elapsed_ms += 119999
    assert tokens.consume("handoff-clock", token) == "call-clock"
    assert tokens.consume("handoff-clock", token) is None


@pytest.mark.parametrize("wall_jump_s", [-86400, 86400])
@pytest.mark.parametrize("age_ms,valid", [(119999, True), (120000, False), (120001, False)])
def test_handoff_ttl_uses_elapsed_time_despite_wall_clock_jumps(wall_jump_s, age_ms, valid):
    clock = ProtocolClock()
    tokens = HandoffTokens(clock, ttl_s=120)
    token, _ = tokens.issue("call-clock", "handoff-clock")
    clock.elapsed_ms += age_ms
    clock.wall += timedelta(seconds=wall_jump_s)
    assert tokens.consume("handoff-clock", token) == ("call-clock" if valid else None)
    assert tokens.consume("handoff-clock", token) is None


@pytest.mark.parametrize("remaining_s,status", [(12.375, "open"), (0, "open"), (-2, "open"), (7, "answered")])
async def test_live_pending_timer_uses_iso_wall_time_and_keeps_timeout_behavior(spec, remaining_s, status):
    clock = ProtocolClock()
    choice = m.PendingChoice(
        choice_id="choice-clock", question_text="Proceed?",
        options=[m.Option(id="decline", label="Decline", detail="")],
        created_at=clock.wall - timedelta(seconds=20),
        expires_at=(clock.wall + timedelta(seconds=remaining_s)).astimezone(timezone.utc),
        status=status,
    )
    pending = SimpleNamespace(get=Mock(return_value=choice), expire_due=Mock(return_value=[choice]))
    injector = FakeInjector()
    actions = LiveActionBackend(
        spec=spec, deps=SimpleNamespace(clock=clock, pending=pending), injector=injector,
        output_guard=FakeGuard(), media_gate=None, pipeline_task=None, carrier=None,
        call_sid="call-clock",
    )
    await actions._expire_pending(choice, "decision-clock")
    assert clock.sleeps == [max(0, remaining_s)]
    pending.get.assert_called_once_with(choice.choice_id)
    if status == "open":
        pending.expire_due.assert_called_once_with()
        assert injector.calls == [("inject", (spec.compiled.say_templates["pending.safe_default"],
            "decision-clock"), {"template": "pending.safe_default"})]
        assert actions.phase == "resuming"
    else:
        pending.expire_due.assert_not_called()
        assert injector.calls == []
        assert actions.phase == "dialing"
