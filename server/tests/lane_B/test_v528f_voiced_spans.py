"""Design 14.2/14.4: real carrier observations, including split packet PCM."""
import json
import struct

import pytest

from waitless.company.carrier import CarrierCall
from waitless.company.config import load_company
from waitless.models import CarrierDialIn
from tests.lane_B.i19b_stats_support import STATS_KEYS


@pytest.fixture
def observation(company_dir, engine_config, mode_env, tmp_path):
    company = load_company(company_dir, 'lakeshore-energy')
    env = mode_env('live-sim', company=True)
    call = CarrierCall(dial=CarrierDialIn(to_number=company.line_number,
        stream_url=env['WAITLESS_LINE_WS_URL'], stream_token='local-fixture',
        call_id='call-voiced', task_id='task-voiced'), call_sid='CA' + 'a' * 32,
        scenario_id='baseline', company=company)
    call._engine_config = engine_config
    call._rep_first_audio_at = 10.
    call._stats_path = tmp_path / 'stats.jsonl'
    return call


def pcm(value, samples=80):
    return struct.pack('<h', value) * samples


async def finish(call):
    await call.hangup(source='api')
    row, = [json.loads(s) for s in call._stats_path.read_text().splitlines()]
    return row


@pytest.mark.parametrize('values,expected', [
    ([8000, 0], [10., 10.]),
    ([0, 8000], [11., 11.]),
    ([8000, 8000], [10., 11.]),
    ([8000], [10., 10.]),
    # At -20 dBFS the second piece is unvoiced but supplies >10% energy.
    ([4000, 3000], [10., 11.]),
])
async def test_piece_rms_and_fallbacks(observation, values, expected):
    call = observation
    call._engine_config.vad.threshold_dbfs = -20
    for index, value in enumerate(values):
        call._observe_inbound(pcm(value, 160 if len(values) == 1 else 80), 10. + index)
    row = await finish(call)
    assert row['nonsilent_inbound_spans_during_rep'] == [[10., 10. + len(values) - 1]]
    assert row['nonsilent_inbound_voiced_spans_during_rep'] == [expected]


@pytest.mark.parametrize('bot_threshold', [False, True])
async def test_uses_configured_or_existing_bot_threshold(observation, bot_threshold):
    call = observation
    call._engine_config.vad.threshold_dbfs = -30
    if bot_threshold:
        call._engine_config = None
        call._bot_threshold_dbfs = -30
    call._observe_inbound(pcm(2000), 10.)
    call._observe_inbound(pcm(0), 11.)
    row = await finish(call)
    assert row['nonsilent_inbound_voiced_spans_during_rep'] == [[10., 10.]]


@pytest.mark.parametrize('below', [False, True])
async def test_energy_share_boundary_uses_sum_not_mean(observation, below):
    call = observation
    call._engine_config.vad.threshold_dbfs = -40
    # 16*2700**2 / (16*2700**2 + 144*300**2) == 90% exactly.
    call._observe_inbound(pcm(2699 if below else 2700, 16), 10.)
    call._observe_inbound(pcm(300, 144), 11.)
    row = await finish(call)
    assert row['nonsilent_inbound_voiced_spans_during_rep'] == [[10., 11. if below else 10.]]


async def test_only_current_window_piece_is_measured_and_spans_stay_aligned(observation):
    call = observation
    call._observe_inbound(pcm(4000, 160), 9.)  # Before rep: excluded.
    call._observe_inbound(pcm(0), 10.)
    call._observe_inbound(pcm(4000) + pcm(0), 11.)
    call._observe_inbound(pcm(4000), 12.)
    call._observe_inbound(pcm(0, 160), 13.)  # Next window is silent.
    call._observe_inbound(pcm(4000, 40), 14.)  # Freeze pads a partial window.
    row = await finish(call)
    assert row['nonsilent_inbound_spans_during_rep'] == [[10., 11.], [11., 12.], [14., 14.]]
    assert row['nonsilent_inbound_voiced_spans_during_rep'] == [[11., 11.], [12., 12.], [14., 14.]]
    assert row['nonsilent_inbound_timestamps_during_rep'] == [10., 11., 14.]
    assert row['nonsilent_inbound_frames_during_rep'] == 3
    assert set(row) == STATS_KEYS | {'nonsilent_inbound_voiced_spans_during_rep'}
    assert all(a <= x <= y <= b for (a, b), (x, y) in zip(
        row['nonsilent_inbound_spans_during_rep'], row['nonsilent_inbound_voiced_spans_during_rep']))


async def test_empty_stats_adds_only_numeric_span_field(observation):
    row = await finish(observation)
    assert row['nonsilent_inbound_voiced_spans_during_rep'] == []
    assert set(row) == STATS_KEYS | {'nonsilent_inbound_voiced_spans_during_rep'}


async def test_no_individually_voiced_piece_keeps_full_span(observation, monkeypatch):
    # Defensive fallback: normal RMS cannot exceed a threshold if every piece
    # is below it. Force only that inconsistent classifier result, retaining
    # the real carrier, packet assembly, tone observer and stats writer.
    monkeypatch.setattr('waitless.company.carrier.pcm16_rms_dbfs',
        lambda data: 0. if len(data) == 320 else -100.)
    observation._observe_inbound(pcm(4000), 10.)
    observation._observe_inbound(pcm(4000), 11.)
    row = await finish(observation)
    assert row['nonsilent_inbound_voiced_spans_during_rep'] == [[10., 11.]]
