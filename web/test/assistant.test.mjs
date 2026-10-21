// AC-05、AC-06：网页只执行 /assistant/turn 给的 next.tool_call；用户动作工具只由按钮处理函数调用
// （tech §6.2；风险 K11）。公开接口：runAssistantStep、postTurn、USER_ACTION_TOOLS；
// app.mjs 的 onStartCall、onConfirmChoice、onJoinCall、onReturn、onHangup、onReportResult 的位置约束用源码扫描检查（理由见测试计划第 3 节）。

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';

import { runAssistantStep, postTurn, USER_ACTION_TOOLS, USER_ACTION_UI } from '../js/assistant.mjs';
import { McpError } from '../js/mcp-client.mjs';
import { createFakeFetch, trackTimers } from './helpers.mjs';
import {
  turnOut, recordingMcp, jsTokens, functionBodyRange, arrayLiteralRange, within,
} from './c-support.mjs';

const TOOLS = [
  { name: 'find_official_number', description: 'Look up the official number' },
  { name: 'prepare_task', description: 'Prepare a call task' },
  { name: 'get_case_result', description: 'Read the case result' },
];

const SIX = ['start_assisted_call', 'answer_pending_choice', 'join_call', 'return_to_agent', 'hangup_call', 'report_call_result'];

function recordingPost(responses) {
  const bodies = [];
  const queue = [...responses];
  const post = async (body) => {
    bodies.push(structuredClone(body));
    if (!queue.length) throw new Error('recordingPost: unexpected extra /assistant/turn');
    return queue.shift();
  };
  post.bodies = bodies;
  return post;
}

function recordingHooks() {
  const log = [];
  return {
    log,
    hooks: {
      onAwaitUser: (ui) => log.push(['await_user', ui]),
      onPoll: (taskId) => log.push(['poll', taskId]),
      onSay: (say) => log.push(['say', say]),
      onDecision: (d) => log.push(['decision', d]),
    },
  };
}

test('ac05_next_tool_call_is_executed_via_mcp_and_result_posted_back', async () => {
  const prepared = { task_id: 'T-1', status: 'ready', task_card: null, missing: [], skills: [], decisions: [], message: 'ready' };
  const mcp = recordingMcp({ prepare_task: prepared });
  const args = { request: 'Start electricity at my new apartment on November 1.', provider_hint: 'Lakeshore Energy' };
  const first = turnOut({ kind: 'tool_call', tool_use_id: 'tu-1', name: 'prepare_task', arguments: args });
  const last = turnOut({ kind: 'await_user', ui: 'task_card' }, { say: 'Here is the call plan.' });
  const post = recordingPost([last]);
  const { hooks, log } = recordingHooks();

  const out = await runAssistantStep(first, { mcp, postTurn: post, conversationId: 'conv-1', tools: TOOLS, hooks });

  assert.deepEqual(mcp.calls, [{ name: 'prepare_task', args }],
    'exactly the tool and the arguments from next.tool_call, nothing added');
  assert.equal(post.bodies.length, 1);
  const body = post.bodies[0];
  assert.equal(body.conversation_id, 'conv-1');
  assert.deepEqual(body.tools, TOOLS);
  assert.deepEqual(body.tool_result, { tool_use_id: 'tu-1', name: 'prepare_task', structured: prepared, is_error: false });
  assert.deepEqual(out, last, 'returns the last AssistantTurnOut');
  assert.ok(log.some(([k, v]) => k === 'await_user' && v === 'task_card'));
  assert.ok(log.some(([k, v]) => k === 'say' && v === 'Here is the call plan.'));
});

test('ac05_chained_tool_calls_run_in_server_order_until_await_user', async () => {
  const mcp = recordingMcp({
    find_official_number: { company_id: 'lakeshore-energy', company_name: 'Lakeshore Energy', official_number: '1-800-555-0142', verified: true, sources: [], reason: 'two sources agree' },
    prepare_task: { task_id: 'T-2', status: 'ready', task_card: null, missing: [], skills: [], decisions: [], message: 'ready' },
  });
  const a1 = { company: 'Lakeshore Energy', intent: 'start service' };
  const a2 = { request: 'Start service', provider_hint: 'Lakeshore Energy' };
  const post = recordingPost([
    turnOut({ kind: 'tool_call', tool_use_id: 'tu-2', name: 'prepare_task', arguments: a2 }),
    turnOut({ kind: 'await_user', ui: 'task_card' }),
  ]);
  const { hooks } = recordingHooks();
  await runAssistantStep(turnOut({ kind: 'tool_call', tool_use_id: 'tu-1', name: 'find_official_number', arguments: a1 }),
    { mcp, postTurn: post, conversationId: 'conv-2', tools: TOOLS, hooks });
  assert.deepEqual(mcp.calls, [{ name: 'find_official_number', args: a1 }, { name: 'prepare_task', args: a2 }]);
  assert.deepEqual(post.bodies.map((b) => b.tool_result.tool_use_id), ['tu-1', 'tu-2']);
  assert.ok(post.bodies.every((b) => b.conversation_id === 'conv-2'));
});

