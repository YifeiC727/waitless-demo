import test from 'node:test';
import assert from 'node:assert/strict';
import { boot } from '../../js/app.mjs';
import { createFakeFetch, createFakeMcpServer } from '../helpers.mjs';
import { handoffEnv, taskCard, turnOut, manualTimers, settle } from '../c-support.mjs';
import { documentHarness } from './s5-dom.mjs';

test('K5 a second explicit Start for the same task receives fresh status polling', async () => {
  const timers = manualTimers();
  const originalSet = globalThis.setTimeout;
  const originalClear = globalThis.clearTimeout;
  globalThis.setTimeout = timers.setTimer;
  globalThis.clearTimeout = timers.clearTimer;
  let ctx;
  const { doc, click, nodes } = documentHarness();
  const readyCard = taskCard();
  readyCard.precall_decisions[0].selected = 'ask_me';
  readyCard.precall_decisions[1].selected = 'paperless_yes';
  const server = createFakeMcpServer({ results: {
    prepare_task: { task_id: 'T-same', status: 'ready', task_card: readyCard, missing: [], skills: [] },
    update_task: { task_id: 'T-same', status: 'ready', task_card: { ...readyCard, card_version: 3 }, missing: [] },
    start_assisted_call: { task_id: 'T-same', call_id: 'C-next', status: 'dialing' },
    get_call_status: [
      { task_id: 'T-same', ended: true, phase: 'ended', status_line: 'First call ended', transcript_tail: [], handoff: null },
      { task_id: 'T-same', ended: false, phase: 'ivr', status_line: 'Second call menu', transcript_tail: [], handoff: null },
    ],
  } });
  const fetchImpl = createFakeFetch({ mcp: server, routes: {
    'GET /app-config': { mode: 'replay', poll_ms: 777 },
    'POST /assistant/turn': [
      turnOut({ kind: 'tool_call', name: 'prepare_task', tool_use_id: 'prep', arguments: { request: 'Start service' } }),
      turnOut({ kind: 'await_user', ui: 'task_card' }),
      turnOut({ kind: 'poll', task_id: 'T-same' }),
      turnOut({ kind: 'done' }),
      turnOut({ kind: 'tool_call', name: 'update_task', tool_use_id: 'update', arguments: { task_id: 'T-same', fields: { address: 'Updated address' } } }),
      turnOut({ kind: 'await_user', ui: 'task_card' }),
      turnOut({ kind: 'poll', task_id: 'T-same' }),
    ],
  } });
  try {
    ctx = await boot({ document: doc, fetchImpl, ...handoffEnv() });
    await ctx.submit('Start service');
    click('start');
    await settle(3);
    await timers.runNext();
    assert.equal(timers.pending.size, 0);
    await ctx.submit('Please call again with this updated address');
    click('start');
    await settle(3);
    assert.equal(server.toolCalls().filter((name) => name === 'start_assisted_call').length, 2);
    assert.equal(timers.pending.size, 1, 'a completed first call must not disable polling for the next call');
    await timers.runNext();
    assert.match(nodes.get('callStatus').innerHTML, /Second call menu/);
    assert.equal(server.toolCalls().filter((name) => name === 'get_call_status').length, 2);
  } finally {
    await ctx?.dispose();
    globalThis.setTimeout = originalSet;
    globalThis.clearTimeout = originalClear;
  }
});
