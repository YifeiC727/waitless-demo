// AC-03、AC-04：网页是真正的 MCP 客户端（tech §6.1、inv-1 §2.2、§6.1；风险 K10）。
// 只经 web/js/mcp-client.mjs 的公开接口 createMcpClient、McpError 访问；后台用 helpers.mjs 的假 MCP 端点。

import test from 'node:test';
import assert from 'node:assert/strict';

import { createMcpClient, McpError } from '../js/mcp-client.mjs';
import { createFakeFetch, createFakeMcpServer, jsonResponse } from './helpers.mjs';

const TOOLS = [
  { name: 'prepare_task', description: 'Prepare a call task', inputSchema: { type: 'object' }, outputSchema: { type: 'object' } },
  { name: 'get_call_status', description: 'Read call status', inputSchema: { type: 'object' }, outputSchema: { type: 'object' } },
];

function setup(results = {}) {
  const mcp = createFakeMcpServer({ tools: TOOLS, results });
  const fetchImpl = createFakeFetch({ mcp });
  const traces = [];
  const client = createMcpClient({ fetchImpl, onTrace: (kind, label, payload) => traces.push({ kind, label, payload }) });
  return { mcp, fetchImpl, client, traces };
}

test('ac03_startup_sends_initialize_then_initialized_then_tools_list', async () => {
  const { mcp, client } = setup();
  await client.initialize();
  await client.listTools();
  assert.deepEqual(mcp.methods(), ['initialize', 'notifications/initialized', 'tools/list']);

  const init = mcp.requests[0];
  assert.equal(init.params?.protocolVersion, '2025-11-25');
  assert.equal(typeof init.params?.capabilities, 'object');
  assert.ok(init.params?.clientInfo?.name, 'initialize.clientInfo.name is required');
  assert.ok(init.params?.clientInfo?.version, 'initialize.clientInfo.version is required');
});

test('ac03_initialized_is_a_notification_without_id', async () => {
  const { fetchImpl, client } = setup();
  await client.initialize();
  const bodies = fetchImpl.calls.map((c) => c.body);
  const note = bodies.find((b) => b && b.method === 'notifications/initialized');
  assert.ok(note, 'notifications/initialized must be sent right after initialize');
  assert.equal(note.jsonrpc, '2.0');
  assert.ok(!('id' in note), 'a JSON-RPC notification carries no id');
});

