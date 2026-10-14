"""Real-time boundary checks for cached IVR and playback-aware menu timers."""
import asyncio
import base64

import httpx
import pytest

from fakes import FakePolly
from waitless.company.app import create_company_app
from tests.company._live_harness import (
    FakeEngineLine, company_settings, loopback_host, no_proxy, patch_boto3_polly,
    running_company, wait_until,
)
from tests.company._scenario_data import copy_company_root, edit_company_yaml
from tests.company.test_audio_carrier_line import place_call, assert_stopped_and_closed


async def test_cache_across_calls_and_realtime_frames(
    mode_env, engine_config, company_dir, tmp_path, monkeypatch,
):
    env = mode_env('live-sim', company=True)
    host = loopback_host(env)
    no_proxy(monkeypatch, host)
    polly = FakePolly(ms_per_char=1, min_ms=40)
    patch_boto3_polly(monkeypatch, polly)
    app = create_company_app(company_settings(env, engine_config, company_dir, tmp_path))
    async with FakeEngineLine(host) as engine, running_company(app, host) as base:
        for tag in ('cache-first', 'cache-second'):
            sid, conn = await place_call(base, engine, tag=tag)
            await wait_until(lambda: any(c.first('start') is not None and
                                         c.first('start')[1]['start']['callSid'] == sid
                                         for c in engine.conns), 3, 'this call to start')
            conn = next(c for c in engine.conns if c.first('start') is not None and
                        c.first('start')[1]['start']['callSid'] == sid)
            await wait_until(lambda: sum(e == 'media' for e in conn.events()) >= 20, 3, '20 media frames')
            frames = [(t, m) for t, m in conn.messages if m['event'] == 'media']
            assert frames[19][0] - frames[0][0] >= 0.3
            assert all(len(base64.b64decode(m['media']['payload'])) == 160 for _, m in frames)
            async with httpx.AsyncClient(base_url=base, trust_env=False) as http:
                await http.post(f'/carrier/calls/{sid}/hangup')
            await wait_until(lambda: conn.closed_at is not None, 3, 'close')
            assert_stopped_and_closed(conn, sid)
    texts = [call['Text'] for call in polly.calls]
    assert len(texts) == 2 and len(set(texts)) == 2


async def test_menu_timeout_counts_waiting_after_each_complete_prompt(
    mode_env, engine_config, company_dir, tmp_path, monkeypatch,
):
    env = mode_env('live-sim', company=True)
    host = loopback_host(env)
    no_proxy(monkeypatch, host)
    polly = FakePolly(ms_per_char=1, min_ms=700)
    patch_boto3_polly(monkeypatch, polly)
    root = copy_company_root(company_dir, tmp_path)
    edit_company_yaml(root, lambda data: data['menu'].update(timeout_sec=1, max_timeouts=2))
    app = create_company_app(company_settings(env, engine_config, root, tmp_path))
    async with FakeEngineLine(host) as engine, running_company(app, host) as base:
        sid, conn = await place_call(base, engine, tag='playback-timer')
        await wait_until(lambda: conn.closed_at is not None, 8, 'two complete prompt and wait cycles')
        stop_t, _ = assert_stopped_and_closed(conn, sid)
        # Greeting + menu + repeated menu (3 * 700ms), plus two 1s waits.
        assert stop_t - conn.first('start')[0] >= 4.0
        assert len(polly.calls) == 2, 'replaying the menu uses cached PCM'
