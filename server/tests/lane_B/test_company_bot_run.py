"""Run a real Pipecat pipeline with offline Sonic and WebSocket substitutes."""
import asyncio
import re
from types import SimpleNamespace
import pytest
from starlette.websockets import WebSocketDisconnect
from pipecat.frames.frames import InputAudioRawFrame, LLMContextFrame, OutputAudioRawFrame, LLMFullResponseEndFrame, CancelFrame
from pipecat.processors.frame_processor import FrameProcessor
from waitless.company import bot
from waitless.company.config import load_company, load_scenario
from waitless.company.engine import ScenarioEngine
from waitless.call.line_protocol import new_stream_sid, new_call_sid, make_start, make_media_in, make_stop, encode_line_message, parse_line_message, media_payload_bytes
from waitless.audio.codec import ulaw_decode, ulaw_encode
from tests.company._live_harness import company_settings

class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False
    async def receive_text(self):
        item = await self.incoming.get()
        if item is None:
            raise WebSocketDisconnect()
        return encode_line_message(item)
    async def send_text(self, text):
        self.sent.append(parse_line_message(text, direction='to_carrier'))
    async def close(self):
        self.closed = True

class Sonic(FrameProcessor):
    def __init__(self, **kwargs):
        super().__init__()
        self.options = kwargs
        self.handlers = {}
        self.context = asyncio.Event()
        self.audio = []
        self.cancelled = False
    def register_function(self, name, handler, **kwargs):
        self.handlers[name] = handler
    async def trigger_assistant_response(self):
        pass
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            self.context.set()
        if isinstance(frame, CancelFrame):
            self.cancelled = True
        if isinstance(frame, InputAudioRawFrame):
            self.audio.append(frame)
            await self.push_frame(OutputAudioRawFrame(b'\x40\x03' * 170, 8000, 1))
            await self.push_frame(LLMFullResponseEndFrame())
        await self.push_frame(frame, direction)
    async def tool(self, name, **arguments):
        results = []
        async def result(value, **kwargs):
            results.append(value)
        await self.handlers.get(name, self.handlers[None])(SimpleNamespace(function_name=name, arguments=arguments, result_callback=result))
        return results[0]

@pytest.fixture
def setup(company_dir, mode_env, engine_config, tmp_path, monkeypatch):
    company = load_company(company_dir, 'lakeshore-energy')
    engine = ScenarioEngine(load_scenario(company_dir, company.company_id, 'baseline'), company)
    engine.start(); engine.on_digits('1'); engine.on_digits('1')
    settings = company_settings(mode_env('live-sim', company=True), engine_config, company_dir, tmp_path)
    instances = []
    def factory(**kwargs):
        service = Sonic(**kwargs)
        instances.append(service)
        return service
    monkeypatch.setattr(bot, '_llm_factory', factory, raising=False)
    return engine, settings, instances

async def launch(setup, role='agent'):
    engine, settings, instances = setup
    previous = len(instances)
    socket = Socket()
    sid, call = new_stream_sid(), new_call_sid()
    await socket.incoming.put(make_start(sequence=1, stream_sid=sid, call_sid=call, call_id='bot-call', task_id='bot-task'))
    task = asyncio.create_task(bot.CompanyBot(role=role, engine=engine, settings=settings).run(socket))
    for _ in range(100):
        if task.done():
            task.result()
        if len(instances) > previous and instances[-1].context.is_set():
            return socket, task, instances[-1], sid, call
        await asyncio.sleep(.01)
    raise AssertionError('Sonic context was never queued')

@pytest.mark.parametrize('role', ['agent', 'rep'])
async def test_voice_persona_and_no_disk_writes(setup, role, tmp_path, monkeypatch):
    engine, settings, _ = setup
    if role == 'rep':
        engine._enter('rep')
    monkeypatch.chdir(tmp_path)
    socket, task, service, _, _ = await launch(setup, role)
    assert service.options['voice'] == getattr(engine._company.voices, role + '_sonic')
    prompt = service.options['persona']
    assert engine._scenario.steps[engine.current_step].persona in prompt
    assert settings.sonic_model_id not in prompt and 'AWS_' not in prompt
    await socket.incoming.put(None)
    await asyncio.wait_for(task, 3)
    assert service.cancelled and socket.closed
    assert list(tmp_path.iterdir()) == []

async def test_media_codec_framing_stop_and_no_background_tasks(setup):
    socket, task, service, sid, call = await launch(setup)
    await socket.incoming.put(make_media_in(sequence=2, stream_sid=sid, chunk=1, timestamp_ms=0, ulaw=b'\xce' * 160))
    for _ in range(100):
        if len(socket.sent) == 2:
            break
        await asyncio.sleep(.01)
    await socket.incoming.put(make_stop(sequence=3, stream_sid=sid, call_sid=call))
    await asyncio.wait_for(task, 3)
    assert service.audio[0].sample_rate == setup[1].engine.audio.pipeline_in_sample_rate
    from pipecat.audio.resamplers.soxr_stream_resampler import SOXRStreamAudioResampler
    reference = SOXRStreamAudioResampler(quality="QQ", clear_after_secs=None)
    assert service.audio[0].audio == await reference.resample(
        ulaw_decode(b'\xce' * 160), 8000, setup[1].engine.audio.pipeline_in_sample_rate)
    expected = ulaw_encode(b'\x40\x03' * 170)
    assert [media_payload_bytes(m) for m in socket.sent] == [expected[:160], expected[160:].ljust(160, b'\xff')]
    assert service.cancelled and socket.closed
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]