test('ac03_every_request_carries_protocol_version_and_accept_headers', async () => {
  const { fetchImpl, client } = setup({ get_call_status: { task_id: 'T-1', phase: 'ivr' } });
  await client.initialize();
  await client.listTools();
  await client.callTool('get_call_status', { task_id: 'T-1' });
  assert.ok(fetchImpl.calls.length >= 4, 'initialize, initialized, tools/list and tools/call are all HTTP requests');
  for (const call of fetchImpl.calls) {
    assert.equal(call.method, 'POST', `${call.body?.method}: MCP requests are POST`);
    assert.equal(call.headers['mcp-protocol-version'], '2025-11-25', `${call.body?.method}: MCP-Protocol-Version header`);
    const accept = call.headers.accept || '';
    assert.ok(accept.includes('application/json'), `${call.body?.method}: Accept must include application/json, got ${accept}`);
    assert.ok(accept.includes('text/event-stream'), `${call.body?.method}: Accept must include text/event-stream, got ${accept}`);
    assert.match(call.headers['content-type'] || '', /application\/json/);
    assert.doesNotMatch(call.url, /^[a-z]+:\/\//i, 'the page only uses relative paths (no host or port)');
  }
});

test('ac03_request_ids_are_unique_and_tools_list_returns_server_tools', async () => {
  const { mcp, client } = setup({ prepare_task: { task_id: 'T-1' }, get_call_status: { task_id: 'T-1' } });
  await client.initialize();
  const listed = await client.listTools();
  await client.callTool('prepare_task', { request: 'Start electricity on November 1' });
  await client.callTool('get_call_status', { task_id: 'T-1' });
  const ids = mcp.requests.filter((r) => r.id !== undefined && r.id !== null).map((r) => r.id);
  assert.equal(ids.length, 4);
  assert.equal(new Set(ids).size, ids.length, 'JSON-RPC ids must not repeat');
  assert.ok(Array.isArray(listed), 'listTools() returns the result.tools array (G2)');
  assert.deepEqual(listed.map((t) => t.name), TOOLS.map((t) => t.name));
});

test('ac03_trace_entries_are_req_and_res_labelled_by_method', async () => {
  const { client, traces } = setup({ get_call_status: { task_id: 'T-1', phase: 'ivr' } });
  await client.initialize();
  await client.listTools();
  await client.callTool('get_call_status', { task_id: 'T-1' });
  assert.ok(traces.every((t) => t.kind === 'req' || t.kind === 'res'), `kinds: ${[...new Set(traces.map((t) => t.kind))]}`);
  const reqs = traces.filter((t) => t.kind === 'req').map((t) => t.label);
  const ress = traces.filter((t) => t.kind === 'res').map((t) => t.label);
  assert.deepEqual(reqs, ['initialize', 'notifications/initialized', 'tools/list', 'tools/call get_call_status']);
  for (const label of ['initialize', 'tools/list', 'tools/call get_call_status']) assert.ok(ress.includes(label), `a res entry for ${label}`);
  const callRes = traces.find((t) => t.kind === 'res' && t.label === 'tools/call get_call_status');
  assert.equal(callRes.payload?.result?.structuredContent?.phase, 'ivr', 'res payload is the parsed response body');
});

test('ac04_successful_call_returns_structured_content', async () => {
  const structured = { task_id: 'T-1', status: 'ready', task_card: null, missing: [], skills: [], decisions: [], message: 'ok' };
  const { mcp, client } = setup({ prepare_task: structured });
  await client.initialize();
  const out = await client.callTool('prepare_task', { request: 'Start electricity on November 1' });
  assert.deepEqual(out, structured);
  const call = mcp.requests.find((r) => r.method === 'tools/call');
  assert.deepEqual(call.params, { name: 'prepare_task', arguments: { request: 'Start electricity on November 1' } },
    'arguments are sent exactly as given (the prototype appended a scenario field)');
});

test('ac04_jsonrpc_error_throws_mcperror_with_code_and_is_traced', async () => {
  const { client, traces } = setup({ get_call_status: { rpcError: { code: -32602, message: 'Invalid params: task_id' } } });
  await client.initialize();
  const tracedBefore = traces.length;
  let traceCountAtThrow = -1;
  await assert.rejects(
    client.callTool('get_call_status', {}).catch((e) => { traceCountAtThrow = traces.length; throw e; }),
    (err) => {
      assert.ok(err instanceof McpError, 'JSON-RPC error must surface as McpError');
      assert.equal(err.code, -32602);
      return true;
    },
  );
  const newTraces = traces.slice(tracedBefore, traceCountAtThrow);
  assert.ok(newTraces.some((t) => JSON.stringify(t.payload ?? null).includes('-32602')),
    'the error response is written to the trace before the error is thrown');
});

test('ac04_iserror_result_throws_business_code_and_is_traced', async () => {
  const { client, traces } = setup({
    get_call_status: { isError: true, code: 'unknown_task', message: 'No task T-404' },
  });
  await client.initialize();
  const tracedBefore = traces.length;
  let traceCountAtThrow = -1;
  await assert.rejects(
    client.callTool('get_call_status', { task_id: 'T-404' }).catch((e) => { traceCountAtThrow = traces.length; throw e; }),
    (err) => {
      assert.ok(err instanceof McpError, 'isError result must surface as McpError');
      assert.equal(err.code, 'unknown_task');
      assert.match(err.message, /No task T-404/);
      return true;
    },
  );
  const newTraces = traces.slice(tracedBefore, traceCountAtThrow);
  assert.ok(newTraces.some((t) => JSON.stringify(t.payload ?? null).includes('unknown_task')),
    'the isError response is traced before the error is thrown');
});

test('ac04_transport_failure_rejects_without_local_fallback', async () => {
  const mcp = createFakeMcpServer({ tools: TOOLS });
  const ok = createFakeFetch({ mcp });
  let fail = false;
  const fetchImpl = async (input, init) => {
    if (fail) throw new TypeError('Failed to fetch');
    return ok(input, init);
  };
  const client = createMcpClient({ fetchImpl, onTrace: () => {} });
  await client.initialize();
  fail = true;
  let result;
  await assert.rejects(async () => { result = await client.callTool('get_call_status', { task_id: 'T-1' }); });
  assert.equal(result, undefined, 'no simulated result may be returned when the server is unreachable');
});

test('ac04_http_error_status_rejects_without_local_fallback', async () => {
  const mcp = createFakeMcpServer({ tools: TOOLS });
  const ok = createFakeFetch({ mcp });
  let status = 0;
  const fetchImpl = async (input, init) => (status ? jsonResponse({ error: 'boom' }, status) : ok(input, init));
  const client = createMcpClient({ fetchImpl, onTrace: () => {} });
  await client.initialize();
  status = 500;
  await assert.rejects(client.callTool('prepare_task', { request: 'x' }));
});
