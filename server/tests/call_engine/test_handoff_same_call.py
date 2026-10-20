"""AC-76：用户加入的是同一通电话——转接前后线路的 callSid 不变，也没有新的 start 消息。

机制（tech §7.1 AC-76）：HandoffBridge 把 /ws/handoff 收到的用户音频接进同一条管线的 MediaGateOut
（push_user_audio），把 MediaGateIn 收到的对方音频复制给浏览器（to_browser）；不调运营商 POST /carrier/calls，
线路 WebSocket 不动。转接 WebSocket 的帧格式见 §6.4（hello / ready / 二进制 PCM16 16 kHz 20 ms / ended）。
"""

import asyncio
from typing import Any

import pytest
from pipecat.frames.frames import OutputAudioRawFrame, TTSAudioRawFrame

from call_engine._support import (
    COMPANY_NAME,
    handoff_path,
    HANDOFF_HELLO,
    AsgiWs,
    Chain,
    handoff_sonic,
    join_handoff,
    patch_menu_parser,
    settle,
    tone_pcm16,
    wait_until,
)
from waitless import models as m


from lane_D.agent_support import AgentCallHarness as LiveCallHarness, reach_handoff_ready

async def test_ac76_bridge_joins_the_browser_to_the_same_pipeline(engine: Any) -> None:
    from waitless.call.handoff import HandoffBridge
    from waitless.call.media_gate import MediaGate

    gate = MediaGate()
    chain = Chain(gate.inbound, gate.outbound)  # 线路 → MediaGateIn → MediaGateOut → 线路
    ws = AsgiWs(path=handoff_path("handoff-1"))
    await ws.websocket.accept()  # 端点校验令牌后已 accept
    bridge = HandoffBridge(websocket=ws.websocket, media_gate=gate, company_name=COMPANY_NAME)
    gate.attach_bridge(bridge)
    async with chain:
        await gate.enter_handoff()
        run = asyncio.ensure_future(bridge.run())
        ws.json(HANDOFF_HELLO)
        await wait_until(lambda: ws.sent_json(), what="ready frame")
        ready = m.HandoffReady.model_validate(ws.sent_json()[0])
        assert ready.company_name == COMPANY_NAME
        assert ready.media == m.JoinMedia(ai_media="disconnected", recording=False, transcription=False)

        mic = [bytes([i]) * m.HANDOFF_FRAME_BYTES for i in (1, 2, 3)]
        for pcm in mic:
            ws.binary(pcm)
        await wait_until(lambda: len([f for f in chain.tail.down(OutputAudioRawFrame) if not isinstance(f, TTSAudioRawFrame)]) >= 3,
                         what="browser audio on the line")
        to_line = [f for f in chain.tail.down(OutputAudioRawFrame) if not isinstance(f, TTSAudioRawFrame)]
        assert [f.audio for f in to_line] == mic
        assert {f.sample_rate for f in to_line} == {m.HANDOFF_SAMPLE_RATE}

        company = tone_pcm16(20, m.HANDOFF_SAMPLE_RATE)
        await bridge.to_browser(company)
        assert ws.sent_bytes() == [company]

        await bridge.close("returned")
        reason = await asyncio.wait_for(run, 5)
        await settle()
    assert reason == "returned"
    assert m.HandoffEnded.model_validate(ws.sent_json()[-1]) == m.HandoffEnded(type="ended", reason="returned")
    assert ws.closed


async def test_ac76_live_call_keeps_the_same_line_through_the_handoff(
    monkeypatch: pytest.MonkeyPatch, settings: Any, spec: Any, fake_clock: Any, trace: Any, gate: Any,
    make_fake_sonic: Any, fake_carrier: Any, store: Any, engine: Any,
) -> None:
    patch_menu_parser(monkeypatch)
    h = LiveCallHarness(monkeypatch=monkeypatch, settings=settings, spec=spec, clock=fake_clock, trace=trace,
                        gate=gate, sonic=handoff_sonic(make_fake_sonic), carrier=fake_carrier, store=store)
    try:
        await reach_handoff_ready(h, engine)
        call_sid = h.call_sid
        join, ws = await join_handoff(h)
        await h.speech(200)  # 对方说话：经同一条线路进来，转给浏览器
        assert ws.sent_bytes(), "company audio did not reach the browser"
        ws.binary(bytes([7]) * m.HANDOFF_FRAME_BYTES)  # 用户说话：经同一条线路出去
        await wait_until(lambda: h.transport.output().user_audio(m.HANDOFF_SAMPLE_RATE), what="user audio on the line")
        ai_audio_before_return = len(h.transport.output().tts_audio())
        await h.sonic_says("Blocked after user joins")  # Live remains isolated on the same line.
        await h.silence(200)
        hangups_before_cleanup = list(fake_carrier.hangups)
    finally:
        await h.close()
    assert len(fake_carrier.dials) == 1, "the engine dialled again during the handoff"
    assert [d.call_id for d in fake_carrier.dials] == [spec.call_id]
    assert h.call_sid == call_sid
    assert len(h.transport_builds) == 1, "a second line transport was built"
    assert len(h.transport.output().tts_audio()) == ai_audio_before_return, 'AI must remain silent after user joins' 
    assert hangups_before_cleanup == []
