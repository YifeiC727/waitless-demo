"""Company-side Sonic, with scenario tools and no transcript persistence."""
import asyncio
import contextlib
import base64
import json
from typing import Any

from starlette.websockets import WebSocketDisconnect
from waitless.company.engine import ScenarioEngine
from waitless.config import Settings
from waitless.models import CompanyBotRole

__all__ = ["COMPANY_TOOLS", "CompanyBot"]
COMPANY_TOOLS: tuple[str, ...] = (
    "record_detail", "verify_account", "transfer_to_representative",
    "request_account_holder", "account_holder_verified", "present_offer",
    "record_answer", "issue_confirmation", "end_call",
)


def _llm_factory(*, settings: Settings, voice: str, persona: str, tools: Any) -> Any:
    from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService, AudioConfig
    from pipecat.services.aws.nova_sonic.session_continuation import SessionContinuationParams
    return AWSNovaSonicLLMService(
        region=settings.aws_region,
        access_key_id=settings.aws_access_key_id.get_secret_value(),
        secret_access_key=settings.aws_secret_access_key.get_secret_value(),
        settings=AWSNovaSonicLLMService.Settings(
            model=settings.sonic_model_id, voice=voice, system_instruction=persona),
        audio_config=AudioConfig(input_sample_rate=settings.engine.audio.pipeline_in_sample_rate),
        tools=tools, session_continuation=SessionContinuationParams(enabled=True))


def _tool_schemas() -> Any:
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    fields = {
        "record_detail": ("Record a required service detail.", ["field", "value"]),
        "verify_account": ("Check account verification from keypad entry ending in #.", []),
        "transfer_to_representative": ("Transfer after collecting and verifying; select on_done event.", ["event"]),
        "request_account_holder": ("Request the account holder on this call.", []),
        "account_holder_verified": ("Record completed private verification of the account holder.", []),
        "present_offer": ("Present a configured offer.", ["tag"]),
        "record_answer": ("Record the caller's choice for a presented offer.", ["tag", "answer"]),
        "issue_confirmation": ("Generate the configured confirmation after required actions.", []),
        "end_call": ("End this call after speaking the final response.", []),
    }
    return [FunctionSchema(name=name, description=description,
        properties={key: {"type": "string"} for key in keys}, required=keys)
        for name, (description, keys) in fields.items()]


