"""Exercise carrier -> company-bot WebSocket -> engine-line media relay."""
import asyncio
import base64
import httpx
import pytest
import yaml
from fakes import FakePolly
from waitless.company import bot
from waitless.company.app import create_company_app
from waitless.audio.codec import ulaw_encode
from waitless.call.line_protocol import LINE_FRAME_MS, MULAW_FRAME_BYTES
from tests.company._live_harness import (company_settings, FakeEngineLine, loopback_host,
    no_proxy, patch_boto3_polly, running_company, wait_until, dial_body)
from tests.company._scenario_data import copy_company_root
from tests.company.test_audio_carrier_line import send_keys, wait_prompt_finished
from tests.lane_B.test_company_bot_run import Sonic

async def test_live_relay_connects_agent_then_rep_and_ends_call(company_dir, mode_env,
        engine_config, tmp_path, monkeypatch):
    env = mode_env('live-sim', company=True)
    host = loopback_host(env)
    no_proxy(monkeypatch, host)
    patch_boto3_polly(monkeypatch, FakePolly(ms_per_char=1, min_ms=20))
    root = copy_company_root(company_dir, tmp_path)
    path = root / 'lakeshore-energy' / 'scenarios' / 'baseline.yaml'
    scenario = yaml.safe_load(path.read_text())
    scenario['steps']['hold']['seconds'] = 1
    path.write_text(yaml.safe_dump(scenario))
    services = []
    def factory(**kwargs):
        service = Sonic(**kwargs)
        services.append(service)
        return service
    monkeypatch.setattr(bot, '_llm_factory', factory, raising=False)
    app = create_company_app(company_settings(env, engine_config, root, tmp_path))
    async with FakeEngineLine(host) as line, running_company(app, host) as base:
        async with httpx.AsyncClient(base_url=base, trust_env=False) as client:
            response = await client.post('/carrier/calls', json=dial_body(call_id='relay-call',
                task_id='relay-task', to_number=next(iter(app.state.carrier._companies)),
                stream_url=line.url, stream_token='relay-token'))
            sid = response.json()['call_sid']
            await wait_until(lambda: bool(line.conns) and line.conns[0].first('start'), 3, 'line start')
            conn = line.conns[0]
            await wait_prompt_finished(conn, quiet_s=.2)
            await send_keys(conn, '1')
            await wait_prompt_finished(conn, quiet_s=.2)
            await send_keys(conn, '1')
            await wait_until(lambda: services and services[0].context.is_set(), 4, 'agent Sonic')
            assert services[0].options['voice'] == app.state.carrier._calls[sid].company.voices.agent_sonic
            expected = ulaw_encode(b'\x40\x03' * 170)[:160]
            await conn.send({'event': 'media', 'streamSid': conn.stream_sid,
                'media': {'payload': base64.b64encode(b'\xce' * 160).decode('ascii')}})
            await wait_until(lambda: expected in conn.media_payloads(), 3, 'bot audio on line')
            await services[0].tool('record_detail', field='address', value='service address')
            await services[0].tool('record_detail', field='start_date', value='next week')
            call = app.state.carrier._calls[sid]
            await send_keys(conn, call.company.customers[0].account_number + '#')
            await wait_until(lambda: call._engine._bot_account_verified, 3, 'keypad verification in bot')
            # The substitute produces 170 samples (two wire frames) per input.
            # Budget all forwarded inputs, including ones not processed yet,
            # and the carrier queue instead of truncating speech to meet 3 s.
            generated_frames = call._bot_chunk * ((170 + MULAW_FRAME_BYTES - 1) // MULAW_FRAME_BYTES)
            switch_budget = (generated_frames + len(call._bot_audio)) * LINE_FRAME_MS / 1000 + 3
            assert (await services[0].tool('transfer_to_representative', event='deposit_review'))['ok']
            await wait_until(lambda: len(services) == 2 and services[1].context.is_set(), switch_budget, 'rep Sonic')
            assert services[1].options['voice'] == call.company.voices.rep_sonic
            await services[1].tool('end_call')
            await wait_until(lambda: conn.closed_at is not None, 3, 'line hangup')
            assert conn.events()[-1] == 'stop'
            await client.post(f'/carrier/calls/{sid}/hangup')
            assert not app.state.carrier._tasks
            assert all(service.cancelled for service in services)


async def test_hangup_while_bot_connects_closes_late_socket(company_dir, mode_env, monkeypatch):
    from unittest.mock import AsyncMock
    from urllib.parse import urlsplit, urlunsplit
    from waitless.company.carrier import CarrierCall
    from waitless.company.config import load_company, load_scenario
    from waitless.company.engine import ScenarioEngine
    from waitless.call.line_protocol import new_call_sid
    from waitless.models import CarrierDialIn
    import websockets.asyncio.client
    company = load_company(company_dir, 'lakeshore-energy')
    env = mode_env('live-sim', company=True)
    call = CarrierCall(dial=CarrierDialIn(call_id='race-call', task_id='race-task',
        to_number=company.line_number, stream_url=env['WAITLESS_LINE_WS_URL'],
        stream_token='race-token'), call_sid=new_call_sid(), scenario_id='baseline', company=company)
    call._engine = ScenarioEngine(load_scenario(company_dir, company.company_id, 'baseline'), company)
    call._engine._enter('agent')
    parts = urlsplit(env['WAITLESS_CARRIER_URL'])
    call._bot_endpoint = urlunsplit(('ws', parts.netloc, '/ws/company-bot', '', ''))
    started, finish = asyncio.Event(), asyncio.Event()
    socket = AsyncMock()
    async def connect(*args, **kwargs):
        started.set()
        await finish.wait()
        return socket
    monkeypatch.setattr(websockets.asyncio.client, 'connect', connect)
    opening = asyncio.create_task(call._open_bot('agent'))
    await started.wait()
    await call.hangup()
    finish.set()
    await asyncio.wait_for(opening, 3)
    socket.close.assert_awaited_once()
    socket.send.assert_not_called()
    assert call._bot_ws is None and call._bot_receive is None
