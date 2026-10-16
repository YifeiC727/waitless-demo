import test, { beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { FakeWebSocket, FakeAudioContext } from '../helpers.mjs';
import { jsTokens } from '../c-support.mjs';
import { startListenAudio, listenAllowed, LISTEN_MAX_LAG_S } from '../../js/listen-audio.mjs';

const ticket = { task_id: 'T-100', call_id: 'C-1', ws_path: '/ws/listen/C-1', token: 'tok-l1', expires_at: '2026-10-14T15:05:00Z' };
const ready = { type: 'ready', format: 'pcm_s16le', sample_rate: 16000, channels: 1 };
beforeEach((t) => {
  const original = { fetch: globalThis.fetch, WebSocket: globalThis.WebSocket };
  globalThis.fetch = () => { throw new Error('Unexpected network fetch'); };
  globalThis.WebSocket = class { constructor() { throw new Error('Unexpected network socket'); } };
  t.after(() => Object.assign(globalThis, original));
  FakeWebSocket.reset();
  FakeAudioContext.reset();
});
function session(t, location = new URL('https://phone-tunnel.example/')) {
  const context = new FakeAudioContext();
  const sources = [];
  const create = context.createBufferSource.bind(context);
  context.createBufferSource = () => { const source = create(); sources.push(source); return source; };
  const ended = [];
  const handle = startListenAudio({ ticket, context, location, WebSocketImpl: FakeWebSocket, onEnded: (reason) => ended.push(reason) });
  t.after(() => handle.stop());
  return { context, sources, ended, handle, ws: FakeWebSocket.last() };
}
test('u92_ws_url_from_ticket_and_location', (t) => {
  const s = session(t);
  assert.equal(s.ws.url, 'wss://phone-tunnel.example/ws/listen/C-1?token=tok-l1');
  assert.equal(s.ws.binaryType, 'arraybuffer');
  assert.match(session(t, new URL('http://phone-tunnel.example/')).ws.url, /^ws:\/\//);
});
test('u92_sends_nothing_on_open_and_no_audio_ever', (t) => {
  const { ws } = session(t);
  ws.serverOpen(); ws.serverSend(ready);
  for (let i = 0; i < 5; i += 1) ws.serverSend(new Int16Array(320));
  assert.equal(ws.sent.length, 0);
});
test('u92_binary_before_ready_is_dropped', (t) => {
  const { ws, context } = session(t);
  ws.serverSend(new Int16Array(320));
  assert.equal(context.buffers.length, 0);
});
test('u92_ready_then_frames_play_back_to_back', (t) => {
  const { ws, context, sources } = session(t);
  ws.serverSend(ready);
  for (let i = 0; i < 3; i += 1) ws.serverSend(new Int16Array(320).fill(16384));
  assert.equal(context.buffers.length, 3);
  assert.equal(sources.length, 3);
  context.buffers.forEach((buffer) => {
    assert.equal(buffer.sampleRate, 16000); assert.equal(buffer.length, 320);
    assert.ok(Math.abs(buffer.getChannelData(0)[0] - 0.5) <= 1e-9);
  });
  sources.forEach((source, i) => {
    assert.equal(source.started.length, 1);
    assert.ok(Math.abs(source.started[0] - i * 0.02) <= 1e-9);
  });
});
test('u92_invalid_ready_ends', (t) => {
  const { ws, context, ended } = session(t);
  ws.serverSend({ ...ready, format: 'opus' });
  assert.deepEqual(ended, ['invalid_ready']);
  assert.ok(ws.closeCalls.length >= 1); assert.equal(context.state, 'closed');
});
test('u92_lag_over_one_second_resets_queue', (t) => {
  const { ws, sources } = session(t);
  ws.serverSend(ready);
  for (let i = 0; i < 60; i += 1) ws.serverSend(new Int16Array(320));
  assert.equal(LISTEN_MAX_LAG_S, 1);
  assert.equal(sources.length, 60);
  assert.ok(Math.max(...sources.flatMap((source) => source.started)) <= LISTEN_MAX_LAG_S);
  assert.ok(sources.filter((source) => source.stopped === true).length >= 50);
});
test('u92_ended_frame_reports_reason_and_releases', (t) => {
  const { ws, context, ended } = session(t);
  ws.serverSend({ type: 'ended', reason: 'handoff' });
  assert.deepEqual(ended, ['handoff']);
  assert.ok(ws.closeCalls.length >= 1); assert.equal(context.state, 'closed');
});
test('u92_stop_sends_stop_then_closes_and_is_idempotent', (t) => {
  const { ws, context, ended, handle } = session(t);
  ws.serverOpen(); handle.stop(); handle.stop();
  assert.deepEqual(ws.sentText, ['{"type":"stop"}']);
  assert.equal(ws.closeCalls.length, 1); assert.equal(context.state, 'closed');
  assert.deepEqual(ended, []);
});
test('u92_stop_before_open_does_not_send', (t) => {
  const { ws, handle } = session(t);
  assert.doesNotThrow(() => handle.stop()); assert.equal(ws.sent.length, 0);
});
test('u92_server_close_reports_disconnected', (t) => {
  const { ws, ended } = session(t);
  ws.serverClose(1006, ''); assert.deepEqual(ended, ['disconnected']);
});
test('u92_listen_allowed_by_mode_and_phase', () => {
  assert.equal(listenAllowed('live-sim', { phase: 'hold', ended: false }), true);
  for (const mode of ['text-sim', 'replay']) assert.equal(listenAllowed(mode, { phase: 'hold', ended: false }), false);
  for (const phase of ['handoff_ready', 'in_handoff', 'wrapping_up', 'ended']) assert.equal(listenAllowed('live-sim', { phase, ended: false }), false);
  assert.equal(listenAllowed('live-sim', { phase: 'hold', ended: true }), false);
});
test('u92_source_has_no_microphone_access', () => {
  const tokens = jsTokens(readFileSync(new URL('../../js/listen-audio.mjs', import.meta.url), 'utf8'));
  for (const name of ['mediaDevices', 'getUserMedia', 'AudioWorkletNode']) {
    assert.equal(tokens.some((token) => token.type === 'ident' && token.value === name), false);
  }
});
