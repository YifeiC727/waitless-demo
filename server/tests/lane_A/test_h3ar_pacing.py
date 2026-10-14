"""H3A-R1: ordinary wake-up error must not accumulate over a reply."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from waitless import cli
from waitless.models import HANDOFF_FRAME_BYTES, HANDOFF_SAMPLE_RATE
from .test_h3a2_holder_sim import HolderSocket, THRESHOLD, TONE


@pytest.mark.parametrize("busy_ms", [0, 1])
async def test_h3ar_175_frames_keep_the_start_anchored_cadence(busy_ms):
    frame_seconds = HANDOFF_FRAME_BYTES / 2 / HANDOFF_SAMPLE_RATE
    frame_count = 175
    plan = SimpleNamespace(frames=[TONE] * frame_count,
                           question_wait_ms=2000, return_after_ms=300)
    stop = asyncio.Event()

    async def load():
        while not stop.is_set():
            until = time.perf_counter() + busy_ms / 1000
            while time.perf_counter() < until:
                pass
            await asyncio.sleep(0.01 - busy_ms / 1000)

    background = asyncio.create_task(load()) if busy_ms else None
    ws = HolderSocket([TONE] * 10)
    try:
        result = await cli._simulate_account_holder(
            ws, plan, threshold_dbfs=THRESHOLD,
            question_end_silence_ms=200, frame_ms=20,
        )
    finally:
        stop.set()
        if background is not None:
            await background
    assert result == "answered"
    assert len(ws.sent) == frame_count
    span = ws.sent[-1][0] - ws.sent[0][0]
    ideal = (frame_count - 1) * frame_seconds
    print(f"busy_ms={busy_ms} span={span:.6f}s ideal={ideal:.6f}s lag={span - ideal:.6f}s")
    assert span <= ideal + frame_seconds
    assert ws.recv_finished
