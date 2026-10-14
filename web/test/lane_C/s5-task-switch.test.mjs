import test from 'node:test';
import assert from 'node:assert/strict';
import { boot } from '../../js/app.mjs';
import { createFakeFetch, createFakeMcpServer } from '../helpers.mjs';
import { handoffEnv, taskCard, turnOut, pendingChoice, manualTimers } from '../c-support.mjs';
import { documentHarness } from './s5-dom.mjs';

test('K3 accepting another task clears old choices and cancels its poll timer', async () => {
  const timers = manualTimers();
  const originalSet = globalThis.setTimeout;
  const originalClear = globalThis.clearTimeout;
  globalThis.setTimeout = timers.setTimer;
  globalThis.clearTimeout = timers.clearTimer;
  let ctx;
  const { doc, nodes } = documentHarness();
  const server = createFakeMcpServer({ results: {
    prepare_task: [
      { task_id: 'T-old', status: 'ready', task_card: taskCard(), missing: [], skills: [] },
      { task_id: 'T-new', status: 'ready', task_card: taskCard({ card_version: 3 }), missing: [], skills: [] },
    ],
    get_call_status: { task_id: 'T-old', phase: 'awaiting_user', status_line: 'Choose an option', ended: false,
      handoff: null, pending_choice: pendingChoice({ choice_id: 'choice-old' }), transcript_tail: [] },
  } });
  const fetchImpl = createFakeFetch({ mcp: server, routes: {
    'GET /app-config': { mode: 'replay', poll_ms: 777 },
    'POST /assistant/turn': [
      turnOut({ kind: 'tool_call', name: 'prepare_task', tool_use_id: 'prep-old', arguments: { request: 'Old task' } }),
      turnOut({ kind: 'poll', task_id: 'T-old' }),
      turnOut({ kind: 'tool_call', name: 'prepare_task', tool_use_id: 'prep-new', arguments: { request: 'New task' } }),
      turnOut({ kind: 'await_user', ui: 'task_card' }),
    ],
  } });
  try {
    ctx = await boot({ document: doc, fetchImpl, ...handoffEnv() });
    await ctx.submit('Old task');
    await timers.runNext();
    assert.equal(ctx.state.choice_id, 'choice-old');
    assert.ok(nodes.has('pendingCard'));
    ctx.state.option_id = 'opt_paperless';
    await ctx.submit('New task');
    assert.equal(ctx.state.task_id, 'T-new');
    assert.equal(ctx.state.choice_id, null);
    assert.equal(ctx.state.option_id, null);
    assert.equal(nodes.has('pendingCard'), false);
    assert.equal(nodes.has('callStatus'), false);
    assert.equal(timers.pending.size, 0);
    assert.equal(await timers.runNext(), false);
    assert.equal(server.toolCalls().filter((name) => name === 'get_call_status').length, 1);
  } finally {
    await ctx?.dispose();
    globalThis.setTimeout = originalSet;
    globalThis.clearTimeout = originalClear;
  }
});
