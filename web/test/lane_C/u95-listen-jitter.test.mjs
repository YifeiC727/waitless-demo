import test, { beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { FakeWebSocket, FakeAudioContext, createFakeFetch } from '../helpers.mjs';
import { onListen, onStopListening } from '../../js/app.mjs';
import * as audio from '../../js/listen-audio.mjs';

const ticket = { task_id: 'T-100', call_id: 'C-1', ws_path: '/ws/listen/C-1', token: 'tok-l1', expires_at: '2026-10-14T15:05:00Z' };
const ready = { type: 'ready', format: 'pcm_s16le', sample_rate: 16000, channels: 1 };
beforeEach(() => { FakeWebSocket.reset(); FakeAudioContext.reset(); });

function session(t) {
  const context = new FakeAudioContext();
  context.currentTime = 10;
  const sources = [];
  const create = context.createBufferSource.bind(context);
  context.createBufferSource = () => { const source = create(); sources.push(source); return source; };
  const handle = audio.startListenAudio({ ticket, context, location: new URL('https://phone-tunnel.example/'), WebSocketImpl: FakeWebSocket });
  t.after(() => handle.stop());
  const ws = FakeWebSocket.last();
  ws.serverOpen(); ws.serverSend(ready);
  const send = (count) => { for (let i = 0; i < count; i += 1) ws.serverSend(new Int16Array(320).fill((i + 1) * 4096)); };
  return { context, sources, send };
}

test('u95 does not schedule before five frames', (t) => {
  const s = session(t);
  s.send(4);
  assert.equal(s.sources.length, 0);
});

test('u95 first chunk starts after prebuffer', (t) => {
  const s = session(t);
  s.send(5);
  assert.equal(s.sources.length, 1);
  assert.equal(s.sources[0].started[0], 10.25);
  assert.equal(s.sources[0].buffer.length, 1600);
  for (let i = 0; i < 5; i += 1) {
    assert.ok(Math.abs(s.sources[0].buffer.getChannelData(0)[i * 320] - (i + 1) / 8) < 1 / 32767);
  }
  assert.equal(audio.LISTEN_PREBUFFER_S, 0.25);
  assert.equal(audio.LISTEN_CHUNK_FRAMES, 5);
});

test('u95 chunks are contiguous', (t) => {
  const s = session(t);
  s.send(5); s.send(5);
  assert.equal(s.sources.length, 2);
  assert.equal(s.sources[1].started[0], 10.35);
});

test('u95 underrun rebuffers', (t) => {
  const s = session(t);
  s.send(5);
  s.context.currentTime = 11;
  s.send(5);
  assert.equal(s.sources.at(-1).started[0], 11.25);
});

test('u95 lag over one second resets with prebuffer', (t) => {
  const s = session(t);
  s.send(40);
  const previous = [...s.sources];
  assert.equal(previous.length, 8);
  s.send(5);
  assert.ok(previous.every((source) => source.stopped && source.disconnected));
  assert.equal(s.sources.length, 9);
  assert.equal(s.sources.at(-1).started[0], 10.25);
});

test('u95 onListen asks for a 16 kHz context and falls back', async (t) => {
  for (const rejectOptions of [false, true]) {
    const calls = [];
    class Context extends FakeAudioContext {
      constructor(...args) {
        calls.push(args);
        if (rejectOptions && args.length) throw new Error('unsupported sample rate');
        super(...args);
      }
    }
    const ctx = { state: { task_id: 'T-100' }, listen: null, AudioContextImpl: Context,
      WebSocketImpl: FakeWebSocket, location: new URL('https://phone-tunnel.example/'),
      fetchImpl: createFakeFetch({ routes: { 'POST /listen': ticket } }) };
    t.after(() => onStopListening(ctx));
    const pending = onListen(ctx);
    assert.deepEqual(calls[0], [{ sampleRate: 16000 }]);
    await pending;
    assert.deepEqual(calls, rejectOptions ? [[{ sampleRate: 16000 }], []] : [[{ sampleRate: 16000 }]]);
    assert.equal(typeof ctx.listen.stop, 'function');
    assert.match(FakeWebSocket.last().url, /\/ws\/listen\/C-1\?token=tok-l1$/);
    await onStopListening(ctx);
  }
});
