"""H3A-2: keep draining audio while listening, answering and waiting."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
import yaml
from websockets.exceptions import ConnectionClosedOK

from a_support import ENGINE_YAML
from fakes.polly import tone_pcm16
from waitless import cli
from waitless.models import HANDOFF_FRAME_BYTES, HANDOFF_SAMPLE_RATE

THRESHOLD = yaml.safe_load(ENGINE_YAML.read_text())["vad"]["threshold_dbfs"]
TONE = tone_pcm16(20, HANDOFF_SAMPLE_RATE)
SILENCE = bytes(HANDOFF_FRAME_BYTES)


class HolderSocket:
    def __init__(self, frames=(), *, continuous=False, ended_at=None, closed=False, send_delay=0):
        self.frames = list(frames)
        self.continuous = continuous
        self.ended_at = ended_at
        self.closed = closed
        self.send_delay = send_delay
        self.started = time.monotonic()
        self.received = []
        self.sent = []
        self.recv_finished = False

    async def recv(self):
        try:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            if self.ended_at is not None and now - self.started >= self.ended_at:
                if self.closed:
                    raise ConnectionClosedOK(None, None)
                return json.dumps({"type": "ended", "reason": "company_hangup"})
            index = len(self.received)
            frame = self.frames[index] if index < len(self.frames) else TONE if self.continuous else SILENCE
            self.received.append((now, frame))
            return frame
        except asyncio.CancelledError:
            self.recv_finished = True
            raise

    async def send(self, frame):
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        self.sent.append((time.monotonic(), frame))


def plan(*, wait=600):
    return SimpleNamespace(frames=cli._holder_audio_frames(b"\x01\x02" * 1500),
                           return_after_ms=300, question_wait_ms=wait)


async def simulate(ws, *, wait=600):
    return await cli._simulate_account_holder(ws, plan(wait=wait), threshold_dbfs=THRESHOLD,
                                              question_end_silence_ms=200, frame_ms=20)


async def test_question_then_answer_keeps_reading_through_return_wait():
    ws = HolderSocket([SILENCE] * 5 + [TONE] * 25)
    assert await simulate(ws, wait=1000) == "answered"
    returned = time.monotonic()
    last_voice = max(t for t, frame in ws.received if frame == TONE)
    assert ws.sent[0][0] >= last_voice + 0.2 - 0.03
    assert len(ws.sent) == 5
    assert all(len(frame) == HANDOFF_FRAME_BYTES for _, frame in ws.sent)
    assert ws.sent[-1][1][-200:] == bytes(200)
    assert all(b[0] - a[0] >= 0.015 for a, b in zip(ws.sent, ws.sent[1:]))
    assert returned >= ws.sent[-1][0] + 0.3 - 0.03
    assert sum(ws.sent[0][0] <= t <= ws.sent[-1][0] for t, _ in ws.received) >= 3
    assert sum(ws.sent[-1][0] < t <= returned for t, _ in ws.received) >= 10
    count = len(ws.received)
    await asyncio.sleep(0.1)
    assert len(ws.received) == count
    assert ws.recv_finished


@pytest.mark.parametrize("closed", [False, True])
async def test_ended_or_closed_stops_without_sending(closed):
    ws = HolderSocket(ended_at=0.06, closed=closed)
    assert await simulate(ws) == "ended"
    assert time.monotonic() - ws.started < 0.2
    assert not ws.sent


@pytest.mark.parametrize("continuous", [False, True])
async def test_question_timeout_never_sends_before_speech_finishes(continuous, capsys):
    ws = HolderSocket(continuous=continuous)
    assert await simulate(ws) == "no_question"
    assert 0.6 <= time.monotonic() - ws.started < 0.9
    assert "holder question not heard" in capsys.readouterr().err
    assert not ws.sent
    assert ws.recv_finished


@pytest.mark.parametrize("ended_at", [0.28, 0.45])
async def test_ended_during_answer_or_return_wait_stops_promptly(ended_at):
    ws = HolderSocket([TONE], ended_at=ended_at)
    assert await simulate(ws) == "ended"
    assert time.monotonic() - ws.started < ended_at + 0.1
    assert 0 < len(ws.sent) <= 5


async def test_slow_sends_do_not_catch_up_in_a_burst():
    ws = HolderSocket([TONE], send_delay=0.025)
    assert await simulate(ws) == "answered"
    assert all(b[0] - a[0] >= 0.015 for a, b in zip(ws.sent, ws.sent[1:]))


async def test_blocked_send_has_a_bound_and_reader_is_reaped():
    ws = HolderSocket([TONE], send_delay=10)
    with pytest.raises(TimeoutError):
        await simulate(ws)
    assert time.monotonic() - ws.started < 1.1
    assert ws.recv_finished


async def test_ended_interrupts_blocked_send():
    ws = HolderSocket([TONE], ended_at=0.3, send_delay=10)
    assert await simulate(ws) == "ended"
    assert time.monotonic() - ws.started < 0.4
    assert not ws.sent


async def test_connection_closed_while_sending_is_normal():
    ws = HolderSocket([TONE])

    async def closed_send(frame):
        raise ConnectionClosedOK(None, None)

    ws.send = closed_send
    assert await simulate(ws) == "ended"
    assert ws.recv_finished


async def test_outer_cancellation_reaps_reader_and_sender():
    ws = HolderSocket([TONE], send_delay=10)
    task = asyncio.create_task(simulate(ws))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ws.recv_finished
    count = len(ws.received)
    await asyncio.sleep(0.1)
    assert len(ws.received) == count
