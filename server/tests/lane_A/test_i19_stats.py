"""I-19 F8: real carrier JSONL flows through the CLI measurement reader."""
import json

import pytest
from waitless import cli
from waitless.company.carrier import CarrierCall
from waitless.company.config import load_company
from waitless.models import CarrierDialIn
from waitless.call.line_protocol import new_call_sid


async def write_stats(path, settings, company_dir, call_id, frames):
    company = load_company(company_dir, 'lakeshore-energy')
    dial = CarrierDialIn(call_id=call_id, task_id='stats-task', to_number=company.line_number,
        stream_url=settings.line_ws_url, stream_token='offline-line-ticket')
    carrier = CarrierCall(dial=dial, call_sid=new_call_sid(), scenario_id='baseline', company=company)
    carrier._stats_path = path
    carrier._rep_first_audio_at = .1
    carrier._rep_last_audio_at = .18
    carrier._rep_first_line_end_at = .18
    carrier._nonsilent_inbound_timestamps_during_rep = frames
    await carrier.hangup()
    return json.loads(path.read_text().splitlines()[-1])


async def test_real_carrier_stats_identity_selection_and_units(tmp_path, company_dir):
    from a_support import make_settings
    settings = make_settings(tmp_path/'env', mode='live-sim')
    path = tmp_path/'stats.jsonl'
    first = await write_stats(path, settings, company_dir, 'wanted', [.15])
    assert first['call_id'] == 'wanted'
    assert first['call_sid'] and first['rep_first_audio_at'] == .1
    assert first['rep_last_audio_at'] == .18 and first['rep_first_line_end_at'] == .18
    assert first['nonsilent_inbound_timestamps_during_rep'] == [.15]
    await write_stats(path, settings, company_dir, 'unrelated', [.25])
    stats = cli._read_company_stats(path, 'wanted')
    assert stats['rep_first_audio_ms'] == 100
    assert stats['rep_last_audio_ms'] == stats['rep_first_sentence_end_ms'] == 180
    assert stats['rep_nonsilent_inbound_frame_ms'] == [150]
    assert cli._read_company_stats(path, 'absent') == {}
    with path.open('a') as out:
        out.write('bad-json\n[]\n')
    assert cli._read_company_stats(path, 'wanted') == {}
    await write_stats(path, settings, company_dir, 'wanted', [.15, .25])
    stats = cli._read_company_stats(path, 'wanted')
    assert not cli._identity_check([], stats, holder_ready_ms=300, rep_final_ms=200, config=settings.engine.speaker)


@pytest.mark.parametrize('field,value', [('rep_first_audio_at', None), ('rep_last_audio_at', 'bad'),
    ('rep_first_line_end_at', True), ('nonsilent_inbound_timestamps_during_rep', None),
    ('nonsilent_inbound_timestamps_during_rep', [False]), ('rep_last_audio_at', float('nan'))])
async def test_invalid_real_measurements_fail_identity(tmp_path, company_dir, field, value):
    from a_support import make_settings
    path = tmp_path/'stats.jsonl'
    settings = make_settings(tmp_path/'env', mode='live-sim')
    row = await write_stats(path, settings, company_dir, 'wanted', [])
    row[field] = value
    path.write_text(json.dumps(row)+'\n')
    stats = cli._read_company_stats(path, 'wanted')
    assert not cli._identity_check([], stats, holder_ready_ms=300, rep_final_ms=200, config=settings.engine.speaker)
