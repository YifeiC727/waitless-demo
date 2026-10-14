import test from 'node:test';
import assert from 'node:assert/strict';
import { boot } from '../../js/app.mjs';
import { USER_ACTION_TOOLS } from '../../js/assistant.mjs';
import { createFakeFetch, createFakeMcpServer } from '../helpers.mjs';
import { manualTimers, pendingChoice, recordingHandle, settle, turnOut } from '../c-support.mjs';
import { documentHarness } from './s5-dom.mjs';

const taskId = 'T-r3-active';
const running = { task_id: taskId, call_id: 'C-r3-active', phase: 'ivr', ended: false,
  status_line: 'Waiting for the representative', transcript_tail: [], handoff: null };
const ready = { ...running, phase: 'handoff_ready', pending_choice: pendingChoice(),
  handoff: { state: 'ready', company_name: 'Lakeshore Energy',
    media: { ai_media: 'disconnected', recording: false, transcription: false } } };
const ended = { ...running, phase: 'ended', ended: true, status_line: 'Call ended' };

async function page(t, { next, action = 'start', afterEnded = { kind: 'done' }, assistant } = {}) {
  const timers = manualTimers();
  const originalSet = globalThis.setTimeout;
  const originalClear = globalThis.clearTimeout;
  globalThis.setTimeout = timers.setTimer;
  globalThis.clearTimeout = timers.clearTimer;
  let ctx;
  t.after(async () => {
    try { await ctx?.dispose(); }
    finally {
      globalThis.setTimeout = originalSet;
      globalThis.clearTimeout = originalClear;
    }
  });
  const { doc, nodes, click } = documentHarness();
  let status = running;
  const name = action === 'start' ? 'start_assisted_call' : 'return_to_agent';
  const server = createFakeMcpServer({ results: {
    [name]: { task_id: taskId, call_id: running.call_id, status: action === 'start' ? 'dialing' : 'resuming' },
    get_call_status: () => status,
  } });
  const fetchImpl = createFakeFetch({ mcp: server, routes: {
    'GET /app-config': { mode: 'text-sim', poll_ms: 777 },
    'POST /assistant/turn': assistant ?? (({ body }) => turnOut(body.event === 'call_ended' ? afterEnded : next)),
  } });
  ctx = await boot({ document: doc, fetchImpl });
  Object.assign(ctx.state, { task_id: taskId, card_version: 2, handoff_id: 'H-r3-active' });
  const handle = recordingHandle();
  if (action === 'return') ctx.handoff = handle;
  return { ctx, doc, nodes, click, timers, server, fetchImpl, handle, name,
    setStatus(value) { status = value; } };
}

for (const action of ['start', 'return']) {
  for (const next of [{ kind: 'await_user', ui: 'task_card' }, { kind: 'done' }]) {
    test(`I1-3 ${action}: ${next.kind} keeps polling through handoff and stops once at ended`, async (t) => {
      const p = await page(t, { action, next });
      p.click(action);
      await settle(3);
      assert.equal(p.timers.pending.size, 1, 'successful action starts polling independently of assistant next');
      await p.timers.runNext();
      assert.match(p.nodes.get('callStatus').innerHTML, /Waiting for the representative/);
      p.setStatus(ready);
      await p.timers.runNext();
      assert.match(p.nodes.get('handoffReady').innerHTML, /Lakeshore Energy is ready/);
      assert.match(p.nodes.get('handoffReady').innerHTML, /Join call/);
      assert.ok(p.nodes.has('pendingCard'));
      assert.equal(p.timers.pending.size, 1);
      assert.deepEqual(p.server.toolCalls().filter((tool) => USER_ACTION_TOOLS.includes(tool)), [p.name]);
      p.setStatus(ended);
      await p.timers.runNext();
      assert.equal(p.fetchImpl.callsTo('/assistant/turn').filter((req) => req.body.event === 'call_ended').length, 1);
      assert.equal(p.timers.pending.size, 0);
      assert.equal(await p.timers.runNext(), false);
      assert.equal(p.server.toolCalls().filter((tool) => tool === 'get_call_status').length, 3);
      if (action === 'return') {
        assert.equal(p.handle.stops, 1);
        assert.equal(p.ctx.handoff, null);
      }
    });
  }
}

