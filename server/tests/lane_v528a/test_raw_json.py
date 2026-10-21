"""T2: JSON primitives must not be coerced by Pydantic at the boundary."""
from copy import deepcopy
import json
import httpx
import pytest
from pydantic import SecretStr
from waitless import models as m
from waitless.decision.jev import HttpJevClient, JevError, build_state

GOOD={'type':'choice','choice':'a','confidence':.9,'probabilities':{'a':.9}}
BAD=[('confidence',True),('confidence','0.9'),('confidence',None),
     ('probabilities',{'a':'0.9'}),('probabilities',{'a':True}),('probabilities',{'a':None}),
     ('probabilities',[]),('probabilities',{'other':.9}),('type','score'),('choice',1),
     ('choice',True),('choice',None),('choice','other')]

async def ask(mode_env,answer):
    env=mode_env('text-sim')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
            httpx.Response(200,content=json.dumps({'answers':{'q':answer}})))) as http:
        client=HttpJevClient(url=env['WAITLESS_JEV_URL'],model=env['WAITLESS_JEV_MODEL'],
            api_key=SecretStr(env['WAITLESS_JEV_API_KEY']),timeout_s=5,http=http)
        return await client.ask(build_state(task_goal='Task',allowed_fields={},standing_rules=[],
            prior_choices=[],transcript=[]),{'q':m.JevQuestion(type='choice',instructions='Choose',
            criteria={'a':'A','b':'B'})})

@pytest.mark.parametrize('field,value',BAD)
async def test_raw_choice_types_rejected_before_model(mode_env,field,value):
    answer=deepcopy(GOOD); answer[field]=value
    with pytest.raises(JevError): await ask(mode_env,answer)

@pytest.mark.parametrize('confidence,probabilities',[(1,{'a':1}),(.9,{'a':.9}),(0,{'a':0,'b':1})])
async def test_raw_numbers_and_subset_distribution_accepted(mode_env,confidence,probabilities):
    reply=await ask(mode_env,{**GOOD,'confidence':confidence,'probabilities':probabilities})
    assert reply.answers['q'].choice=='a'
    assert reply.answers['q'].confidence==confidence
