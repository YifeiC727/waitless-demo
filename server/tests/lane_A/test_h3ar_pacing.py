"""H3A-R1: ordinary wake-up error must not accumulate over a reply."""

import asyncio
from types import SimpleNamespace

import pytest

from waitless import cli
from waitless.models import HANDOFF_FRAME_BYTES, HANDOFF_SAMPLE_RATE
from .test_h3a2_holder_sim import THRESHOLD, TONE


@pytest.mark.parametrize("wake_error_ms", [0, 1])
@pytest.mark.parametrize("late_frame", [None, 87])
async def test_h3ar_175_frames_keep_the_start_anchored_cadence(
    monkeypatch, wake_error_ms, late_frame,
):
    frame_seconds = HANDOFF_FRAME_BYTES / 2 / HANDOFF_SAMPLE_RATE
    frame_count = 175
    plan = SimpleNamespace(frames=[TONE] * frame_count,
                           question_wait_ms=2000, return_after_ms=300)
    clock = SimpleNamespace(now=0.0)
    wake_error = wake_error_ms / 1000
    late_error = 0.007  # Less than one frame: the start must stay anchored.
    sent = []
    injected = []
    recv_finished = False

    class Socket:
        async def recv(self):
            nonlocal recv_finished
            if not getattr(self, "heard_question", False):
                self.heard_question = True
                return TONE
            try:
                await asyncio.get_running_loop().create_future()
            finally:
                recv_finished = True

        async def send(self, frame):
            sent.append((clock.now, frame))

    real_wait = asyncio.wait
    yield_control = asyncio.sleep

    async def virtual_wait(tasks, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
        if timeout is None:
            # Keep the actual reader/sender race and cancellation handling.
            return await real_wait(tasks, return_when=return_when)
        tasks = set(tasks)
        await yield_control(0)
        if not all(task.done() for task in tasks):
            delay = 0.0
            if timeout > 0 and 0 < len(sent) < frame_count:
                delay = wake_error + (late_error if len(sent) == late_frame else 0)
                injected.append((len(sent), delay))
            # Only explicit virtual waits advance time; host load cannot do so.
            clock.now += max(0, timeout) + delay
            await yield_control(0)
        done = {task for task in tasks if task.done()}
        return done, tasks - done

    # Scope the clock to this coroutine, including the real timeout context.
    # Zero-delay yields keep tasks runnable without any wall-clock wait.
    with monkeypatch.context() as patches:
        patches.setattr(asyncio.get_running_loop(), "time", lambda: clock.now)
        patches.setattr(asyncio, "wait", virtual_wait)
        result = await cli._simulate_account_holder(
            Socket(), plan, threshold_dbfs=THRESHOLD,
            question_end_silence_ms=200, frame_ms=20,
        )

    assert result == "answered"
    assert [frame for _, frame in sent] == plan.frames
    assert [index for index, _ in injected] == list(range(1, frame_count))
    for i, (actual, _) in enumerate(sent):
        expected = sent[0][0] + i * frame_seconds
        if i:
            expected += wake_error + (late_error if i == late_frame else 0)
        # Nanosecond allowance covers float arithmetic, not scheduling drift.
        assert actual == pytest.approx(expected, rel=0, abs=1e-9), f"frame {i}"
    assert clock.now == pytest.approx(sent[-1][0] + plan.return_after_ms / 1000,
                                     rel=0, abs=1e-9)
    assert recv_finished
