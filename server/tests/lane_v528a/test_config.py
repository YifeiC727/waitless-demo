"""v5.28 T1: exact configuration contracts, without live services."""
from copy import deepcopy
from pathlib import Path
import pytest
import yaml
from pydantic import ValidationError
from waitless import models as m
from waitless.call import keypad

ROOT = Path(__file__).resolve().parents[3]
NEW = {'uncertain_quiet_ms':5000, 'quiet_max_gap_ms':200, 'uncertain_max_wait_ms':20000,
       'uncertain_max_turns':3, 'unavailable_max_attempts':3,
       'teardown_step_timeout_ms':5000, 'teardown_cancel_grace_ms':1000}
TEXT = {'connected_text':'转接后线路上在跟你说话，请马上加入',
        'needs_user_text':'线路上的情况 Waitless 判断不了，请你接手'}
SPEAKER = {'instructions':'Read context.', 'min_confidence':.7,
    'options':dict.fromkeys(('menu_or_recording','hold','virtual_agent','live_human'),'Meaning'),
    'automated_max_human_p':.2, 'probability_sum_tolerance':.05}
TRANSFER = {'instructions':'Read context.', 'min_confidence':.7,
    'options':dict.fromkeys(('none','pending','connected'),'Meaning')}
MISSING = object()


def engine():
    data = yaml.safe_load((ROOT/'server/config/engine.yaml').read_text())
    data['speaker'].update(NEW); data['handoff'].update(TEXT)
    return data


def load(tmp_path, kind, data):
    ref=tmp_path/'phone-agent/references'; ref.mkdir(parents=True,exist_ok=True)
    (ref/(kind+'.yaml')).write_text(yaml.safe_dump(data))
    return getattr(keypad,'load_'+kind+'_config')(tmp_path)


def test_engine_values_and_three_reason_kinds():
    data=yaml.safe_load((ROOT/'server/config/engine.yaml').read_text())
    assert {k:data['speaker'].get(k) for k in NEW} == NEW
    assert {k:data['handoff'].get(k) for k in TEXT} == TEXT
    model=m.EngineConfig.model_validate(data)
    assert set(NEW) <= m.EngineSpeaker.model_fields.keys()
    for kind in ('live_human','transfer_connected','needs_user'):
        assert m.HandoffStatus(state='ready', company_name='Company', media=m.Media(ai_media='disconnected', recording=False, transcription=False), reason_kind=kind).reason_kind == kind


@pytest.mark.parametrize('key',NEW)
@pytest.mark.parametrize('bad',[MISSING,0,-1,True,'5',1.5,None,[]])
def test_engine_strict_matrix(key,bad):
    data=engine()
    if bad is MISSING: data['speaker'].pop(key)
    else: data['speaker'][key]=bad
    with pytest.raises(ValidationError): m.EngineConfig.model_validate(data)


@pytest.mark.parametrize('key',TEXT)
@pytest.mark.parametrize('bad',[MISSING,'','  ',True,4,None,[],1.5])
def test_notification_matrix(key,bad):
    data=engine()
    if bad is MISSING: data['handoff'].pop(key)
    else: data['handoff'][key]=bad
    with pytest.raises(ValidationError): m.EngineConfig.model_validate(data)


@pytest.mark.parametrize('key',['automated_max_human_p','probability_sum_tolerance'])
@pytest.mark.parametrize('bad',[MISSING,0.,-0.1,1.,1.1,True,'0.2',1,None,[],float('nan'),float('inf')])
def test_speaker_float_matrix(tmp_path,key,bad):
    data=deepcopy(SPEAKER)
    if bad is MISSING: data.pop(key)
    else: data[key]=bad
    with pytest.raises(ValueError): load(tmp_path,'speaker',data)


@pytest.mark.parametrize('kind',['speaker','transfer'])
@pytest.mark.parametrize('mutation',['top_list','missing','extra','blank','option_missing','option_extra','option_blank','option_list'])
def test_reference_structure(tmp_path,kind,mutation):
    data=deepcopy(SPEAKER if kind=='speaker' else TRANSFER)
    if mutation=='top_list': data=[]
    elif mutation=='missing': data.pop('instructions')
    elif mutation=='extra': data['extra']=1
    elif mutation=='blank': data['instructions']=' '
    elif mutation=='option_missing': data['options'].pop(next(iter(data['options'])))
    elif mutation=='option_extra': data['options']['extra']='Extra'
    elif mutation=='option_blank': data['options'][next(iter(data['options']))]=' '
    elif mutation=='option_list': data['options']=list(data['options'])
    with pytest.raises(ValueError): load(tmp_path,kind,data)


@pytest.mark.parametrize('bad',[0,-.1,1.1,True,'0.7',None,float('nan'),float('inf')])
def test_transfer_confidence_matrix(tmp_path,bad):
    with pytest.raises(ValueError): load(tmp_path,'transfer',{**TRANSFER,'min_confidence':bad})


@pytest.mark.parametrize('kind,threshold',[('speaker',0),('speaker',1),('transfer',1)])
def test_threshold_boundaries(tmp_path,kind,threshold):
    data=deepcopy(SPEAKER if kind=='speaker' else TRANSFER); data['min_confidence']=threshold
    assert load(tmp_path,kind,data)==data


def test_real_references():
    speaker=keypad.load_speaker_config(ROOT/'skills')
    transfer=keypad.load_transfer_config(ROOT/'skills')
    assert speaker['automated_max_human_p']==.2
    assert transfer['min_confidence']==.7
    assert set(transfer['options'])=={'none','pending','connected'}