test('ac05_await_user_poll_and_done_execute_no_tool', async () => {
  for (const next of [{ kind: 'await_user', ui: 'pending_choice' }, { kind: 'poll', task_id: 'T-3' }, { kind: 'done' }]) {
    const mcp = recordingMcp({});
    const post = recordingPost([]);
    const { hooks, log } = recordingHooks();
    const t = turnOut(next);
    const out = await runAssistantStep(t, { mcp, postTurn: post, conversationId: 'conv-1', tools: TOOLS, hooks });
    assert.equal(mcp.calls.length, 0, `${next.kind}: the page must not pick a tool on its own`);
    assert.equal(post.bodies.length, 0, `${next.kind}: nothing to post back`);
    assert.deepEqual(out, t);
    if (next.kind === 'poll') assert.ok(log.some(([k, v]) => k === 'poll' && v === 'T-3'), 'poll is handed to the status poller');
  }
});

test('ac05_tool_error_is_posted_back_with_is_error_true', async () => {
  const mcp = recordingMcp({ get_case_result: new McpError('unknown_task', 'No task T-9') });
  const post = recordingPost([turnOut({ kind: 'done' })]);
  const { hooks } = recordingHooks();
  await runAssistantStep(turnOut({ kind: 'tool_call', tool_use_id: 'tu-9', name: 'get_case_result', arguments: { task_id: 'T-9' } }),
    { mcp, postTurn: post, conversationId: 'conv-9', tools: TOOLS, hooks });
  assert.equal(post.bodies.length, 1);
  const tr = post.bodies[0].tool_result;
  assert.equal(tr.tool_use_id, 'tu-9');
  assert.equal(tr.name, 'get_case_result');
  assert.equal(tr.is_error, true);
  assert.deepEqual(tr.structured, { code: 'unknown_task', message: 'No task T-9' }, 'G7: structured is {code, message}');
});

test('ac05_each_tool_result_reaches_the_app_before_it_is_posted', async () => {
  const prepared = { task_id: 'T-1', status: 'ready', task_card: null, missing: [], skills: [], decisions: [], message: 'ready' };
  const mcp = recordingMcp({ prepare_task: prepared });
  const order = [];
  const post = async (body) => { order.push(['post', body.tool_result.tool_use_id]); return turnOut({ kind: 'await_user', ui: 'task_card' }); };
  const seen = [];
  await runAssistantStep(turnOut({ kind: 'tool_call', tool_use_id: 'tu-r', name: 'prepare_task', arguments: { request: 'r' } }), {
    mcp, postTurn: post, conversationId: 'conv-1', tools: TOOLS,
    hooks: { onToolResult: (tr) => { seen.push(tr); order.push(['result', tr.tool_use_id]); } },
  });
  assert.deepEqual(seen, [{ tool_use_id: 'tu-r', name: 'prepare_task', structured: prepared, is_error: false }]);
  assert.deepEqual(order, [['result', 'tu-r'], ['post', 'tu-r']], 'the app sees the result (to show the card) before the next turn');
});

test('ac05_assistant_loop_sets_no_timers', async () => {
  const timers = trackTimers();
  try {
    const mcp = recordingMcp({ prepare_task: { task_id: 'T-1', status: 'ready', task_card: null, missing: [], skills: [], decisions: [], message: '' } });
    const post = recordingPost([turnOut({ kind: 'await_user', ui: 'task_card' })]);
    await runAssistantStep(turnOut({ kind: 'tool_call', tool_use_id: 'tu-1', name: 'prepare_task', arguments: { request: 'x' } }),
      { mcp, postTurn: post, conversationId: 'conv-1', tools: TOOLS, hooks: {} });
  } finally {
    timers.restore();
  }
  assert.equal(timers.count(), 0, 'the assistant loop is driven by server responses, not timers');
});