class CompanyBot:
    """agent/rep voices and persona come from the current company scenario."""
    def __init__(self, *, role: CompanyBotRole, engine: ScenarioEngine, settings: Settings) -> None:
        if role not in {"agent", "rep"}:
            raise ValueError("Company bot role must be agent or rep")
        self.role, self.engine, self.settings = role, engine, settings
        self.voice = (engine._company.voices.agent_sonic if role == "agent"
                      else engine._company.voices.rep_sonic)
        step = engine._scenario.steps.get(engine.current_step)
        self.persona = step.persona if step is not None else None
        self.tools = COMPANY_TOOLS

    async def run(self, websocket: Any) -> None:
        """Decode Twilio input, run Pipecat + Sonic, and encode 20 ms output."""
        from waitless.call.line_protocol import (
            StartMsg, StopMsg, MediaMsg, parse_line_message, media_payload_bytes,
            MULAW_FRAME_BYTES, LINE_FRAME_MS, encode_line_message)
        from waitless.audio.codec import ulaw_decode, ulaw_encode
        from waitless.company.dtmf_detect import GoertzelDetector
        from pipecat.audio.resamplers.soxr_stream_resampler import SOXRStreamAudioResampler
        from pipecat.frames.frames import (InputAudioRawFrame, OutputAudioRawFrame,
            LLMContextFrame, LLMFullResponseEndFrame, InterruptionFrame)
        from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.task import PipelineTask, PipelineParams
        from pipecat.pipeline.runner import PipelineRunner

        missing = [env for field, env in (
            ("aws_region", "AWS_REGION"), ("aws_access_key_id", "AWS_ACCESS_KEY_ID"),
            ("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY"),
            ("sonic_model_id", "WAITLESS_SONIC_MODEL_ID")) if not getattr(self.settings, field)]
        if missing:
            raise ValueError("Missing configuration: " + ", ".join(missing))
        wire_rate = self.settings.engine.audio.wire_sample_rate
        input_rate = self.settings.engine.audio.pipeline_in_sample_rate
        if wire_rate != MULAW_FRAME_BYTES * 1000 // LINE_FRAME_MS:
            raise ValueError("Configured wire sample rate differs from the line protocol")
        step_id = self.engine.current_step
        step = self.engine._scenario.steps.get(step_id)
        expected_kind = "ai_agent" if self.role == "agent" else "live_rep"
        if step is None or step.kind != expected_kind or self.engine.ended:
            raise ValueError("Company bot role differs from the active scenario step")
        persona = (step.persona or "") + "\n" + json.dumps({
            "collects": step.collects or [], "verifies": step.verifies,
            "on_done": step.on_done or {},
            "actions": [a.model_dump(exclude_none=True) for a in step.actions or []],
        }) + ("\nUse the company tools to record progress. Account verification requires keypad entry "
               "ending in #. Never invent a confirmation number; speak the tool's result. "
               "Speak your final response before calling end_call. Begin by greeting the caller.")
        schemas = _tool_schemas()
        sonic = _llm_factory(settings=self.settings, voice=self.voice, persona=persona, tools=schemas)
        done = asyncio.Event()
        async def handle_tool(params: Any) -> None:
            if params.function_name not in COMPANY_TOOLS:
                result = {"ok": False, "error": "unknown_tool"}
            elif not isinstance(params.arguments, dict):
                result = {"ok": False, "error": "invalid_arguments"}
            else:
                result = self.engine._bot_tool(params.function_name, params.arguments)
            await params.result_callback(result)
            if self.engine.ended or self.engine.current_step != step_id:
                done.set()
        for name in (*COMPANY_TOOLS, None):
            sonic.register_function(name, handle_tool)
        # Nova 2 starts a response from an interactive USER message; the
        # legacy audio trigger is a no-op for that model.
        context = LLMContext(messages=[
            {"role": "system", "content": persona},
            {"role": "user", "content": "Greet the caller according to your role and current scenario."},
        ], tools=schemas)
        pair = LLMContextAggregatorPair(context, realtime_service_mode=True)
        output_resampler = SOXRStreamAudioResampler(clear_after_secs=None)
        input_resampler = SOXRStreamAudioResampler(quality="QQ", clear_after_secs=None)
        detector = GoertzelDetector(sample_rate=wire_rate)
        stream_sid = None
        pending = bytearray()

        class Output(FrameProcessor):
            async def emit(self, pcm: bytes, *, flush: bool = False) -> None:
                pending.extend(ulaw_encode(pcm))
                while len(pending) >= MULAW_FRAME_BYTES or (flush and pending):
                    payload = bytes(pending[:MULAW_FRAME_BYTES]).ljust(MULAW_FRAME_BYTES, b"\xff")
                    del pending[:MULAW_FRAME_BYTES]
                    msg = MediaMsg(event="media", streamSid=stream_sid,
                        media={"payload": base64.b64encode(payload).decode('ascii')})
                    await websocket.send_text(encode_line_message(msg))
                    await asyncio.sleep(LINE_FRAME_MS / 1000)
            async def process_frame(self, frame: Any, direction: Any) -> None:
                await super().process_frame(frame, direction)
                if direction == FrameDirection.DOWNSTREAM:
                    if isinstance(frame, OutputAudioRawFrame):
                        if frame.num_channels != 1:
                            raise ValueError("Company audio must be mono PCM16")
                        await self.emit(await output_resampler.resample(frame.audio, frame.sample_rate, wire_rate))
                    elif isinstance(frame, LLMFullResponseEndFrame):
                        await self.emit(await output_resampler.flush(), flush=True)
                    elif isinstance(frame, InterruptionFrame):
                        pending.clear()
                        await output_resampler.reset()
                        from waitless.call.line_protocol import ClearMsg
                        await websocket.send_text(encode_line_message(ClearMsg(event="clear", streamSid=stream_sid)))
                await self.push_frame(frame, direction)

        task = PipelineTask(Pipeline([pair.user(), sonic, Output(), pair.assistant()]),
            params=PipelineParams(audio_in_sample_rate=input_rate, audio_out_sample_rate=input_rate),
            idle_timeout_secs=None)
        runner = PipelineRunner(handle_sigint=False)
        pipeline_run = receive = ending = None
        try:
            while True:
                msg = parse_line_message(await websocket.receive_text(), direction="to_engine")
                if isinstance(msg, StartMsg):
                    stream_sid = msg.streamSid
                    call_sid = msg.start.callSid
                    break
                if isinstance(msg, StopMsg):
                    return
            pipeline_run = asyncio.create_task(runner.run(task))
            await task.queue_frame(LLMContextFrame(context))
            await sonic.trigger_assistant_response()
            ending = asyncio.create_task(done.wait())
            while not done.is_set():
                receive = asyncio.create_task(websocket.receive_text())
                finished, _ = await asyncio.wait({receive, pipeline_run, ending}, return_when=asyncio.FIRST_COMPLETED)
                if pipeline_run in finished:
                    pipeline_run.result()
                    break
                if ending in finished:
                    break
                msg = parse_line_message(receive.result(), direction="to_engine")
                if getattr(msg, "streamSid", stream_sid) != stream_sid:
                    raise ValueError("Company media stream identifier differs from start")
                if isinstance(msg, StopMsg):
                    if msg.stop.callSid != call_sid:
                        raise ValueError("Company stop call identifier differs from start")
                    break
                if isinstance(msg, StartMsg):
                    raise ValueError("Company stream already started")
                if isinstance(msg, MediaMsg):
                    pcm = ulaw_decode(media_payload_bytes(msg))
                    for digit in detector.feed(pcm):
                        self.engine._bot_on_digits(digit)
                    audio = await input_resampler.resample(pcm, wire_rate, input_rate)
                    if audio:
                        await task.queue_frame(InputAudioRawFrame(audio, input_rate, 1))
        except WebSocketDisconnect:
            pass
        finally:
            for waiter in (receive, ending):
                if waiter is not None:
                    waiter.cancel()
            await asyncio.gather(*(t for t in (receive, ending) if t is not None), return_exceptions=True)
            try:
                if pipeline_run is not None:
                    await task.cancel()
                    await pipeline_run
            finally:
                with contextlib.suppress(WebSocketDisconnect):
                    await websocket.close()
