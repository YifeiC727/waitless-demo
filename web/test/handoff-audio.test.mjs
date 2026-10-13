// AC-14、AC-15：转接音频（tech §6.4；风险 K7）。公开接口：joinHandoffAudio({...}) -> {stop()}、downsampleToPcm16；
// web/js/pcm-worklet.js 在 AudioWorkletGlobalScope 里运行，这里用 node:vm 给它一个最小的全局环境。
// 麦克风、WebSocket、Web Audio 全部用 helpers.mjs 的假对象；断言看假对象收到的调用与字节。

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

import { joinHandoffAudio, downsampleToPcm16, PCM_WORKLET_PROCESSOR } from '../js/handoff-audio.mjs';
import { float32Sine, FakeWebSocket } from './helpers.mjs';
import { joinOut, openHandoff, sendReady, handleOf, emitSine, byteLen, int16Of, handoffEnv, settle } from './c-support.mjs';

const PHONE = new URL('https://phone-tunnel.example/?case=baseline');

test('ac14_getusermedia_once_after_join_and_ws_url_from_page_location', async () => {
  const join = joinOut({ handoff_id: 'H-91', token: 'tok-91' });
  const s = await openHandoff(joinHandoffAudio, { join, location: PHONE });
  handleOf(s);
  assert.equal(s.mediaDevices.calls.length, 1, 'getUserMedia exactly once');
  assert.ok(s.mediaDevices.calls[0]?.audio, 'audio is requested');
  assert.ok(!s.mediaDevices.calls[0]?.video, 'no camera');
  assert.equal(s.ws.url, 'wss://phone-tunnel.example/ws/handoff/H-91?token=tok-91', 'https page → wss, host from location, ws_path + token from join_call');
});

test('ac14_hello_first_then_640_byte_frames_only_after_ready', async () => {
  const s = await openHandoff(joinHandoffAudio, { join: joinOut(), location: PHONE, ready: false });
  if (s.state.error) throw s.state.error;
  assert.ok(s.ws, 'a WebSocket is opened');
  assert.ok(s.ws.sentText.length >= 1, 'hello is sent on open');
  assert.deepEqual(JSON.parse(s.ws.sentText[0]), { type: 'hello', format: 'pcm_s16le', sample_rate: 16000, channels: 1 });
  if (s.node) {
    emitSine(s.node, { sampleRate: s.ctx.sampleRate, seconds: 0.1 });
    assert.equal(s.ws.sentBinary.length, 0, 'no microphone audio before the server says ready');
  }
  await sendReady(s);
  handleOf(s);
  assert.ok(s.node, 'an AudioWorkletNode captures the microphone');
  emitSine(s.node, { sampleRate: s.ctx.sampleRate, seconds: 0.1 });
  const frames = s.ws.sentBinary;
  assert.ok(frames.length >= 4, `about five 20 ms frames for 100 ms of audio, got ${frames.length}`);
  assert.deepEqual([...new Set(frames.map(byteLen))], [640], 'every frame is 20 ms of 16 kHz PCM16 (640 bytes)');
  assert.ok(Math.max(...frames.flatMap((f) => Array.from(int16Of(f), Math.abs))) > 1000, 'frames carry the captured audio');
});

test('ac14_stop_stops_every_track_and_closes_the_websocket', async () => {
  const s = await openHandoff(joinHandoffAudio, { join: joinOut(), location: PHONE });
  const handle = handleOf(s);
  await handle.stop();
  assert.ok(s.mediaDevices.streams.length >= 1);
  assert.ok(s.mediaDevices.allTracksStopped(), 'all microphone tracks are stopped');
  assert.ok(s.ws.closeCalls.length >= 1, 'the handoff WebSocket is closed');
  assert.equal(s.mediaDevices.calls.length, 1, 'stopping never asks for the microphone again');
});

test('ac14_ended_frame_reports_reason_without_new_microphone_request', async () => {
  const s = await openHandoff(joinHandoffAudio, { join: joinOut(), location: PHONE });
  handleOf(s);
  s.ws.serverSend({ type: 'ended', reason: 'company_hangup' });
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(s.events.ended, ['company_hangup']);
  assert.equal(s.mediaDevices.calls.length, 1);
});

