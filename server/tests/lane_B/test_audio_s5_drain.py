"""Tool completion must preserve audio already queued for the caller."""
import asyncio
from unittest.mock import AsyncMock

from pipecat.frames.frames import OutputAudioRawFrame, LLMFullResponseEndFrame

from tests.lane_B.test_company_bot_run import launch, setup
from waitless.audio.codec import ulaw_encode
from waitless.call.line_protocol import media_payload_bytes, new_call_sid, parse_line_message
from waitless.company.carrier import CarrierCall
from waitless.company.config import load_company, load_scenario
from waitless.company.engine import ScenarioEngine
from waitless.models import CarrierDialIn


async def test_end_call_drains_preceding_bot_audio(setup):
    setup[0]._enter("rep")
    socket, task, service, _, _ = await launch(setup, "rep")
    pcm = b"\x40\x03" * (160 * 9 + 17)
    try:
        await service.push_frame(OutputAudioRawFrame(pcm, 8000, 1))
        await service.push_frame(LLMFullResponseEndFrame())
        assert (await service.tool("end_call"))["ok"]
        await asyncio.wait_for(task, 3)
        expected = ulaw_encode(pcm).ljust(160 * 10, b"\xff")
        assert b"".join(media_payload_bytes(m) for m in socket.sent) == expected
        assert socket.closed and service.cancelled
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_carrier_plays_queued_bot_audio_before_stop(company_dir, mode_env, monkeypatch):
    import websockets.asyncio.client
    from waitless.company import carrier

    class AtRep(ScenarioEngine):
        def start(self):
            self._enter("rep")
            return []

    monkeypatch.setattr(carrier, "ScenarioEngine", AtRep)
    company = load_company(company_dir, "lakeshore-energy")
    env = mode_env("live-sim", company=True)
    call = CarrierCall(dial=CarrierDialIn(call_id="drain-call", task_id="drain-task",
        to_number=company.line_number, stream_url=env["WAITLESS_LINE_WS_URL"],
        stream_token="drain-test-token"), call_sid=new_call_sid(),
        scenario_id="baseline", company=company)
    call._scenario = load_scenario(company_dir, company.company_id, "baseline")
    call._prompt_cache = object()
    line, bot = AsyncMock(), AsyncMock()
    line.recv.side_effect = asyncio.Event().wait
    monkeypatch.setattr(websockets.asyncio.client, "connect", AsyncMock(return_value=line))
    payloads = [bytes([190 + i]) * 160 for i in range(7)]

    async def open_bot(role):
        call._bot_ws, call._bot_step = bot, call._engine.current_step

        async def last_audio_and_close():
            call._bot_audio.extend(payloads)
            call._engine._finish()

        call._bot_receive = asyncio.create_task(last_audio_and_close())

    monkeypatch.setattr(call, "_open_bot", open_bot)
    await asyncio.wait_for(call.run(), 3)
    messages = [parse_line_message(args.args[0], direction="to_engine")
                for args in line.send.await_args_list]
    actual = [media_payload_bytes(m) for m in messages if m.event == "media"]
    assert [p for p in actual if p != b"\xff" * 160] == payloads
    assert messages[-1].event == "stop"
    line.close.assert_awaited_once()
    assert call._bot_receive is None
