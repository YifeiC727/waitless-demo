"""AC-74：转接期间的媒体门——送往 Sonic 的帧全是零值静音；Sonic 输出到线路的音频帧为 0；程序的 DTMF 帧为 0。

机制（tech §6.5、§7.1 AC-74；S0-r2 V6 零值帧方案）：
- MediaGateIn：转接期间把 InputAudioRawFrame 复制给 HandoffBridge（to_browser），往 Sonic 只送等长的零值帧；
- MediaGateOut：转接期间丢弃 Sonic 的音频帧（含 TTSAudioRawFrame）和所有 OutputDTMF* 帧，只放行
  push_user_audio() 注入的用户音频（OutputAudioRawFrame，16 kHz）；生命周期帧与 InterruptionFrame 照常传递；
- enter_handoff() 广播 interruption，清掉已排队的 AI 音频（inv-1 §1.8）；counters 只统计转接期间。
这里的链：线路 → MediaGateIn → Sonic（FakeSonicService，记录收到的输入帧）→ MediaGateOut → 线路；
Sonic 与程序产出的帧从 MediaGateOut 前面送入。
"""

from types import SimpleNamespace
from typing import Any

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputDTMFFrame,
    OutputDTMFUrgentFrame,
    TTSAudioRawFrame,
)

from call_engine._support import STEP_MS, Chain, FakeBridge, in_frame, is_all_zero, tone_pcm16
from waitless import models as m


@pytest.fixture
def mg(make_fake_sonic: Any, engine: Any) -> SimpleNamespace:
    from waitless.call.media_gate import MediaGate

    bridge = FakeBridge()
    gate = MediaGate(bridge=bridge)
    sonic = make_fake_sonic()
    chain = Chain(gate.inbound, sonic, gate.outbound)
    return SimpleNamespace(gate=gate, bridge=bridge, sonic=sonic, chain=chain, rate=engine.audio.pipeline_in_sample_rate)


def to_sonic(env: SimpleNamespace) -> list[InputAudioRawFrame]:
    return env.chain.relay_after(env.gate.inbound).down(InputAudioRawFrame)


async def push_ai_output(env: SimpleNamespace) -> None:
    """Sonic 的音频（24 kHz，Nova Sonic 默认输出）、一段非 TTS 的输出音频，以及程序的两种 DTMF 帧。"""
    for frame in (
        TTSAudioRawFrame(audio=tone_pcm16(40, 24000), sample_rate=24000, num_channels=1),
        OutputAudioRawFrame(audio=tone_pcm16(40, 24000), sample_rate=24000, num_channels=1),
        OutputDTMFUrgentFrame.from_string("1"),
        OutputDTMFFrame.from_string("9"),
    ):
        await env.chain.push_at(env.gate.outbound, frame)


async def test_ac74_media_flows_normally_outside_a_handoff(mg: SimpleNamespace) -> None:
    async with mg.chain:
        assert mg.gate.in_handoff is False
        for _ in range(3):
            await mg.chain.push(in_frame(tone_pcm16(STEP_MS, mg.rate), mg.rate))
        await push_ai_output(mg)
    assert mg.sonic.input_frames == 3 and mg.sonic.input_zero_frames == 0
    assert len(mg.chain.tail.down(TTSAudioRawFrame)) == 1
    assert len(mg.chain.tail.dtmf()) == 2
    assert mg.bridge.to_browser_frames == []
    assert mg.gate.counters == mg.gate.counters.__class__()  # 转接之外不计数


async def test_ac74_handoff_sends_only_zero_frames_to_sonic_and_copies_to_the_browser(mg: SimpleNamespace) -> None:
    async with mg.chain:
        await mg.gate.enter_handoff()
        assert mg.gate.in_handoff is True
        sent = [tone_pcm16(STEP_MS, mg.rate, amplitude=0.05 + 0.01 * i) for i in range(10)]
        for pcm in sent:
            await mg.chain.push(in_frame(pcm, mg.rate))
    frames = to_sonic(mg)
    assert len(frames) == 10
    assert all(is_all_zero(f.audio) for f in frames)
    assert [len(f.audio) for f in frames] == [len(p) for p in sent]
    assert {f.sample_rate for f in frames} == {mg.rate}
    assert mg.sonic.input_frames == 10 and mg.sonic.input_zero_frames == 10
    assert mg.bridge.to_browser_frames == sent
    c = mg.gate.counters
    assert (c.frames_to_sonic, c.nonzero_frames_to_sonic, c.frames_to_bridge) == (10, 0, 10)


async def test_ac74_handoff_blocks_ai_audio_and_dtmf_and_passes_user_audio(mg: SimpleNamespace) -> None:
    user = [bytes([i % 256]) * m.HANDOFF_FRAME_BYTES for i in range(1, 4)]
    async with mg.chain:
        await mg.gate.enter_handoff()
        mg.chain.tail.clear()
        await push_ai_output(mg)
        for pcm in user:
            await mg.gate.outbound.push_user_audio(pcm)
    out = mg.chain.tail
    assert out.down(TTSAudioRawFrame) == []
    assert out.dtmf() == []
    user_out = [f for f in out.down(OutputAudioRawFrame) if not isinstance(f, TTSAudioRawFrame)]
    assert [f.audio for f in user_out] == user
    assert {f.sample_rate for f in user_out} == {m.HANDOFF_SAMPLE_RATE}
    assert mg.sonic.input_frames == 0  # 用户的声音不进 Sonic
    c = mg.gate.counters
    assert (c.ai_audio_frames_out, c.dtmf_frames_out, c.user_audio_frames_out) == (0, 0, 3)


async def test_ac74_entering_a_handoff_broadcasts_an_interruption(mg: SimpleNamespace) -> None:
    async with mg.chain:
        mg.chain.tail.clear()
        await mg.gate.enter_handoff()
        assert mg.chain.tail.down(InterruptionFrame), "queued AI audio is not cleared on the line"
        await mg.chain.push_at(mg.gate.outbound, InterruptionFrame())  # 转接期间系统帧照常传递
    assert len(mg.chain.tail.down(InterruptionFrame)) >= 2


async def test_ac74_exit_restores_normal_media(mg: SimpleNamespace) -> None:
    async with mg.chain:
        await mg.gate.enter_handoff()
        await mg.chain.push(in_frame(tone_pcm16(STEP_MS, mg.rate), mg.rate))
        await mg.gate.exit_handoff()
        assert mg.gate.in_handoff is False
        mg.chain.tail.clear()
        await mg.chain.push(in_frame(tone_pcm16(STEP_MS, mg.rate), mg.rate))
        await push_ai_output(mg)
    assert mg.sonic.input_frames == 2 and mg.sonic.input_zero_frames == 1
    assert len(mg.chain.tail.down(TTSAudioRawFrame)) == 1 and len(mg.chain.tail.dtmf()) == 2
    assert len(mg.bridge.to_browser_frames) == 1
