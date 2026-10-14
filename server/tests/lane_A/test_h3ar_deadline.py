"""H3A-R2: a late question still leaves the complete bounded reply budget."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from waitless import cli
from waitless.models import HANDOFF_FRAME_BYTES, HANDOFF_SAMPLE_RATE
from .test_h3a2_holder_sim import HolderSocket, THRESHOLD, TONE


async def test_h3ar_late_question_gets_a_fresh_reply_and_return_budget(monkeypatch):
    frame_seconds = HANDOFF_FRAME_BYTES / 2 / HANDOFF_SAMPLE_RATE
    plan = SimpleNamespace(frames=[TONE] * 175,
                           question_wait_ms=420, return_after_ms=300)
    original_timeout_at = asyncio.timeout_at
    timeouts = []
    question_deadlines = []

    def observe_timeout_at(deadline):
        timeout = original_timeout_at(deadline)
        timeouts.append(timeout)
        question_deadlines.append(deadline - len(plan.frames) * frame_seconds - plan.return_after_ms / 1000)
        return timeout

    monkeypatch.setattr(asyncio, "timeout_at", observe_timeout_at)
    ws = HolderSocket([TONE] * 10)
    first_budget = []
    send = ws.send

    async def observe_send(frame):
        if not ws.sent:
            first_budget.append(timeouts[0].when() - asyncio.get_running_loop().time())
        await send(frame)

    ws.send = observe_send
    result = await cli._simulate_account_holder(
        ws, plan, threshold_dbfs=THRESHOLD,
        question_end_silence_ms=200, frame_ms=20,
    )
    returned = time.monotonic()
    assert result == "answered"
    assert len(ws.sent) == 175
    question_slack = question_deadlines[0] - ws.sent[0][0]
    print(f"question_slack={question_slack:.6f}s reply_budget={first_budget[0]:.6f}s")
    assert 0 < question_slack < 0.03
    required_budget = len(plan.frames) * frame_seconds + plan.return_after_ms / 1000 + frame_seconds
    assert first_budget[0] == pytest.approx(required_budget, abs=0.001)
    assert returned >= ws.sent[-1][0] + plan.return_after_ms / 1000 - 0.03
    assert ws.recv_finished