async def test_tools_progress_and_unknown_tools_do_not_mutate(setup):
    engine = setup[0]
    socket, task, sonic, _, _ = await launch(setup)
    before = engine.step_log()
    assert (await sonic.tool('unknown'))['error'] == 'unknown_tool'
    assert engine.step_log() == before
    assert not (await sonic.tool('transfer_to_representative', event='deposit_review'))['ok']
    assert (await sonic.tool('record_detail', field='address', value='service address'))['ok']
    assert (await sonic.tool('record_detail', field='start_date', value='next week'))['ok']
    assert not (await sonic.tool('verify_account'))['ok']
    engine._bot_on_digits(re.sub(r"\D", "", engine._company.customers[0].phone_number) + '#')
    assert (await sonic.tool('verify_account'))['ok']
    assert (await sonic.tool('transfer_to_representative', event='deposit_review'))['ok']
    await asyncio.wait_for(task, 3)
    assert engine.current_step == 'rep' and socket.closed
    socket, task, sonic, _, _ = await launch(setup, 'rep')
    assert (await sonic.tool('request_account_holder'))['ok']
    assert (await sonic.tool('account_holder_verified'))['ok']
    assert (await sonic.tool('present_offer', tag='deposit_or_autopay'))['ok']
    assert (await sonic.tool('record_answer', tag='deposit_or_autopay', answer='autopay'))['ok']
    result = await sonic.tool('issue_confirmation')
    assert result['confirmation_number'] == engine.confirmation_number()
    assert result['confirmation_number'].startswith('LE-') and len(result['confirmation_number']) == 8
    assert (await sonic.tool('end_call'))['ok']
    await asyncio.wait_for(task, 3)
    assert engine.ended and socket.closed

async def test_missing_aws_lists_names_without_values(setup):
    engine, settings, instances = setup
    settings = settings.model_copy(update={name: None for name in ('aws_region', 'aws_access_key_id', 'aws_secret_access_key', 'sonic_model_id')})
    with pytest.raises(ValueError) as exc:
        await bot.CompanyBot(role='agent', engine=engine, settings=settings).run(Socket())
    for name in ('AWS_REGION', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'WAITLESS_SONIC_MODEL_ID'):
        assert name in str(exc.value)
    assert not instances


async def test_native_sonic_output_resampled_and_flushed(setup):
    from pipecat.audio.resamplers.soxr_stream_resampler import SOXRStreamAudioResampler
    from pipecat.processors.frame_processor import FrameDirection
    socket, task, service, sid, call = await launch(setup)
    pcm = b'\x00\x10' * 1200
    reference = SOXRStreamAudioResampler(clear_after_secs=None)
    expected_pcm = await reference.resample(pcm, 24000, 8000) + await reference.flush()
    expected = ulaw_encode(expected_pcm)
    await service.push_frame(OutputAudioRawFrame(pcm, 24000, 1), FrameDirection.DOWNSTREAM)
    await service.push_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    for _ in range(100):
        if len(socket.sent) == (len(expected) + 159) // 160:
            break
        await asyncio.sleep(.01)
    await socket.incoming.put(make_stop(sequence=2, stream_sid=sid, call_sid=call))
    await asyncio.wait_for(task, 3)
    assert b''.join(media_payload_bytes(m) for m in socket.sent) == expected.ljust(
        ((len(expected) + 159) // 160) * 160, b'\xff')


async def test_bad_stream_rejected_and_cancelled(setup):
    socket, task, service, sid, call = await launch(setup)
    await socket.incoming.put(make_media_in(sequence=2, stream_sid=new_stream_sid(),
        chunk=1, timestamp_ms=0, ulaw=b'\xff' * 160))
    with pytest.raises(ValueError, match='stream identifier'):
        await asyncio.wait_for(task, 3)
    assert service.cancelled and socket.closed


async def test_invalid_tool_arguments_and_role_do_not_change_state(setup):
    engine = setup[0]
    socket, task, service, _, _ = await launch(setup)
    assert not (await service.tool('record_detail', field='unknown', value='value'))['ok']
    assert not engine._bot_details
    assert not (await service.tool('issue_confirmation'))['ok']
    assert engine.confirmation_number() is None
    engine._bot_on_digits('0#')
    assert not (await service.tool('verify_account'))['ok']
    assert engine.current_step == 'agent'
    await socket.incoming.put(None)
    await asyncio.wait_for(task, 3)


async def test_external_cancellation_cleans_pipeline(setup):
    socket, task, service, _, _ = await launch(setup)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert service.cancelled and socket.closed
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