test('ac05_postTurn_posts_json_to_relative_assistant_turn', async () => {
  const reply = turnOut({ kind: 'done' });
  const fetchImpl = createFakeFetch({ routes: { 'POST /assistant/turn': (req) => { fetchImpl.lastBody = req.body; return reply; } } });
  const body = { conversation_id: 'conv-1', user_text: 'Start electricity on November 1', tools: TOOLS };
  const out = await postTurn(body, { fetchImpl });
  assert.deepEqual(out, reply);
  const call = fetchImpl.callsTo('/assistant/turn')[0];
  assert.ok(call, 'POST /assistant/turn');
  assert.equal(call.method, 'POST');
  assert.doesNotMatch(call.url, /^[a-z]+:\/\//i, 'relative path only');
  assert.match(call.headers['content-type'] || '', /application\/json/);
  assert.deepEqual(call.body, body);
});

test('ac06_user_action_tool_in_next_is_never_executed', async () => {
  assert.deepEqual([...USER_ACTION_TOOLS].sort(), [...SIX].sort());
  assert.ok(Object.isFrozen(USER_ACTION_TOOLS));
  assert.deepEqual(USER_ACTION_UI, { start_assisted_call: 'task_card', answer_pending_choice: 'pending_choice', join_call: 'handoff', return_to_agent: 'handoff', hangup_call: 'handoff', report_call_result: 'result' });
  for (const name of SIX) {
    const mcp = recordingMcp(Object.fromEntries(SIX.map((n) => [n, { task_id: 'T-1' }])));
    const post = recordingPost([]);
    const { hooks, log } = recordingHooks();
    const t = turnOut({ kind: 'tool_call', tool_use_id: 'tu-x', name, arguments: { task_id: 'T-1', confirmed: true, card_version: 2 } });
    await runAssistantStep(t, { mcp, postTurn: post, conversationId: 'conv-1', tools: TOOLS, hooks });
    assert.equal(mcp.calls.length, 0, `${name} must only run from its button handler`);
    assert.equal(post.bodies.length, 0, `${name}: nothing is posted back because nothing ran`);
    assert.deepEqual(log.filter(([k]) => k === 'await_user'), [['await_user', USER_ACTION_UI[name]]], `${name}: waits on the ${USER_ACTION_UI[name]} UI`);
  }
});

test('ac06_user_action_tools_only_appear_in_their_button_handlers', () => {
  const jsDir = new URL('../js/', import.meta.url);
  const HANDLER = {
    start_assisted_call: 'onStartCall',
    answer_pending_choice: 'onConfirmChoice',
    join_call: 'onJoinCall',
    return_to_agent: 'onReturn',
    hangup_call: 'onHangup',
    report_call_result: 'onReportResult',
  };
  // scripted-cases.mjs 是脚本演示的本地结果，不经 MCP（PRD §3.2、Q1），不在扫描范围内。
  const files = readdirSync(jsDir).filter((f) => /\.m?js$/.test(f) && f !== 'scripted-cases.mjs');
  assert.ok(files.includes('app.mjs') && files.includes('assistant.mjs'));
  const violations = [];
  const appTokens = jsTokens(readFileSync(new URL('app.mjs', jsDir), 'utf8'));
  for (const file of files) {
    const tokens = file === 'app.mjs' ? appTokens : jsTokens(readFileSync(new URL(file, jsDir), 'utf8'));
    const allowedArray = file === 'assistant.mjs' ? arrayLiteralRange(tokens, 'USER_ACTION_TOOLS') : null;
    for (const tok of tokens) {
      if (tok.type !== 'string' || !SIX.includes(tok.value)) continue;
      if (file === 'assistant.mjs' && within(allowedArray, tok)) continue;
      if (file === 'app.mjs' && within(functionBodyRange(tokens, HANDLER[tok.value]), tok)) continue;
      violations.push(`${file}: '${tok.value}' outside ${file === 'app.mjs' ? HANDLER[tok.value] + '()' : 'USER_ACTION_TOOLS'}`);
    }
  }
  assert.deepEqual(violations, [], 'user-action tool names may only appear inside their button handlers');
  for (const [tool, handler] of Object.entries(HANDLER)) {
    const range = functionBodyRange(appTokens, handler);
    assert.ok(range, `app.mjs must define ${handler}()`);
    assert.ok(appTokens.some((t) => t.type === 'string' && t.value === tool && within(range, t)),
      `${handler}() must call ${tool}`);
  }
});
