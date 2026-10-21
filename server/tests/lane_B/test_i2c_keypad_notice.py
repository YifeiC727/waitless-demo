"""Exercise keypad notices through the real pipeline with no Sonic echo."""
import asyncio
import re
import json
from contextlib import asynccontextmanager

import pytest
from loguru import logger
from pipecat.adapters.services.aws_nova_sonic_adapter import Role
from pipecat.frames.frames import (
    CancelFrame, InputAudioRawFrame, InterruptionFrame, LLMContextFrame,
    LLMFullResponseEndFrame, LLMFullResponseStartFrame, OutputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameProcessor

from tests.company._signals import pipecat_dtmf_pcm
from tests.lane_B.test_company_bot_run import Sonic, launch, setup
from waitless.audio.codec import silence_pcm16, ulaw_encode
from waitless.call.line_protocol import LINE_FRAME_MS, MULAW_FRAME_BYTES, make_media_in, media_payload_bytes
from waitless.company import bot


class QuietSonic(Sonic):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.notices = []
        self.at_notice = lambda: None
        self.notice_snapshots = []

    async def _send_text_event(self, text, role, interactive=False):
        self.notices.append((text, role, interactive))
        self.notice_snapshots.append(self.at_notice())

    async def process_frame(self, frame, direction):
        # Bypass the echoing Sonic.process_frame used by the existing tests.
        await FrameProcessor.process_frame(self, frame, direction)
        if isinstance(frame, LLMContextFrame):
            self.context.set()
        if isinstance(frame, CancelFrame):
            self.cancelled = True
        if isinstance(frame, InputAudioRawFrame):
            self.audio.append(frame)
        await self.push_frame(frame, direction)


@pytest.fixture
def quiet_setup(setup, monkeypatch):
    def factory(**kwargs):
        service = QuietSonic(**kwargs)
        setup[2].append(service)
        return service
    monkeypatch.setattr(bot, "_llm_factory", factory)
    return setup


@asynccontextmanager
async def running(quiet_setup, role="agent"):
    socket, task, service, sid, _ = await launch(quiet_setup, role)
    service.at_notice = lambda: list(socket.sent)
    try:
        yield socket, task, service, sid
    finally:
        if not task.done():
            await socket.incoming.put(None)
        await asyncio.wait_for(task, 3)


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.01)


async def processed(service, frame):
    finished = asyncio.Event()
    async def observe(processor, seen):
        if seen is frame:
            finished.set()
    output = service.next
    output.add_event_handler("on_after_process_frame", observe)
    try:
        await service.push_frame(frame)
        await asyncio.wait_for(finished.wait(), 3)
    finally:
        output.remove_event_handler("on_after_process_frame", observe)


async def send_digits(socket, service, sid, keys):
    rate = MULAW_FRAME_BYTES * 1000 // LINE_FRAME_MS
    ulaw = ulaw_encode(await pipecat_dtmf_pcm(keys, rate=rate) + silence_pcm16(200, rate))
    initial = len(service.audio)
    chunks = (len(ulaw) + MULAW_FRAME_BYTES - 1) // MULAW_FRAME_BYTES
    sequence = getattr(socket, "keypad_sequence", 1)
    for i in range(chunks):
        sequence += 1
        await socket.incoming.put(make_media_in(
            sequence=sequence, stream_sid=sid, chunk=sequence - 1,
            timestamp_ms=(sequence - 2) * LINE_FRAME_MS,
            ulaw=ulaw[i * MULAW_FRAME_BYTES:(i + 1) * MULAW_FRAME_BYTES].ljust(MULAW_FRAME_BYTES, b"\xff")))
    socket.keypad_sequence = sequence
    await until(lambda: len(service.audio) == initial + chunks)


def assert_notice(service):
    assert len(service.notices) == 1
    text, role, interactive = service.notices[0]
    assert text.startswith("[Phone system notice, not spoken by the caller]")
    assert "verify_account" in text
    assert not any(char.isdigit() for char in text)
    assert "deposit" not in text.lower()
    assert role is Role.USER and interactive is True


async def test_i2c_idle_notice_is_once_and_keeps_audio(quiet_setup):
    engine = quiet_setup[0]
    async with running(quiet_setup) as (socket, _, service, sid):
        await send_digits(socket, service, sid, re.sub(r"\D", "", engine._company.customers[0].phone_number) + "#")
        assert engine._bot_account_verified
        assert service.audio and not socket.sent  # no echo
        assert_notice(service)
        await processed(service, LLMFullResponseEndFrame())
        await processed(service, LLMFullResponseEndFrame())
        assert_notice(service)


