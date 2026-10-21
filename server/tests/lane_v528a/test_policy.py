"""T3: pure tiering, atomic guard and configured consecutive limits."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from importlib import import_module, util
from types import SimpleNamespace
import pytest
from waitless import models as m

SPEAKERS=('menu_or_recording','hold','virtual_agent','live_human')

def policy():
    assert util.find_spec('waitless.call.speaker_policy') is not None, 'speaker policy module required'
    return import_module('waitless.call.speaker_policy')

def config(**over):
    c={'speaker':{'options':dict.fromkeys(SPEAKERS,'Meaning'),'min_confidence':.7,
          'automated_max_human_p':.2,'probability_sum_tolerance':.05},
       'transfer':{'options':dict.fromkeys(('none','pending','connected'),'Meaning'),'min_confidence':.7},
       'keypad':{'options':{'none':'Wait','1':'One'},'min_confidence':.6},
       'engine':SimpleNamespace(uncertain_max_turns=3,unavailable_max_attempts=3),
       'seq':1,'evidence':'Company evidence'}
    c.update(over); return c

def answers(who='virtual_agent',confidence=.6,ph=.01,transfer='none',tc=.9):
    probs={k:(ph if k=='live_human' else (1-ph)/3) for k in SPEAKERS}
    def a(choice,confidence,prob):
        return m.JevAnswer.model_construct(type='choice',choice=choice,confidence=confidence,probabilities=prob)
    return {'speaker':a(who,confidence,probs),'keypad':a('1',.9,{'1':.9}),
            'transfer':a(transfer,tc,{transfer:tc})}

@pytest.mark.parametrize('who,confidence,ph,want,gate,phase',[
    ('live_human',.7,.7,'live_human','live_human','live_rep'),
    ('live_human',.69,.01,'uncertain','unknown',None),
    ('virtual_agent',.6,.2,'uncertain','unknown',None),
    ('virtual_agent',.1,.19,'automated','virtual_agent','ai_agent'),
    ('menu_or_recording',.4,.01,'automated','virtual_agent','ivr'),
    ('hold',.4,.01,'automated','hold','hold')])
def test_boundaries(who,confidence,ph,want,gate,phase):
    p=policy();v,s=p.classify(answers(who,confidence,ph),state=p.SpeakerPolicyState(),config=config())
    assert (v.speaker_class,v.gate_speaker,v.phase)==(want,gate,phase)
    assert v.action==('handoff' if want=='live_human' else 'wait' if want=='uncertain' else 'hold' if who=='hold' else 'allow')

@pytest.mark.parametrize('total,valid',[(.95,True),(1.05,True),(.9499,False),(1.0501,False)])
def test_probability_sum_boundaries(total,valid):
    p=policy();a=answers();a['speaker'].probabilities={k:total/4 for k in SPEAKERS}
    v,_=p.classify(a,state=p.SpeakerPolicyState(),config=config())
    assert (v.speaker_class!='unavailable')==valid

@pytest.mark.parametrize('probs',[{'menu_or_recording':.5,'hold':.5},
    {'menu_or_recording':.3,'hold':.3,'virtual_agent':.4},dict.fromkeys(SPEAKERS,.1),
    {**dict.fromkeys(SPEAKERS,.25),'extra':0}])
def test_complete_speaker_distribution_required(probs):
    p=policy();a=answers('live_human',1);a['speaker'].probabilities=probs
    v,_=p.classify(a,state=p.SpeakerPolicyState(),config=config())
    assert v.speaker_class=='unavailable' and v.action=='wait'

@pytest.mark.parametrize('qid',['speaker','keypad','transfer'])
@pytest.mark.parametrize('bad',[float('nan'),float('inf'),-.1,1.1,True,'0.9',None])
@pytest.mark.parametrize('field',['confidence','probabilities'])
def test_invalid_numeric_package_before_tiering(qid,bad,field):
    p=policy();a=answers('live_human',1,transfer='connected')
    if field=='confidence':a[qid].confidence=bad
    else:
        probs=dict(a[qid].probabilities)
        if qid=='speaker' and type(bad) in (int,float) and bad not in (float('inf'),float('-inf')):
            probs={k:(bad if k=='menu_or_recording' else (1-bad)/3) for k in SPEAKERS}
        elif qid=='speaker' and bad is True:
            probs={'menu_or_recording':True,'hold':0,'virtual_agent':0,'live_human':0}
        else:probs[next(iter(probs))]=bad
        a[qid].probabilities=probs
    v,s=p.classify(a,state=p.SpeakerPolicyState(),config=config())
    assert v.speaker_class=='unavailable' and v.action=='wait' and not s.guard

@pytest.mark.parametrize('mutation',['missing','extra','type','choice','prob_extra','prob_empty'])
def test_answer_contract(mutation):
    p=policy();a=answers('live_human',1,transfer='connected')
    if mutation=='missing':a.pop('keypad')
    elif mutation=='extra':a['extra']=a['keypad']
    elif mutation=='type':a['speaker'].type='score'
    elif mutation=='choice':a['speaker'].choice='other'
    elif mutation=='prob_extra':a['transfer'].probabilities={'other':1}
    elif mutation=='prob_empty':a['keypad'].probabilities={}
    v,s=p.classify(a,state=p.SpeakerPolicyState(),config=config())
    assert v.speaker_class=='unavailable' and not s.guard

@pytest.mark.parametrize('guard',[False,True])
@pytest.mark.parametrize('transfer,tc,want',[('pending',.7,'guard_wait'),('connected',.7,'transfer_connected'),('pending',.69,'automated')])
def test_same_package_guard_priority(guard,transfer,tc,want):
    p=policy();initial=p.SpeakerPolicyState(guard=guard)
    v,s=p.classify(answers(transfer=transfer,tc=tc),state=initial,config=config())
    if guard and tc<.7:want='guard_wait'
    assert v.speaker_class==want
    assert s.guard==(guard or tc>=.7)
    if want=='transfer_connected':assert v.handoff_kind=='transfer_connected'
    if want=='guard_wait':assert v.phase=='hold' and v.arm_quiet and not v.start_stuck


def test_r1_priority_and_first_guard_evidence_preserved():
    p=policy();state=p.SpeakerPolicyState(uncertain_run=2)
    v,s=p.classify(answers('live_human',.7,transfer='connected'),state=state,config=config(seq=5))
    assert v.handoff_kind=='live_human' and s.guard and s.guard_since_seq==5 and s.uncertain_run==0
    _,s2=p.classify(answers(transfer='pending'),state=s,config=config(seq=6,evidence='Later'))
    assert s2.guard_since_seq==5 and s2.guard_evidence==s.guard_evidence
    assert not state.guard
    with pytest.raises(FrozenInstanceError):s.guard=False
    with pytest.raises(FrozenInstanceError):v.action='allow'


def test_counters_pause_reset_and_caps():
    p=policy();s=p.SpeakerPolicyState()
    for count in (1,2):
        v,s=p.classify(answers(ph=.2),state=s,config=config())
        assert s.uncertain_run==count and v.action=='wait'
    v,s=p.classify(None,state=s,config=config())
    assert s.uncertain_run==2 and s.unavailable_run==1 and not v.arm_quiet and v.start_stuck
    v,s=p.classify(answers(ph=.2),state=s,config=config())
    assert s.uncertain_run==3 and s.unavailable_run==0
    assert (v.action,v.handoff_kind,v.reason_code)==('handoff','needs_user','speaker_uncertain_repeated')
    _,s=p.classify(answers(),state=s,config=config())
    assert s.uncertain_run==s.unavailable_run==0
    for count in (1,2,3):
        v,s=p.classify(None,state=s,config=config())
        assert s.unavailable_run==count
    assert (v.action,v.handoff_kind,v.reason_code)==('handoff','needs_user','speaker_unavailable_repeated')
    assert p.SpeakerPolicyState().guard is False and p.SpeakerPolicyState().unavailable_run==0


def test_guard_unavailable_phase_and_counter():
    p=policy();s=p.SpeakerPolicyState(guard=True,uncertain_run=0)
    v,s=p.classify(None,state=s,config=config())
    assert v.phase=='hold' and v.arm_quiet and not v.start_stuck and s.unavailable_run==1


def test_subset_distributions_need_no_sum_check():
    p=policy();a=answers();a['keypad'].probabilities={'none':.1};a['transfer'].probabilities={'pending':.1}
    v,_=p.classify(a,state=p.SpeakerPolicyState(),config=config())
    assert v.speaker_class=='automated'


def test_configuration_thresholds_and_caps_are_used():
    p=policy();c=config();c['speaker'].update(automated_max_human_p=.4,min_confidence=.8,probability_sum_tolerance=.01)
    c['transfer']['min_confidence']=.8;c['engine']=SimpleNamespace(uncertain_max_turns=1,unavailable_max_attempts=1)
    v,s=p.classify(answers(ph=.3,transfer='pending',tc=.7),state=p.SpeakerPolicyState(),config=c)
    assert v.speaker_class=='automated' and not s.guard
    v,s=p.classify(answers('live_human',.75),state=s,config=c)
    assert v.handoff_kind=='needs_user' and v.reason_code=='speaker_uncertain_repeated'
    v,_=p.classify(None,state=p.SpeakerPolicyState(),config=c)
    assert v.handoff_kind=='needs_user'
    a=answers();a['speaker'].probabilities=dict.fromkeys(SPEAKERS,.24)
    v,_=p.classify(a,state=p.SpeakerPolicyState(),config=c)
    assert v.speaker_class=='unavailable'


def test_guard_hold_and_valid_package_reset_unavailability():
    p=policy();initial=p.SpeakerPolicyState(guard=True,unavailable_run=2)
    v,s=p.classify(answers('hold'),state=initial,config=config())
    assert v.speaker_class=='guard_wait' and v.action=='hold' and v.gate_speaker=='hold'
    assert v.phase=='hold' and s.unavailable_run==0 and initial.unavailable_run==2
