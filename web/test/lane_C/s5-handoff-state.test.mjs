import test from 'node:test';
import assert from 'node:assert/strict';
import { boot, onJoinCall } from '../../js/app.mjs';
import { createFakeFetch, createFakeMcpServer, FakeWebSocket } from '../helpers.mjs';
import { handoffEnv, joinOut, taskCard, turnOut, settle } from '../c-support.mjs';
import { documentHarness } from './s5-dom.mjs';

test('K2 an ended status cancels a Join still waiting for microphone permission', async () => {
  const env = handoffEnv();
  const original = env.mediaDevices.getUserMedia.bind(env.mediaDevices);
  let allow;
  env.mediaDevices.getUserMedia = () => new Promise((resolve) => { allow = async () => resolve(await original({ audio: true })); });
  const { doc, nodes } = documentHarness();
  const server = createFakeMcpServer({ results: {
    prepare_task: { task_id: 'T-100', status: 'ready', task_card: taskCard(), missing: [], skills: [] },
    join_call: joinOut(),
    get_call_status: { task_id: 'T-100', phase: 'ended', status_line: 'Call ended', ended: true, handoff: null, transcript_tail: [] },
  } });
  const fetchImpl = createFakeFetch({ mcp: server, routes: {
    'GET /app-config': { mode: 'replay', poll_ms: 777 },
    'POST /assistant/turn': [
      turnOut({ kind: 'tool_call', name: 'prepare_task', tool_use_id: 'prep', arguments: { request: 'Start service' } }),
      turnOut({ kind: 'await_user', ui: 'task_card' }),
      turnOut({ kind: 'tool_call', name: 'get_call_status', tool_use_id: 'status', arguments: { task_id: 'T-100' } }),
      turnOut({ kind: 'done' }),
    ],
  } });
  const ctx = await boot({ document: doc, fetchImpl, ...env });
  try {
    await ctx.submit('Start service');
    const joining = onJoinCall(ctx);
    await settle(1);
    assert.equal(ctx._joining, true);
    await ctx.submit('Check current status');
    await allow();
    await joining;
    assert.equal(nodes.get('callView').hidden, true);
    assert.equal(ctx.handoff, null);
    assert.ok(env.mediaDevices.allTracksStopped());
    assert.equal(FakeWebSocket.last().readyState, FakeWebSocket.CLOSED);
  } finally { await ctx.dispose(); }
});
