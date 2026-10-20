"""Exercise proactive live holder and absence of Return through the real MCP check."""
import json
import pytest
import websockets
import yaml
from a_support import canned_outputs
from waitless import cli
from .test_h3a1_holder_config import holder_check
from .test_r9_live_pending import pending_check
from .test_u119_holder import FlowSocket, TONE


@pytest.mark.parametrize('mode', ['answer','silence','ended','no_plan'])
async def test_check_live_handoff_order_and_frames(holder_check,monkeypatch,capsys,mode):
    check=holder_check
    outputs=canned_outputs()
    ready=outputs['get_call_status'].model_copy(update={'ended':False,'pending_choice':None})
    statuses=iter([ready,ready.model_copy(update={'ended':True,'handoff':None})])
    async def status(**kwargs):
        check.handlers.calls.append(('get_call_status',kwargs))
        return next(statuses)
    check.handlers.get_call_status=status
    check.handlers.outputs['get_case_result']=outputs['get_case_result']
    raw=yaml.safe_load(check.path.read_text())
    rep=raw['steps']['rep']
    rep['scripted'].update(holder_prompt_ms=40,holder_max_asks=10,reply_end_silence_ms=100)
    rep['scripted']['holder_reply']['then_keys']='1'
    if mode=='no_plan':
        rep['kind']='ai_agent'; rep['text_script']=[]; del rep['scripted']
    check.path.write_text(yaml.safe_dump(raw))
    check.synth.return_value=TONE
    ws=FlowSocket()
    if mode=='silence':
        async def silent(frame): ws.sent.append(frame)
        ws.send=silent
    if mode=='ended':
        await ws.incoming.put(json.dumps({'type':'ended'}))
    hello=[]
    recv,send=ws.recv,ws.send
    async def receive():
        if len(hello)==1:
            hello.append('ready')
            return json.dumps({'type':'ready'})
        return await recv()
    async def transmit(frame):
        if not hello:
            hello.append(json.loads(frame))
        else:
            await send(frame)
    ws.recv,ws.send=receive,transmit
    class Connection:
        async def __aenter__(self): return ws
        async def __aexit__(self,*args): await ws.cleanup()
    monkeypatch.setattr(websockets,'connect',lambda url:Connection())
    report=await cli.check_live('baseline',check.settings)
    assert [name for name,_ in check.handlers.calls]==[
        'prepare_task','update_task','start_assisted_call','get_call_status','join_call',
        'get_call_status','get_case_result']
    assert report.outcome=='confirmed'
    assert hello[0]=={'type':'hello','format':'pcm_s16le','sample_rate':16000,'channels':1}
    if mode=='answer':
        assert ws.sent[0]==TONE
        assert [json.loads(v) for v in ws.sent[1:]]==[
            {'type':'dtmf','keys':'01011990#'},{'type':'dtmf','keys':'1'}]
    elif mode=='silence':
        assert ws.sent==[TONE]
        assert 'holder question not heard' in capsys.readouterr().err
    else:
        assert not ws.sent
    if mode=='no_plan': check.synth.assert_not_called()
