"""Exact schema assertions shared by carrier lifecycle tests."""
STATS_KEYS = {'schema_version','call_id','call_sid','company_id','scenario_id','agent_mode',
    'ended','lines','holder','dtmf_tones','nonsilent_inbound_frames_during_handoff',
    'rep_first_audio_at','rep_last_audio_at','rep_first_line_end_at',
    'nonsilent_inbound_frames_during_rep','nonsilent_inbound_timestamps_during_rep',
    'nonsilent_inbound_spans_during_rep'}


def assert_schema(row):
    assert set(row) == STATS_KEYS
    assert row['schema_version'] == 2 and type(row['schema_version']) is int
    assert set(row['ended']) == {'by','reason','at','closing_index','closing_outcome','closing_line_completed'}
    assert type(row['ended']['at']) is float and row['ended']['at'] >= 0
    assert type(row['ended']['closing_line_completed']) is bool
    assert set(row['holder']) == {'keypad_entries_complete','keypad_wrong_length','choice','choices_recorded','holder_voice_answers'}
    assert row['holder']['choice'] in {'agree','decline',None}
    assert all(type(row['holder'][key]) is int for key in row['holder'] if key != 'choice')
    assert isinstance(row['lines'],list) and isinstance(row['dtmf_tones'],list)
    assert len(row['nonsilent_inbound_spans_during_rep']) == len(row['nonsilent_inbound_timestamps_during_rep']) == row['nonsilent_inbound_frames_during_rep']
    forbidden = {'text','keys','date_of_birth','birthday','raw_mapping','exception'}
    assert not (forbidden & row.keys())
    assert all(not(forbidden & line.keys()) for line in row['lines'])


def assert_empty(row, *, call_id, call_sid, by, reason):
    assert_schema(row)
    assert row == {'schema_version':2,'call_id':call_id,'call_sid':call_sid,
        'company_id':'lakeshore-energy','scenario_id':'baseline','agent_mode':'sonic',
        'ended':{'by':by,'reason':reason,'at':row['ended']['at'],
                 'closing_index':None,'closing_outcome':None,'closing_line_completed':False},
        'lines':[],'holder':{'keypad_entries_complete':0,'keypad_wrong_length':0,
                            'choice':None,'choices_recorded':0,'holder_voice_answers':0},
        'dtmf_tones':[],'nonsilent_inbound_frames_during_handoff':0,
        'rep_first_audio_at':None,'rep_last_audio_at':None,'rep_first_line_end_at':None,
        'nonsilent_inbound_frames_during_rep':0,'nonsilent_inbound_timestamps_during_rep':[],
        'nonsilent_inbound_spans_during_rep':[]}
