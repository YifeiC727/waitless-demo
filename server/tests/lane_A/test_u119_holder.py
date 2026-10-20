"""v5.27 live holder; B1 metric fixture until company branch is merged."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from a_support import make_settings
from fakes.polly import tone_pcm16
from waitless import cli
from waitless.company import ivr_audio
from waitless.models import HANDOFF_SAMPLE_RATE

TONE=tone_pcm16(20,HANDOFF_SAMPLE_RATE)


class FlowSocket:
    def __init__(self, bank=False, close_after=True):
        self.sent=[]; self.incoming=asyncio.Queue(); self.bank=bank; self.close_after=close_after
        self.producers=[]; self.reader_closed=False
    async def question(self, last=False):
        await asyncio.sleep(.015)
        for _ in range(4):
            await self.incoming.put(TONE)
            await asyncio.sleep(.02)
        if last and self.close_after:
            await asyncio.sleep(.08)
            await self.incoming.put(json.dumps({'type':'ended','reason':'company_hangup'}))
    async def send(self, frame):
        self.sent.append(frame)
        # One-frame synthetic replies make each send a complete answer.
        self.producers.append(asyncio.create_task(self.question(last=len(self.sent)==3 if not self.bank else len(self.sent)==2)))
    async def recv(self):
        try: return await self.incoming.get()
        finally: self.reader_closed=True
    async def cleanup(self):
        for t in self.producers: t.cancel()
        await asyncio.gather(*self.producers,return_exceptions=True)


def live_plan(bank=False):
    return SimpleNamespace(frames=[TONE],question_wait_ms=400,return_after_ms=300,
        choice_min_voice_ms=40,then_keys=None if bank else '2',live=True,
        birthday_keys=None if bank else '01011990#',bank=bank, first_answer_silence_ms=40,
        first_answer_no_voice_ms=60,holder_audio_tail_ms=20,key_ms=5,key_tail_ms=0,holder_windows=[])


@pytest.mark.parametrize('bank',[False,True])
async def test_proactive_reply_then_ordered_questions_until_company_end(bank):
    ws=FlowSocket(bank)
    try:
        outcome=await cli._simulate_account_holder(ws,live_plan(bank),threshold_dbfs=-40,
            question_end_silence_ms=40,frame_ms=20)
        assert outcome=='ended'
        assert ws.sent[0]==TONE  # No company audio exists until the holder replies.
        if bank:
            assert ws.sent==[TONE,TONE]
        else:
            assert [json.loads(v) for v in ws.sent[1:]]==[
                {'type':'dtmf','keys':'01011990#'},{'type':'dtmf','keys':'2'}]
        assert all(not isinstance(v,str) or json.loads(v)['type']!='return' for v in ws.sent)
    finally: await ws.cleanup()


async def test_live_missing_birthday_question_is_bounded_and_no_return(capsys):
    ws=FlowSocket()
    async def no_question(frame): ws.sent.append(frame)
    ws.send=no_question
    try:
        out=await cli._simulate_account_holder(ws,live_plan(),threshold_dbfs=-40,question_end_silence_ms=40,frame_ms=20)
        assert out=='no_question' and ws.sent==[TONE]
    finally: await ws.cleanup()


async def test_any_live_rep_gets_holder_plan_including_bank(tmp_path,monkeypatch):
    settings=make_settings(tmp_path,mode='live-sim')
    client=object()
    monkeypatch.setattr(cli,'_create_holder_polly_client',lambda settings:client)
    synth=Mock(return_value=TONE)
    monkeypatch.setattr(ivr_audio,'synthesize_prompt',synth)
    scenario={'max_call_seconds':1500,'steps':{'rep':{'kind':'live_rep',
        'scripted':{'reply_end_silence_ms':100,'polly_voice':'Salli'},
        'text_script':[{'say':'Would you like a callback?','expect':{'intent':'ask_user'}}]}}}
    plan=await cli._prepare_holder_plan(scenario,settings)
    assert plan is not None and plan.live and plan.bank
    assert plan.frames and plan.birthday_keys is None
    # Bank has no holder_prompt_ms; the configured call lifetime bounds long spoken questions.
    assert plan.question_wait_ms >= scenario['max_call_seconds'] * 1000
    assert cli._live_check_timeout(scenario,settings)>=1530


def test_identity_check_requires_complete_e183c_facts(engine_config):
    # Incomplete adapters cannot supply a gate history or a real carrier schema.
    stats={'rep_first_audio_ms':100,'rep_last_audio_ms':180,'rep_first_sentence_end_ms':180,
           'rep_nonsilent_inbound_frame_ms':[150]}
    assert not cli._identity_check([],stats,holder_ready_ms=300,rep_final_ms=None,config=engine_config.speaker)
    assert not cli._identity_check([],{},holder_ready_ms=None,config=engine_config.speaker)
