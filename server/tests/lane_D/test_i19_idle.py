"""I-19 F6: strict idle policy and the real Pipecat idle wait boundary."""
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.frames.frames import InputAudioRawFrame
from pipecat.pipeline import worker
from waitless import models as m
from waitless.config import load_engine_config, ConfigError
from waitless.call.pipeline import build_pipeline, LivePipelineParts


@pytest.mark.parametrize('bad', ['missing', None, True, False, '1800', 0, -1, 1.5, 1800.0])
def test_idle_timeout_is_required_strict_positive(engine_config_path, tmp_path, bad):
    data = yaml.safe_load(engine_config_path.read_text())
    data['call'] = {} if bad == 'missing' else {'idle_timeout_s': bad}
    with pytest.raises(ValidationError):
        m.EngineConfig.model_validate(data)
    path = tmp_path / 'engine.yaml'
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_engine_config(path)


def test_idle_section_required_and_shipped(engine_config_path):
    data = yaml.safe_load(engine_config_path.read_text())
    assert data['call']['idle_timeout_s'] == 1800
    assert m.EngineConfig.model_fields['call'].is_required()
    cfg = m.EngineConfig.model_validate(data)
    assert cfg.call.idle_timeout_s == 1800
    del data['call']
    with pytest.raises(ValidationError):
        m.EngineConfig.model_validate(data)


@pytest.mark.parametrize('seconds,valid', [(1600, True), (1800, True), (1801, False)])
def test_long_hold_scenario_limit(company_dir, seconds, valid):
    data = yaml.safe_load((company_dir/'bluefin-harbor-bank/scenarios/long-hold.yaml').read_text())
    data['max_call_seconds'] = seconds
    if valid:
        assert m.Scenario.model_validate(data).max_call_seconds == seconds
    else:
        with pytest.raises(ValidationError):
            m.Scenario.model_validate(data)


@pytest.mark.parametrize('configured', [1800, 1900])
async def test_real_pipeline_monitor_waits_past_300_until_configured_deadline(engine_config, monkeypatch, configured):
    # Construct the project pipeline and its real PipelineTask; replace only the
    # worker's wait point so no wall-clock minutes pass and no services start.
    settings = SimpleNamespace(engine=SimpleNamespace(audio=engine_config.audio,
        call=SimpleNamespace(idle_timeout_s=configured)))
    transport = SimpleNamespace(input=FrameProcessor, output=FrameProcessor)
    context = SimpleNamespace(user=FrameProcessor, assistant=FrameProcessor)
    parts = LivePipelineParts(FrameProcessor(), context, FrameProcessor(), FrameProcessor(), FrameProcessor())
    pipe, task = build_pipeline(None, transport, settings=settings, parts=parts)
    task._cancel_runner_on_idle_timeout = False
    cancelled = []
    elapsed = []
    async def cancel(**kwargs):
        cancelled.append((elapsed[-1], kwargs))
    monkeypatch.setattr(task, 'cancel', cancel)
    async def wait_point(waiter, *, timeout):
        waiter.close()
        # Ordinary media does not reset Pipecat's silence timer.
        idle_observer = next(o for o in task._observer._observers if isinstance(o, worker.IdleFrameObserver))
        await idle_observer.on_push_frame(SimpleNamespace(frame=InputAudioRawFrame(b'\0\0'*320, 16000, 1)))
        assert not task._idle_event.is_set()
        elapsed.append(min(300, timeout))
        assert cancelled == []
        elapsed.append(timeout)
        raise TimeoutError
    monkeypatch.setattr(worker, 'asyncio', SimpleNamespace(wait_for=wait_point))
    await task._idle_monitor_handler()
    assert cancelled == [(configured, {'reason': 'idle timeout'})]
    assert elapsed == [300, configured]
    await pipe.cleanup()