test('ac14_handle_is_returned_once_the_socket_exists_without_waiting_for_the_server', async () => {
  const env = handoffEnv({ location: PHONE });
  let handle = null;
  let error = null;
  joinHandoffAudio({ join: joinOut(), ...env }).then((h) => { handle = h; }, (e) => { error = e; });
  for (let i = 0; i < 40 && !handle && !error; i += 1) await settle(1);
  if (error) throw error;
  assert.ok(handle, 'G10: resolves before the server opens the socket or says ready');
  assert.equal(typeof handle.stop, 'function');
  assert.equal(typeof handle.setMuted, 'function');
  assert.equal(FakeWebSocket.instances.length, 1);
  assert.equal(FakeWebSocket.last().readyState, FakeWebSocket.CONNECTING);
  await handle.stop();
  assert.ok(env.mediaDevices.allTracksStopped(), 'stop() works before the socket is open');
});

test('ac14_set_muted_pauses_and_resumes_microphone_frames', async () => {
  const s = await openHandoff(joinHandoffAudio, { join: joinOut(), location: PHONE });
  const handle = handleOf(s);
  handle.setMuted(true);
  emitSine(s.node, { sampleRate: s.ctx.sampleRate, seconds: 0.1 });
  assert.equal(s.ws.sentBinary.length, 0, 'muted: no microphone frames are sent');
  assert.ok(s.mediaDevices.streams[0].getTracks().every((t) => !t.stopped), 'muting keeps the tracks');
  assert.equal(s.ws.closeCalls.length, 0, 'muting keeps the socket');
  handle.setMuted(false);
  emitSine(s.node, { sampleRate: s.ctx.sampleRate, seconds: 0.1 });
  assert.ok(s.ws.sentBinary.length >= 4, 'unmuted: frames flow again');
  assert.ok(s.ws.sentBinary.every((f) => byteLen(f) === 640));
});

test('ac14_worklet_posts_a_copy_of_the_mono_input_and_keeps_running', () => {
  const src = readFileSync(new URL('../js/pcm-worklet.js', import.meta.url), 'utf8');
  const registered = new Map();
  class AudioWorkletProcessor {
    constructor() {
      const posted = [];
      this.port = { posted, postMessage: (msg) => posted.push(msg), onmessage: null };
    }
  }
  const context = vm.createContext({
    AudioWorkletProcessor,
    registerProcessor: (name, cls) => registered.set(name, cls),
    Float32Array,
    sampleRate: 48000,
    currentTime: 0,
    currentFrame: 0,
  });
  vm.runInContext(src, context, { filename: 'pcm-worklet.js' });
  assert.equal(registered.size, 1, 'the worklet registers one processor');
  assert.deepEqual([...registered.keys()], [PCM_WORKLET_PROCESSOR], 'registered under PCM_WORKLET_PROCESSOR (G15)');
  const Proc = [...registered.values()][0];
  const proc = new Proc({});
  const input = float32Sine({ sampleRate: 48000, seconds: 128 / 48000 });
  const expected = Array.from(input);
  const keep = proc.process([[input]], [[new Float32Array(128)]], {});
  assert.equal(keep, true, 'process returns true to keep capturing');
  assert.equal(proc.port.posted.length, 1, 'one message per render quantum');
  const msg = proc.port.posted[0];
  assert.ok(ArrayBuffer.isView(msg) && msg.length === 128, 'posts the 128 mono samples');
  assert.notEqual(msg, input, 'posts a copy, not the reused input buffer');
  input.fill(0);
  assert.deepEqual(Array.from(msg), expected, 'the copy is unaffected when the browser reuses the input buffer');
});

test('ac15_one_second_48k_sine_becomes_16k_pcm16_length_and_peak', () => {
  const amplitude = 0.5;
  const out = downsampleToPcm16(float32Sine({ freq: 440, sampleRate: 48000, seconds: 1, amplitude }), 48000);
  assert.ok(out instanceof Int16Array, 'PCM16 samples as Int16Array');
  assert.ok(Math.abs(out.length - 16000) <= 1, `length 16000±1, got ${out.length}`);
  let peak = 0;
  for (const v of out) peak = Math.max(peak, Math.abs(v));
  const err = Math.abs(peak / 32767 - amplitude) / amplitude;
  assert.ok(err <= 0.01, `peak error ${(err * 100).toFixed(3)}% must be ≤1%`);
});