async def test_i2c_notice_waits_for_response_tail_flush(quiet_setup):
    async with running(quiet_setup) as (socket, _, service, sid):
        await processed(service, LLMFullResponseStartFrame())
        pcm = b"\x40\x03" * (MULAW_FRAME_BYTES * 2 + 17)
        await processed(service, OutputAudioRawFrame(pcm, 8000, 1))
        assert len(socket.sent) == 2
        await send_digits(socket, service, sid, re.sub(r"\D", "", quiet_setup[0]._company.customers[0].phone_number) + "#")
        assert not service.notices
        await processed(service, LLMFullResponseEndFrame())
        assert_notice(service)
        expected = ulaw_encode(pcm).ljust(MULAW_FRAME_BYTES * 3, b"\xff")
        assert b"".join(media_payload_bytes(m) for m in service.notice_snapshots[0]) == expected
        await processed(service, LLMFullResponseEndFrame())
        assert_notice(service)


async def test_i2c_multiple_pending_entries_merge(quiet_setup):
    async with running(quiet_setup) as (socket, _, service, sid):
        await processed(service, LLMFullResponseStartFrame())
        account = re.sub(r"\D", "", quiet_setup[0]._company.customers[0].phone_number)
        await send_digits(socket, service, sid, account + "#" + account + "#")
        assert not service.notices
        await processed(service, LLMFullResponseEndFrame())
        assert_notice(service)


async def test_i2c_incomplete_entry_has_no_notice(quiet_setup):
    async with running(quiet_setup) as (socket, _, service, sid):
        await send_digits(socket, service, sid, "1002")
        assert not quiet_setup[0]._bot_account_verified
        await processed(service, LLMFullResponseEndFrame())
        assert not service.notices


async def test_i2c_pending_notice_dropped_after_transfer(quiet_setup):
    engine = quiet_setup[0]
    async with running(quiet_setup) as (socket, task, service, sid):
        for field in ("address", "start_date"):
            assert (await service.tool("record_detail", field=field, value="service detail"))["ok"]
        await processed(service, LLMFullResponseStartFrame())
        await send_digits(socket, service, sid, re.sub(r"\D", "", engine._company.customers[0].phone_number) + "#")
        assert not service.notices
        await service.push_frame(LLMFullResponseEndFrame())
        # No yield occurs before the tool changes the step, even though the end is queued.
        assert (await service.tool("transfer_to_representative", event="transition_1"))["ok"]
        await asyncio.wait_for(task, 3)
        assert engine.current_step == "rep" and not service.notices


async def test_i2c_persona_waits_for_system_notice(quiet_setup):
    async with running(quiet_setup) as (_, _, service, _):
        prompt = service.options["persona"]
        assert "After requesting keypad entry, wait for the phone system keypad notice before calling verify_account." in prompt
        assert "deposit" not in prompt.lower()
        assert json.loads(prompt.splitlines()[1])["on_done"] == {"transition_1": "rep"}


async def test_i2c_interruption_sends_after_clear_and_never_twice(quiet_setup):
    async with running(quiet_setup) as (socket, _, service, sid):
        await processed(service, LLMFullResponseStartFrame())
        await processed(service, OutputAudioRawFrame(b"\x40\x03" * 17, 8000, 1))
        await send_digits(socket, service, sid, "0#")
        assert not service.notices
        await processed(service, InterruptionFrame())
        assert_notice(service)
        assert service.notice_snapshots[0][-1].event == "clear"
        await processed(service, LLMFullResponseEndFrame())
        assert_notice(service)


async def test_i2c_missing_sender_logs_error_without_ending_call(quiet_setup):
    logs = []
    sink = logger.add(lambda message: logs.append(message), level="ERROR")
    try:
        async with running(quiet_setup) as (socket, task, service, sid):
            service._send_text_event = None
            await send_digits(socket, service, sid, "0#")
            assert not task.done() and not service.notices
            assert any("_send_text_event" in str(message) for message in logs)
    finally:
        logger.remove(sink)


async def test_i2c_new_idle_entry_gets_one_new_notice(quiet_setup):
    async with running(quiet_setup) as (socket, _, service, sid):
        await send_digits(socket, service, sid, "0#")
        assert_notice(service)
        await send_digits(socket, service, sid, "0#")
        assert len(service.notices) == 2
        assert service.notices[0] == service.notices[1]


async def test_i2c_representative_never_gets_keypad_notice(quiet_setup):
    quiet_setup[0]._enter("rep")
    async with running(quiet_setup, "rep") as (socket, _, service, sid):
        await send_digits(socket, service, sid, "100200300#")
        assert not service.notices
