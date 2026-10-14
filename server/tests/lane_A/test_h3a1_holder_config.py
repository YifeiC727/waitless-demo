"""H3A-1: validate and synthesize holder audio before dialing."""

import threading

import pytest
import yaml

from waitless import cli, config
from waitless.company import ivr_audio
from waitless.models import HANDOFF_FRAME_BYTES, HANDOFF_SAMPLE_RATE
from .h3_support import HOLDER_SAY
from .test_r9_live_pending import pending_check


@pytest.fixture
def holder_check(pending_check, monkeypatch):
    check = pending_check
    check.handlers.outputs["get_call_status"] = check.handlers.outputs["get_call_status"].model_copy(
        update={"ended": True, "pending_choice": None},
    )
    check.path = check.settings.company_dir / "lakeshore-energy" / "scenarios" / "baseline.yaml"
    check.synth = ivr_audio.synthesize_prompt
    check.polly = cli._create_holder_polly_client.return_value
    monkeypatch.setattr(config, "load_settings", lambda env: check.settings)
    for name in config.REQUIRED_FOR_COMPANY:
        monkeypatch.setenv(name, "configured")
    return check


async def test_synthesis_precedes_scenario_selection_and_dial(holder_check):
    check = holder_check
    order = []
    main_thread = threading.get_ident()

    def synth(*args, **kwargs):
        assert threading.get_ident() != main_thread
        order.append("synth")
        return b"\x01\x02" * 1500

    check.synth.side_effect = synth
    check.carrier.set_scenario.side_effect = lambda *args: order.append("scenario")
    original = check.handlers.start_assisted_call

    async def start(**kwargs):
        order.append("dial")
        return await original(**kwargs)

    check.handlers.start_assisted_call = start
    report = await cli.check_live("baseline", check.settings)
    assert report.call_id
    assert order == ["synth", "scenario", "dial"]
    check.synth.assert_called_once_with(HOLDER_SAY, voice="Salli", polly_client=check.polly,
                                       sample_rate=HANDOFF_SAMPLE_RATE)


@pytest.mark.parametrize("field,value", [
    ("holder_reply", None), ("holder_reply.say", ""), ("holder_reply.polly_voice", 5),
    ("holder_prompt_ms", "3000"), ("holder_max_asks", 0),
    ("reply_end_silence_ms", True), ("scripted", None),
])
def test_invalid_config_exits_before_dial_and_cleans_up(holder_check, field, value, capsys):
    check = holder_check
    raw = yaml.safe_load(check.path.read_text())
    if field == "scripted":
        del raw["steps"]["rep"]["scripted"]
    else:
        target = raw["steps"]["rep"]["scripted"]
        keys = field.split(".")
        for key in keys[:-1]:
            target = target[key]
        if value is None:
            del target[keys[-1]]
        else:
            target[keys[-1]] = value
    check.path.write_text(yaml.safe_dump(raw))
    assert cli.main(["check", "live", "--scenario", "baseline"]) == 2
    assert "steps.rep.scripted" + ("." + field if field != "scripted" else "") in capsys.readouterr().err
    assert "start_assisted_call" not in [name for name, _ in check.handlers.calls]
    check.carrier.set_scenario.assert_not_awaited()
    check.synth.assert_not_called()
    assert check.order == ["hangup", "server_exit", "company_terminate", "store_close"]


async def test_no_handoff_does_not_read_holder_fields(holder_check):
    check = holder_check
    raw = yaml.safe_load(check.path.read_text())
    rep = raw["steps"]["rep"]
    rep["text_script"] = [line for line in rep["text_script"]
                          if line.get("expect", {}).get("intent") != "request_handoff"
                          and line.get("expect", {}).get("action") != "handoff"]
    del rep["scripted"]
    check.path.write_text(yaml.safe_dump(raw))
    await cli.check_live("baseline", check.settings)
    assert "start_assisted_call" in [name for name, _ in check.handlers.calls]
    check.synth.assert_not_called()


def test_holder_frames_are_padded():
    frames = cli._holder_audio_frames(b"\x01\x02" * 1500)
    assert len(frames) == 5
    assert all(len(frame) == HANDOFF_FRAME_BYTES for frame in frames)
    assert b"".join(frames)[:3000] == b"\x01\x02" * 1500
    assert frames[-1][-200:] == bytes(200)


@pytest.mark.parametrize("failure", [b"", RuntimeError("Polly failed")])
async def test_synthesis_failure_does_not_dial_and_cleans_up(holder_check, failure):
    check = holder_check
    if isinstance(failure, Exception):
        check.synth.side_effect = failure
    else:
        check.synth.return_value = failure
    with pytest.raises(ExceptionGroup) as caught:
        await cli.check_live("baseline", check.settings)
    cause = caught.value
    while isinstance(cause, BaseExceptionGroup):
        assert len(cause.exceptions) == 1
        cause = cause.exceptions[0]
    assert isinstance(cause, RuntimeError)
    assert "start_assisted_call" not in [name for name, _ in check.handlers.calls]
    assert check.order == ["hangup", "server_exit", "company_terminate", "store_close"]
