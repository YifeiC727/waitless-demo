"""Loading failures after dial must still hang up the remote call."""
import pytest
from lane_D.agent_support import agent_call, settings, spec, engine, compiled, line_number, store, trace

@pytest.mark.parametrize('loader',['load_speaker_config','load_transfer_config'])
async def test_invalid_config_after_dial_hangs_up_and_finishes(agent_call,monkeypatch,loader):
    from waitless.call import live
    def fail(path): raise ValueError('invalid reference')
    monkeypatch.setattr(live,loader,fail,raising=False)
    h=agent_call; h.component_tools=False
    await h.start()
    with pytest.raises(ValueError,match='invalid reference'):
        await h.connect_line()
    assert h.carrier.hangups == [h.call._call_sid]
    assert h.call.ended
    assert len(h.events('call_ended'))==1
    assert h.on_ended.calls == [h.spec.call_id]
