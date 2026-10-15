import asyncio
import base64
import json
import logging
import re
import time
from contextlib import asynccontextmanager

import pytest
from fakes import FakePolly
from fakes.polly import tone_pcm16
from tests.company._live_harness import (
    FakeEngineLine, company_settings, no_proxy, patch_boto3_polly, running_company, wait_until,
)
from tests.company._scenario_data import copy_company_root, edit_scenario_yaml
from tests.company.test_audio_carrier_line import place_call, send_keys, wait_prompt_finished
from waitless.audio.codec import ulaw_encode
from waitless.company.app import create_company_app
from waitless.company.carrier import CarrierCall
from waitless.company.config import load_company, load_scenario


@pytest.fixture
def rig(company_dir, tmp_path, engine_config, mode_env, monkeypatch):
    root = copy_company_root(company_dir, tmp_path)
    def edit(data):
        data['steps']['hold']['seconds'] = 1
        for name in ('agent', 'rep'):
            data['steps'][name]['scripted']['reply_end_silence_ms'] = 300
    edit_scenario_yaml(root, 'baseline', edit)
    engine = engine_config.model_copy(update={
        'handoff': engine_config.handoff.model_copy(update={'announce_idle_ms': 300})})
    env = mode_env('live-sim', company=True)
    host = env['WAITLESS_COMPANY_HOST']
    no_proxy(monkeypatch, host)
    settings = company_settings(env, engine, root, tmp_path, company_stats_path=tmp_path / 'stats.jsonl')
    polly = FakePolly(ms_per_char=1, min_ms=20)
    patch_boto3_polly(monkeypatch, polly)
    times = {}
    synth = polly.synthesize_speech
    def timed(**kw):
        times.setdefault(kw['Text'], time.monotonic())
        return synth(**kw)
    monkeypatch.setattr(polly, 'synthesize_speech', timed)
    opened = []
    async def forbidden(self, role):
        opened.append(role)
        raise AssertionError('scripted mode opened Sonic')
    monkeypatch.setattr(CarrierCall, '_open_bot', forbidden)
    company = load_company(root, 'lakeshore-energy')
    scenario = load_scenario(root, company.company_id, 'baseline')
    return settings, host, polly, times, opened, company, scenario


async def speak(conn, ms):
    frame = base64.b64encode(ulaw_encode(tone_pcm16(20, 8000))).decode('ascii')
    for _ in range(0, ms, 20):
        last = time.monotonic()
        await conn.send({'event': 'media', 'streamSid': conn.stream_sid, 'media': {'payload': frame}})
        await asyncio.sleep(0.02)
    return last


async def wait_line(conn, polly, times, text):
    await wait_until(lambda: text in times, 6, f'Polly line {text}')
    await wait_prompt_finished(conn, quiet_s=0.2, after=times[text] - 0.01)


@asynccontextmanager
async def connect_rig(rig, mode='scripted'):
    settings, host, *_ = rig
    app = create_company_app(settings, agent_mode=mode) if mode == 'scripted' else create_company_app(settings)
    async with FakeEngineLine(host) as engine, running_company(app, host) as base:
        sid, conn = await place_call(base, engine, tag='scripted')
        yield app, app.state.carrier._calls[sid], conn


async def reach_agent(conn):
    await wait_prompt_finished(conn, quiet_s=0.2)
    after = time.monotonic()
    await send_keys(conn, '1')
    await wait_prompt_finished(conn, quiet_s=0.2, after=after)
    await send_keys(conn, '1')


async def reach_keypad(conn, rig):
    _, _, polly, times, _, _, scenario = rig
    agent = scenario.steps['agent'].text_script
    await reach_agent(conn)
    for line in agent[:2]:
        await wait_line(conn, polly, times, line.say)
        await speak(conn, 200)
    await wait_line(conn, polly, times, agent[2].say)


