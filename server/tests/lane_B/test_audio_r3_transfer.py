"""Drain the agent's generated speech before opening the representative."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import OutputAudioRawFrame, LLMFullResponseEndFrame
from pipecat.pipeline.task import PipelineTask

from tests.lane_B.test_company_bot_run import launch, setup
from waitless.audio.codec import ulaw_encode
from waitless.call.line_protocol import LINE_FRAME_MS, MULAW_FRAME_BYTES, media_payload_bytes, new_call_sid, parse_line_message
from waitless.company.carrier import CarrierCall
from waitless.company.config import load_company, load_scenario
from waitless.company.engine import ScenarioEngine
from waitless.models import CarrierDialIn


async def prepare_transfer(engine, service):
    step = engine._scenario.steps[engine.current_step]
    for field in step.collects:
        assert (await service.tool("record_detail", field=field, value="service detail"))["ok"]
    engine._bot_on_digits(getattr(engine._company.customers[0], step.verifies) + "#")
    assert (await service.tool("verify_account"))["ok"]
    return next(iter(step.on_done))


async def test_transfer_drains_preceding_bot_audio(setup):
    socket, task, service, _, _ = await launch(setup)
    pcm = b"\x40\x03" * (MULAW_FRAME_BYTES * 9 + 17)
    try:
        event = await prepare_transfer(setup[0], service)
        await service.push_frame(OutputAudioRawFrame(pcm, 8000, 1))
        await service.push_frame(LLMFullResponseEndFrame())
        assert (await service.tool("transfer_to_representative", event=event))["ok"]
        await asyncio.wait_for(task, 3)
        expected = ulaw_encode(pcm).ljust(MULAW_FRAME_BYTES * 10, b"\xff")
        assert b"".join(media_payload_bytes(m) for m in socket.sent) == expected
        assert setup[0].current_step == "rep" and socket.closed and service.cancelled
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_carrier_plays_agent_audio_before_opening_rep(company_dir, mode_env, monkeypatch):
    import websockets.asyncio.client
    from waitless.company import carrier

    class AtAgent(ScenarioEngine):
        def start(self):
            self._enter("agent")
            return []

    monkeypatch.setattr(carrier, "ScenarioEngine", AtAgent)
    company = load_company(company_dir, "lakeshore-energy")
    env = mode_env("live-sim", company=True)
    call = CarrierCall(dial=CarrierDialIn(call_id="transfer-call", task_id="transfer-task",
        to_number=company.line_number, stream_url=env["WAITLESS_LINE_WS_URL"],
        stream_token="transfer-test-token"), call_sid=new_call_sid(),
        scenario_id="baseline", company=company)
    call._scenario = load_scenario(company_dir, company.company_id, "baseline")
    call._prompt_cache = object()
    line, agent, rep = AsyncMock(), AsyncMock(), AsyncMock()
    line.recv.side_effect = asyncio.Event().wait
    monkeypatch.setattr(websockets.asyncio.client, "connect", AsyncMock(return_value=line))
    payloads = [bytes([190 + i]) * MULAW_FRAME_BYTES for i in range(7)]
    rep_audio = bytes([210]) * MULAW_FRAME_BYTES
    opened = []
    frames_at_rep = []
    reader_finished = asyncio.Event()
    frame_times = []

    async def sent(raw):
        msg = parse_line_message(raw, direction="to_engine")
        if msg.event == "media" and media_payload_bytes(msg) in payloads:
            frame_times.append(asyncio.get_running_loop().time())

    line.send.side_effect = sent

    async def open_bot(role):
        opened.append(role)
        call._bot_ws = agent if role == "agent" else rep
        call._bot_step = call._engine.current_step
        if role == "rep":
            frames_at_rep.extend(media_payload_bytes(m)
                for args in line.send.await_args_list
                if (m := parse_line_message(args.args[0], direction="to_engine")).event == "media")
            assert reader_finished.is_set()

        async def audio_and_close():
            if role == "agent":
                call._bot_audio.extend(payloads[:5])
                call._engine._enter("rep")
                # The old reader can still deliver audio after the step changes.
                await asyncio.sleep(LINE_FRAME_MS * 2 / 1000)
                call._bot_audio.extend(payloads[5:])
                reader_finished.set()
            else:
                call._bot_audio.append(rep_audio)
                call._engine._finish()

        call._bot_receive = asyncio.create_task(audio_and_close())

    monkeypatch.setattr(call, "_open_bot", open_bot)
    budget = (len(payloads) + 1) * LINE_FRAME_MS / 1000 + 3
    await asyncio.wait_for(call.run(), budget)
    messages = [parse_line_message(args.args[0], direction="to_engine")
                for args in line.send.await_args_list]
    actual = [media_payload_bytes(m) for m in messages if m.event == "media"]
    assert [p for p in actual if p != b"\xff" * MULAW_FRAME_BYTES] == payloads + [rep_audio]
    assert [p for p in frames_at_rep if p != b"\xff" * MULAW_FRAME_BYTES] == payloads
    assert opened == ["agent", "rep"]
    assert all(b - a >= LINE_FRAME_MS / 1000 * .75 for a, b in zip(frame_times, frame_times[1:]))
    # The first loop can send silence before transfer; once changed, only stop remains.
    assert len(agent.send.await_args_list) == 2
    assert parse_line_message(agent.send.await_args_list[-1].args[0], direction="to_engine").event == "stop"
    assert messages[-1].event == "stop"
    line.close.assert_awaited_once()
    assert call._bot_receive is None


async def test_failed_transfer_keeps_bot_running_without_flush(setup, monkeypatch):
    from waitless.call.line_protocol import make_stop
    flushing = AsyncMock(return_value=True)
    monkeypatch.setattr(PipelineTask, "flush_pipeline", flushing)
    socket, task, service, sid, call_sid = await launch(setup)
    try:
        event = next(iter(setup[0]._scenario.steps[setup[0].current_step].on_done))
        assert not (await service.tool("transfer_to_representative", event=event))["ok"]
        pcm = b"\x40\x03" * (MULAW_FRAME_BYTES * 2 + 17)
        await service.push_frame(OutputAudioRawFrame(pcm, 8000, 1))
        await service.push_frame(LLMFullResponseEndFrame())
        for _ in range(100):
            if len(socket.sent) == 3:
                break
            await asyncio.sleep(.01)
        assert b"".join(media_payload_bytes(m) for m in socket.sent) == ulaw_encode(pcm).ljust(
            MULAW_FRAME_BYTES * 3, b"\xff")
        assert not task.done() and setup[0].current_step == "agent"
        flushing.assert_not_awaited()
        await socket.incoming.put(make_stop(sequence=2, stream_sid=sid, call_sid=call_sid))
        await asyncio.wait_for(task, 3)
        flushing.assert_not_awaited()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_transfer_flush_failure_is_explicit_and_cleans_up(setup, monkeypatch):
    monkeypatch.setattr(PipelineTask, "flush_pipeline", AsyncMock(return_value=False))
    socket, task, service, _, _ = await launch(setup)
    event = await prepare_transfer(setup[0], service)
    assert (await service.tool("transfer_to_representative", event=event))["ok"]
    with pytest.raises(RuntimeError, match="output did not finish draining"):
        await asyncio.wait_for(task, 3)
    assert socket.closed and service.cancelled


@pytest.mark.parametrize("ending", ["deadline", "http_hangup"])
async def test_drain_does_not_block_call_limits_or_hangup(company_dir, mode_env, monkeypatch, ending):
    import websockets.asyncio.client
    from waitless.call.line_protocol import encode_line_message, make_media_in
    from waitless.company import carrier

    class AtAgent(ScenarioEngine):
        def start(self):
            self._enter("agent")
            return []

    monkeypatch.setattr(carrier, "ScenarioEngine", AtAgent)
    company = load_company(company_dir, "lakeshore-energy")
    env = mode_env("live-sim", company=True)
    call = CarrierCall(dial=CarrierDialIn(call_id="limit-call", task_id="limit-task",
        to_number=company.line_number, stream_url=env["WAITLESS_LINE_WS_URL"],
        stream_token="limit-test-token"), call_sid=new_call_sid(),
        scenario_id="baseline", company=company)
    call._scenario = load_scenario(company_dir, company.company_id, "baseline").model_copy(
        update={"max_call_seconds": 1})
    call._prompt_cache = object()
    line, agent = AsyncMock(), AsyncMock()
    incoming = asyncio.Queue()
    line.recv.side_effect = incoming.get
    monkeypatch.setattr(websockets.asyncio.client, "connect", AsyncMock(return_value=line))
    changed = asyncio.Event()
    sent_at_change = []
    readers = []

    async def open_bot(role):
        assert role == "agent", "Representative opened before old reader finished"
        call._bot_ws, call._bot_step = agent, call._engine.current_step

        async def never_finish_draining():
            call._engine._enter("rep")
            sent_at_change.append(agent.send.await_count)
            changed.set()
            await asyncio.Event().wait()

        call._bot_receive = asyncio.create_task(never_finish_draining())
        readers.append(call._bot_receive)

    monkeypatch.setattr(call, "_open_bot", open_bot)
    running = asyncio.create_task(call.run())
    try:
        await asyncio.wait_for(changed.wait(), 1)
        await incoming.put(encode_line_message(make_media_in(sequence=2,
            stream_sid=call._stream_sid, chunk=1, timestamp_ms=0, ulaw=b"\xce" * MULAW_FRAME_BYTES)))
        await asyncio.sleep(LINE_FRAME_MS * 3 / 1000)
        assert agent.send.await_count == sent_at_change[0]
        if ending == "http_hangup":
            await asyncio.wait_for(call.hangup(), LINE_FRAME_MS / 1000 + .2)
        await asyncio.wait_for(running, 1.5)
        assert call._closed and all(reader.done() for reader in readers)
        messages = [parse_line_message(args.args[0], direction="to_engine")
                    for args in line.send.await_args_list]
        assert messages[-1].event == "stop"
        line.close.assert_awaited_once()
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