test('I1-3 assistant poll and read-only tool_call share the ongoing status poller', async (t) => {
  let turns = 0;
  const p = await page(t, { assistant: ({ body }) => {
    if (body.event) return turnOut({ kind: 'done' });
    turns += 1;
    return turnOut(turns === 1 ? { kind: 'poll', task_id: taskId }
      : turns === 2 ? { kind: 'tool_call', name: 'get_call_status', tool_use_id: 'read-r3', arguments: { task_id: taskId } }
      : { kind: 'await_user', ui: 'handoff' });
  } });
  p.click('start');
  await settle(3);
  assert.equal(p.timers.pending.size, 1, 'assistant poll must not duplicate the action poller');
  p.setStatus(ready);
  await p.ctx.submit('Please check the call');
  assert.ok(p.nodes.has('handoffReady'));
  assert.equal(p.timers.pending.size, 1, 'read-only assistant tool must not stop or duplicate polling');
  p.setStatus(ended);
  await p.timers.runNext();
  assert.equal(p.timers.pending.size, 0);
  assert.deepEqual(p.server.toolCalls(), ['start_assisted_call', 'get_call_status', 'get_call_status']);
});

for (const name of USER_ACTION_TOOLS) {
  test(`I1-3 ongoing polling never executes assistant-proposed ${name}`, async (t) => {
    const p = await page(t, { next: { kind: 'tool_call', name, tool_use_id: 'blocked-r3', arguments: { task_id: taskId } } });
    p.click('start');
    await settle(3);
    p.setStatus(ready);
    assert.equal(await p.timers.runNext(), true, 'polling continues even when the assistant waits for a user action');
    assert.deepEqual(p.server.toolCalls(), ['start_assisted_call', 'get_call_status']);
    assert.ok(p.nodes.has('handoffReady'));
  });
}

test('I1-3 ended does not restart polling even if its assistant response says poll', async (t) => {
  const p = await page(t, { next: { kind: 'poll', task_id: taskId }, afterEnded: { kind: 'poll', task_id: taskId } });
  p.click('start');
  await settle(3);
  p.setStatus(ended);
  await p.timers.runNext();
  assert.equal(p.timers.pending.size, 0, 'ended is terminal until another explicit successful Start');
  assert.equal(await p.timers.runNext(), false);
  assert.equal(p.fetchImpl.callsTo('/assistant/turn').filter((req) => req.body.event === 'call_ended').length, 1);
});

test('I1-3 status polling runs while the Start assistant response is still pending', async (t) => {
  let release;
  const p = await page(t, { assistant: () => new Promise((resolve) => { release = resolve; }) });
  p.click('start');
  await settle(3);
  try {
    p.setStatus(ready);
    assert.equal(await p.timers.runNext(), true);
    assert.ok(p.nodes.has('handoffReady'));
  } finally {
    release(turnOut({ kind: 'done' }));
    await settle(3);
  }
});

for (const navigation of ['leave', 'background']) {
  test(`I1-3 ${navigation} stops polling and audio without performing user actions`, async (t) => {
    const p = await page(t, { next: { kind: 'poll', task_id: taskId } });
    p.click('start');
    await settle(3);
    p.ctx.handoff = p.handle;
    if (navigation === 'leave') p.click('leave');
    else { p.doc.hidden = true; p.doc.dispatchEvent(new Event('visibilitychange')); }
    await settle(3);
    assert.equal(p.timers.pending.size, 0);
    assert.equal(p.handle.stops, 1);
    assert.deepEqual(p.server.toolCalls(), ['start_assisted_call']);
  });
}

test('I1-3 a late assistant poll cannot undo leaving the call page', async (t) => {
  let release;
  const p = await page(t, { assistant: () => new Promise((resolve) => { release = resolve; }) });
  p.click('start');
  await settle(3);
  assert.equal(p.timers.pending.size, 1);
  p.click('leave');
  release(turnOut({ kind: 'poll', task_id: taskId }));
  await settle(3);
  assert.equal(p.timers.pending.size, 0);
  assert.deepEqual(p.server.toolCalls(), ['start_assisted_call']);
});

test('I1-3 another explicit Start after ended creates a fresh poller even with assistant done', async (t) => {
  const p = await page(t, { next: { kind: 'done' } });
  p.click('start');
  await settle(3);
  p.setStatus(ended);
  await p.timers.runNext();
  assert.equal(p.timers.pending.size, 0);
  p.setStatus({ ...running, call_id: 'C-r3-next', status_line: 'Next call started' });
  p.click('start');
  await settle(3);
  assert.equal(p.timers.pending.size, 1);
  await p.timers.runNext();
  assert.match(p.nodes.get('callStatus').innerHTML, /Next call started/);
  p.setStatus({ ...ended, call_id: 'C-r3-next' });
  await p.timers.runNext();
  assert.equal(p.timers.pending.size, 0);
  assert.equal(p.fetchImpl.callsTo('/assistant/turn').filter((req) => req.body.event === 'call_ended').length, 2);
});