async def test_baseline_on_line(rig, caplog):
    settings, _, polly, times, opened, company, scenario = rig
    caplog.set_level(logging.INFO)
    async with connect_rig(rig) as (app, call, conn):
        await reach_keypad(conn, rig)
        count = len(polly.calls)
        await speak(conn, 300)
        await asyncio.sleep(1)
        assert len(polly.calls) == count
        await send_keys(conn, re.sub(r"\D", "", company.customers[0].phone_number) + '#')
        rep = scenario.steps['rep'].text_script
        await wait_line(conn, polly, times, rep[0].say)
        await speak(conn, 200)
        await wait_line(conn, polly, times, rep[1].say)
        await speak(conn, 200)  # Explanation: before the window.
        await wait_line(conn, polly, times, 'Please confirm your date of birth.')
        assert call._scripted_turns.phase == 'holder_answer'
        await speak(conn, 200)  # Holder answer: ten counted frames.
        await asyncio.sleep(0.45)
        assert call._scripted_turns.phase == 'resume_wait'
        await speak(conn, 200)  # Return announcement: excluded from the count.
        await wait_line(conn, polly, times, rep[2].say)
        await speak(conn, 200)
        await wait_until(lambda: call._engine.confirmation_number() is not None, 6, 'confirmation number')
        confirmation = rep[3].say.replace('{confirmation_number}', call._engine.confirmation_number())
        await wait_line(conn, polly, times, confirmation)
        await speak(conn, 200)
        await wait_until(lambda: conn.closed_at is not None, 6, 'scripted stop')
        assert call._engine.ended
        assert call._engine.step_log() == ['greeting', 'main_menu', 'service_menu', 'hold', 'agent', 'rep']
        assert conn.events()[-1] == 'stop'
        # Scripted lines and the holder question are synthesized on demand.
        expected = [entry.say for entry in scenario.steps['agent'].text_script]
        expected += [rep[0].say, rep[1].say, 'Please confirm your date of birth.', rep[2].say,
                     rep[3].say.replace('{confirmation_number}', call._engine.confirmation_number())]
        speech = [c for c in polly.calls if c['Text'] in expected]
        assert [c['Text'] for c in speech] == expected
        assert [c['VoiceId'] for c in speech] == [company.voices.ivr_polly] * 4 + ['Matthew'] * 5
        assert sum(c['Text'] == 'Please confirm your date of birth.' for c in polly.calls) == 1
        assert opened == []
        assert len(app.state.carrier._prompt_caches) == 2
    rows = [json.loads(line) for line in settings.company_stats_path.read_text().splitlines()]
    assert rows == [{'call_sid': call.call_sid, 'nonsilent_inbound_frames_during_handoff': 10}]
    messages = [record.getMessage() for record in caplog.records]
    assert sum('scripted line step=' in m and 'step=agent' in m for m in messages) == 4
    assert sum('scripted line step=' in m and 'step=rep' in m for m in messages) == 5
    assert sum('scripted handoff window opened' in m for m in messages) == 1
    assert sum('scripted handoff window closed' in m for m in messages) == 1
    assert messages.count('scripted handoff ask n=1 step=rep') == 1
    assert messages.count('scripted holder answered') == 1
    assert messages.count('scripted handoff window closed step=rep reason=resume') == 1
    assert messages.count('keypad entry accepted') == 1


async def test_mismatch_repeats_and_accepts(rig, caplog):
    caplog.set_level(logging.INFO)
    _, _, polly, times, _, company, scenario = rig
    async with connect_rig(rig) as (_, call, conn):
        await reach_keypad(conn, rig)
        after = time.monotonic()
        await send_keys(conn, '1#')
        await wait_prompt_finished(conn, quiet_s=0.2, after=after)
        assert call._engine.current_step == 'agent'
        assert sum(c['Text'] == scenario.steps['agent'].text_script[2].say for c in polly.calls) == 1
        assert any(record.getMessage() == 'keypad entry rejected' for record in caplog.records)
        await send_keys(conn, re.sub(r"\D", "", company.customers[0].phone_number) + '#')
        await wait_line(conn, polly, times, scenario.steps['rep'].text_script[0].say)
        assert call._engine.current_step == 'rep'


async def test_short_pause_does_not_finish_reply(rig):
    _, _, polly, times, _, _, scenario = rig
    async with connect_rig(rig) as (_, _, conn):
        await reach_agent(conn)
        first, second = scenario.steps['agent'].text_script[:2]
        await wait_line(conn, polly, times, first.say)
        await speak(conn, 200)
        await asyncio.sleep(0.15)
        assert second.say not in times
        last = await speak(conn, 200)
        await wait_line(conn, polly, times, second.say)
        assert times[second.say] >= last + 0.3 - 0.05


async def test_default_sonic_still_opens_bot(rig, monkeypatch):
    opened = rig[4]
    async def record(self, role):
        opened.append(role)
        self._bot_ws = object()  # Sentinel prevents repeated open attempts before shutdown.
        self._bot_step = self._engine.current_step
    async def close(self):
        self._bot_ws = None
    async def media(self, payload):
        pass
    monkeypatch.setattr(CarrierCall, '_open_bot', record)
    monkeypatch.setattr(CarrierCall, '_close_bot', close)
    monkeypatch.setattr(CarrierCall, '_send_bot_media', media)
    async with connect_rig(rig, 'sonic') as (app, call, conn):
        await reach_agent(conn)
        await wait_until(lambda: bool(opened), 6, 'default Sonic')
        assert opened == ['agent']
        assert app.state.carrier.agent_mode == call._agent_mode == 'sonic'
        assert not any(c['Text'] == rig[6].steps['agent'].text_script[0].say for c in rig[2].calls)


async def test_voice_caches_are_shared_and_lazy(rig):
    settings, _, polly, _, _, company, _ = rig
    async with connect_rig(rig) as (app, call, _):
        # Immediately after start only IVR can have been requested.
        assert all(c['VoiceId'] == company.voices.ivr_polly for c in polly.calls)
        assert not any(c['Text'] == 'What is the service address?' for c in polly.calls)
        assert call._voice_cache(company.voices.ivr_polly) is call._prompt_cache
        rep_cache = call._voice_cache('Matthew')
        assert call._voice_cache('Matthew') is rep_cache
        assert rep_cache.get('cache probe') == rep_cache.get('cache probe')
        assert sum(c['Text'] == 'cache probe' for c in polly.calls) == 1
        from waitless.models import CarrierDialIn
        sid = (await app.state.carrier.place_call(CarrierDialIn(**call.dial.model_dump()))).call_sid
        assert app.state.carrier._calls[sid]._voice_cache('Matthew') is rep_cache
