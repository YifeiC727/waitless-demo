"""U112-d: a re-anchored reply leaves a full, bounded return wait."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from waitless import cli
from .test_h3a2_holder_sim import HolderSocket, THRESHOLD, TONE


def _plan():
    return SimpleNamespace(frames=[TONE] * 10,
                           question_wait_ms=240, return_after_ms=100)


class HiccupSocket(HolderSocket):
    def __init__(self, *, stuck=False):
        super().__init__([TONE])
        self.stuck = stuck
        self.send_cancelled = False

    async def send(self, frame):
        if len(self.sent) == 5:
            await asyncio.sleep(0.06)
        if self.stuck and len(self.sent) == 7:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.send_cancelled = True
                raise
        await super().send(frame)


async def test_u112_d_1_hiccup_mid_reply_still_returns_answered():
    plan = _plan()
    ws = HiccupSocket()
    result = await cli._simulate_account_holder(
        ws, plan, threshold_dbfs=THRESHOLD,
        question_end_silence_ms=200, frame_ms=20,
    )
    returned = time.monotonic()
    assert result == "answered"
    assert len(ws.sent) == len(plan.frames)
    assert returned - ws.sent[-1][0] >= plan.return_after_ms / 1000
    assert ws.recv_finished


async def test_u112_d_2_hiccup_does_not_unbound_a_stuck_send(monkeypatch):
    plan = _plan()
    ws = HiccupSocket(stuck=True)
    original_timeout_at = asyncio.timeout_at
    timeouts = []
    first_send_deadlines = []

    def observe_timeout_at(deadline):
        timeout = original_timeout_at(deadline)
        timeouts.append(timeout)
        return timeout

    monkeypatch.setattr(asyncio, "timeout_at", observe_timeout_at)
    send = ws.send

    async def observe_send(frame):
        if not ws.sent:
            first_send_deadlines.append(timeouts[0].when())
        await send(frame)

    ws.send = observe_send
    with pytest.raises(TimeoutError):
        await cli._simulate_account_holder(
            ws, plan, threshold_dbfs=THRESHOLD,
            question_end_silence_ms=200, frame_ms=20,
        )
    returned = asyncio.get_running_loop().time()
    deadline = first_send_deadlines[0]
    assert deadline <= returned < deadline + 0.1
    assert timeouts[0].when() == deadline
    assert len(ws.sent) == 7
    assert ws.recv_finished
    assert ws.send_cancelled
