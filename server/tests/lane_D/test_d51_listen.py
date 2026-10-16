import asyncio
import pytest
from lane_D.agent_support import *
from lane_D.test_u92_live_listen import open_listener, media, Tap
from pipecat.frames.frames import OutputAudioRawFrame
from waitless.interfaces import ToolFailure

async def test_listen_with_agent_pipeline(agent_call):
    h = agent_call
    await h.boot()
    ws, task = await open_listener(h)
    serializer = h.transport_builds[0]
    # Real serializer takes both directions, with the hub installed by LiveCall.
    from pipecat.frames.frames import StartFrame
    await serializer.setup(StartFrame(audio_in_sample_rate=16000, audio_out_sample_rate=16000))
    for seq in range(1, 20):
        await serializer.deserialize(media(seq).replace('MZ' + '5' * 32, h.start_msg.streamSid))
    for _ in range(20):
        await serializer.serialize(OutputAudioRawFrame(audio=bytes(640), sample_rate=16000, num_channels=1))
    await wait_until(lambda: ws.sent_bytes(), what='listen PCM')
    assert len(ws.sent_bytes()) > 1
    assert h.call.status().interrupts_used == 0
    ws.json({'type': 'stop'})
    await asyncio.wait_for(task, 1)
    h.assert_trace_contract()

async def test_listen_refused_after_request_handoff(agent_call):
    h = agent_call
    await h.boot()
    await h.tool('request_handoff', {'reason': 'Verify'})
    with pytest.raises(ToolFailure) as exc:
        h.call.listen()
    assert exc.value.code == 'listen_unavailable'
    h.assert_trace_contract()
