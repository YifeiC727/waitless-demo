import test from 'node:test';
import assert from 'node:assert/strict';
import { boot } from '../../js/app.mjs';
import { createFakeFetch, createFakeMcpServer, jsonResponse } from '../helpers.mjs';
import { manualTimers, recordingHandle, settle, turnOut } from '../c-support.mjs';
import { documentHarness } from './s5-dom.mjs';

for (const [action, name, structured] of [
  ['start', 'start_assisted_call', { task_id: 'T-r2', call_id: 'C-start', status: 'dialing' }],
  ['return', 'return_to_agent', { task_id: 'T-r2', call_id: 'C-return', status: 'resuming', media: { ai_media: 'connected', recording: false, transcription: true } }],
]) {
  test(`r2 ${action} sends its successful MCP result and follows the assistant poll`, async () => {
    const timers = manualTimers();
    const originalSet = globalThis.setTimeout;
    const originalClear = globalThis.clearTimeout;
    globalThis.setTimeout = timers.setTimer;
    globalThis.clearTimeout = timers.clearTimer;
    const { doc, click, nodes } = documentHarness();
    const status = { task_id: 'T-r2', phase: 'ivr', ended: false, status_line: 'Still running', transcript_tail: [] };
    const server = createFakeMcpServer({ results: { [name]: structured, get_call_status: status } });
    const fetchImpl = createFakeFetch({ mcp: server, routes: {
      'GET /app-config': { mode: 'text-sim', poll_ms: 777 },
      'POST /assistant/turn': ({ body }) => body.tool_result
        ? turnOut({ kind: 'poll', task_id: 'T-r2' })
        : jsonResponse({ detail: 'one of user_text, tool_result or event is required' }, 422),
    } });
    let ctx;
    try {
      ctx = await boot({ document: doc, fetchImpl });
      Object.assign(ctx.state, { task_id: 'T-r2', card_version: 2, handoff_id: 'H-r2' });
      const handle = recordingHandle();
      if (action === 'return') ctx.handoff = handle;
      for (let i = 0; i < 2; i += 1) {
        click(action);
        await settle(3);
        const bodies = fetchImpl.callsTo('/assistant/turn').map((req) => req.body);
        assert.equal(bodies.length, i + 1);
        const body = bodies[i];
        assert.ok(body.tool_result, 'successful user action must send tool_result instead of an empty turn');
        assert.equal(body.conversation_id, ctx.state.conversation_id);
        assert.equal(body.task_id, 'T-r2');
        assert.deepEqual(body.tool_result, {
          tool_use_id: body.tool_result.tool_use_id, name, structured, is_error: false,
        });
        assert.equal(typeof body.tool_result.tool_use_id, 'string');
        assert.ok(body.tool_result.tool_use_id.length > 0);
        assert.equal(new Set(bodies.map((b) => b.tool_result.tool_use_id)).size, i + 1);
        assert.equal(nodes.get('error').hidden, true);
        assert.equal(timers.pending.size, 1, 'the returned poll instruction must start polling');
        await timers.runNext();
        assert.match(nodes.get('callStatus').innerHTML, /Still running/);
      }
      assert.deepEqual(server.toolCalls(), [name, 'get_call_status', name, 'get_call_status']);
      if (action === 'return') {
        assert.equal(handle.stops, 1);
        assert.equal(ctx.handoff, null);
        assert.equal(nodes.get('chatView').hidden, false);
      }
    } finally {
      await ctx?.dispose();
      globalThis.setTimeout = originalSet;
      globalThis.clearTimeout = originalClear;
    }
  });

  test(`r2 ${action} failure displays the error without sending an assistant turn`, async () => {
    const { doc, click, nodes } = documentHarness();
    const server = createFakeMcpServer({ results: { [name]: { isError: true, code: 'unknown_task', message: 'Task unavailable' } } });
    const fetchImpl = createFakeFetch({ mcp: server, routes: { 'GET /app-config': { mode: 'text-sim', poll_ms: 777 } } });
    const ctx = await boot({ document: doc, fetchImpl });
    const handle = recordingHandle();
    try {
      Object.assign(ctx.state, { task_id: 'T-r2', card_version: 2, handoff_id: 'H-r2' });
      if (action === 'return') ctx.handoff = handle;
      click(action);
      await settle(3);
      assert.deepEqual(server.toolCalls(), [name]);
      assert.equal(fetchImpl.callsTo('/assistant/turn').length, 0);
      assert.equal(nodes.get('error').hidden, false);
      assert.match(nodes.get('error').textContent, /Task unavailable/);
      if (action === 'return') assert.equal(handle.stops, 1);
    } finally { await ctx.dispose(); }
  });
}
