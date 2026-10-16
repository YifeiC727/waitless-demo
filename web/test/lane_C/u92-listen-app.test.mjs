import test, { beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { boot, onListen, onStopListening, onJoinCall, onLeaveCallPage } from '../../js/app.mjs';
import { renderListenControl } from '../../js/cards.mjs';
import { FakeWebSocket, FakeAudioContext, createFakeFetch, createFakeMcpServer, jsonResponse, trackTimers } from '../helpers.mjs';
import { handoffEnv, recordingHandle, recordingMcp, callLog, joinOut, settle, manualTimers, buttonsByText, textOf, elements, turnOut } from '../c-support.mjs';
import { documentHarness } from './s5-dom.mjs';

const ticket = { task_id: 'T-100', call_id: 'C-1', ws_path: '/ws/listen/C-1', token: 'tok-l1', expires_at: '2026-10-14T15:05:00Z' };
const running = { task_id: 'T-100', call_id: 'C-1', phase: 'hold', ended: false, status_line: 'Waiting', transcript_tail: [], handoff: null };
beforeEach((t) => {
  const original = { fetch: globalThis.fetch, WebSocket: globalThis.WebSocket };
  globalThis.fetch = () => { throw new Error('Unexpected network fetch'); };
  globalThis.WebSocket = class { constructor() { throw new Error('Unexpected network socket'); } };
  t.after(() => Object.assign(globalThis, original));
});
function context(t, route = ticket) {
  const env = handoffEnv();
  const fetchImpl = createFakeFetch({ routes: { 'POST /listen': route } });
  const ctx = { state: { task_id: 'T-100' }, listen: null, fetchImpl, ...env };
  t.after(() => onLeaveCallPage(ctx));
  return { ctx, env, fetchImpl };
}
async function page(t, mode = 'live-sim') {
  const timers = manualTimers();
  const original = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  globalThis.setTimeout = timers.setTimer; globalThis.clearTimeout = timers.clearTimer;
  let ctx;
  t.after(async () => { try { await ctx?.dispose(); } finally { Object.assign(globalThis, original); } });
  const env = handoffEnv();
  const { doc, nodes, click } = documentHarness();
  let status = running;
  const server = createFakeMcpServer({ results: {
    start_assisted_call: { task_id: 'T-100', call_id: 'C-1', status: 'dialing' },
    get_call_status: () => status,
  } });
  const fetchImpl = createFakeFetch({ mcp: server, routes: {
    'GET /app-config': { mode, poll_ms: 777 },
    'POST /listen': ticket,
    'POST /assistant/turn': () => turnOut({ kind: 'done' }),
  } });
  ctx = await boot({ document: doc, fetchImpl, ...env });
  Object.assign(ctx.state, { task_id: 'T-100', card_version: 2 });
  click('start'); await settle(3); await timers.runNext();
  return { ctx, env, doc, nodes, click, timers, server, fetchImpl, setStatus(value) { status = value; } };
}
async function listeningPage(t) {
  const p = await page(t);
  p.click('listen'); await settle(3);
  assert.ok(p.ctx.listen);
  return { ...p, ws: FakeWebSocket.last(), audio: FakeAudioContext.instances[0] };
}
test('u92_audiocontext_is_created_synchronously_before_fetch', async (t) => {
  let count;
  const { ctx, env } = context(t, () => { count = FakeAudioContext.instances.length; return ticket; });
  const p = onListen(ctx);
  assert.equal(FakeAudioContext.instances.length, 1);
  assert.equal(count, 1);
  await p;
  assert.equal(typeof ctx.listen.stop, 'function'); assert.equal(env.mediaDevices.calls.length, 0);
});
test('u92_listen_posts_task_id_as_relative_json', async (t) => {
  const { ctx, fetchImpl } = context(t);
  await onListen(ctx);
  const req = fetchImpl.callsTo('/listen')[0];
  assert.equal(req.method, 'POST'); assert.doesNotMatch(req.url, /^[a-z]+:\/\//i);
  assert.match(req.headers['content-type'], /application\/json/); assert.deepEqual(req.body, { task_id: 'T-100' });
});
test('u92_ticket_failure_closes_context_and_opens_no_socket', async (t) => {
  const { ctx } = context(t, jsonResponse({ code: 'listen_unavailable', message: 'No live call' }, 409));
  await assert.rejects(onListen(ctx), /No live call/);
  assert.equal(ctx.listen, null); assert.equal(FakeAudioContext.instances[0].state, 'closed'); assert.equal(FakeWebSocket.instances.length, 0);
});
test('u92_stop_while_fetching_ticket_aborts', async (t) => {
  let release;
  const { ctx } = context(t, () => new Promise((resolve) => { release = resolve; }));
  const pending = onListen(ctx);
  await onStopListening(ctx); release(ticket); await pending;
  assert.equal(FakeWebSocket.instances.length, 0); assert.equal(FakeAudioContext.instances[0].state, 'closed'); assert.equal(ctx.listen, null);
});
test('u92_join_stops_listening_before_join_call', async (t) => {
  const { ctx, env } = context(t);
  const mcp = ctx.mcp = recordingMcp({ join_call: joinOut() });
  const log = callLog(mcp, env.mediaDevices);
  const handle = ctx.listen = recordingHandle();
  const stop = handle.stop;
  handle.stop = () => { log.push('listen:stop'); return stop(); };
  await onJoinCall(ctx);
  assert.deepEqual(log.slice(0, 3), ['listen:stop', 'tool:join_call', 'getUserMedia']);
  assert.equal(ctx.listen, null); assert.deepEqual(mcp.calls.map((call) => call.name), ['join_call']);
});
test('u92_leave_call_page_stops_listening_without_tools', async (t) => {
  const { ctx } = context(t);
  const handle = ctx.listen = recordingHandle(); ctx.mcp = recordingMcp();
  await onLeaveCallPage(ctx);
  assert.equal(handle.stops, 1); assert.equal(ctx.mcp.calls.length, 0);
});
test('u92_render_listen_control', () => {
  const timers = trackTimers();
  try {
    const idle = renderListenControl({ listening: false });
    const buttons = buttonsByText(idle, /^Listen$/);
    assert.equal(buttons.length, 1); assert.equal(buttons[0].attrs.get('data-action'), 'listen'); assert.doesNotMatch(idle, /Listening only/);
    const active = renderListenControl({ listening: true });
    const stop = elements(active, 'button').filter((el) => el.attrs.get('data-action') === 'stop-listening');
    assert.equal(stop.length, 1); assert.equal(stop[0].text, 'Stop listening');
    assert.ok(textOf(active).includes('Listening only · microphone off · not recording'));
    assert.equal(timers.count(), 0);
  } finally { timers.restore(); }
});
test('u92_boot_live_sim_shows_listen_and_auto_stops_on_handoff', async (t) => {
  const p = await page(t);
  assert.match(p.nodes.get('listenControl').innerHTML, /data-action="listen"/);
  p.click('listen'); await settle(3);
  const ws = FakeWebSocket.last();
  assert.match(ws.url, /\/ws\/listen\/C-1\?token=tok-l1$/);
  p.setStatus({ ...running, phase: 'handoff_ready' }); await p.timers.runNext();
  assert.ok(ws.closeCalls.length >= 1); assert.equal(p.ctx.listen, null); assert.equal(p.nodes.has('listenControl'), false);
  assert.ok(p.server.toolCalls().every((name) => ['get_call_status', 'start_assisted_call'].includes(name)));
});
test('u92_boot_ended_status_stops_listening', async (t) => {
  const p = await listeningPage(t);
  p.setStatus({ ...running, phase: 'ended', ended: true }); await p.timers.runNext();
  assert.ok(p.ws.closeCalls.length >= 1); assert.equal(p.ctx.listen, null);
});
for (const event of ['pagehide', 'visibilitychange']) {
  test(`u92_pagehide_and_hidden_stop_listening: ${event}`, async (t) => {
    const p = await listeningPage(t);
    if (event === 'pagehide') p.doc.defaultView.dispatchEvent(new Event('pagehide'));
    else { p.doc.hidden = true; p.doc.dispatchEvent(new Event('visibilitychange')); }
    await settle(3);
    assert.ok(p.ws.closeCalls.length >= 1); assert.equal(p.audio.state, 'closed');
  });
}
for (const mode of ['text-sim', 'replay']) {
  test(`u92_text_sim_and_replay_show_no_listen: ${mode}`, async (t) => {
    const p = await page(t, mode);
    assert.equal(p.nodes.has('listenControl'), false); assert.equal(p.fetchImpl.callsTo('/listen').length, 0);
  });
}
test('u92_listen_never_requests_the_microphone', async (t) => {
  const p = await listeningPage(t);
  p.setStatus({ ...running, phase: 'handoff_ready' }); await p.timers.runNext();
  p.setStatus({ ...running, phase: 'ended', ended: true }); await p.timers.runNext();
  assert.equal(p.env.mediaDevices.calls.length, 0);
});
