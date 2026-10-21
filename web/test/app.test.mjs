// 按钮处理函数与出卡判断（C 线 r2；G4 定稿的 ctx）：
//   AC-06：4 个用户动作工具各由自己的按钮处理函数调用，参数正确，且只调这一个工具
//   AC-14：Join 之后才请求一次麦克风；点 Return 或离开通话页停掉音轨并关闭转接 WebSocket；离开通话页不调任何工具
//   AC-07 / A-14：只有 status=ready 且有 task_card 的结果才出任务卡
// 公开接口：app.mjs 的 onStartCall、onConfirmChoice、onJoinCall、onReturn、onLeaveCallPage、cardForToolResult。
// 涉及 onJoinCall 的测试设 5 秒超时：G10 规定句柄不等服务器就返回，等服务器的实现会卡住，超时让它以失败结束而不是挂起。

import test from 'node:test';
import assert from 'node:assert/strict';

import { onStartCall, onConfirmChoice, onJoinCall, onReturn, onLeaveCallPage, cardForToolResult } from '../js/app.mjs';
import { FakeWebSocket } from './helpers.mjs';
import {
  recordingMcp, handoffEnv, recordingHandle, callLog, joinOut, taskCard, caseResult, settle,
  buttonsByText, textOf, headings,
} from './c-support.mjs';

test('ac06_start_button_sends_confirmed_true_and_the_card_version', async () => {
  const mcp = recordingMcp({ start_assisted_call: { task_id: 'T-1', call_id: 'C-1', status: 'dialing' } });
  const out = await onStartCall({ mcp, state: { task_id: 'T-1', card_version: 3 }, handoff: null });
  assert.deepEqual(mcp.calls, [{ name: 'start_assisted_call', args: { task_id: 'T-1', confirmed: true, card_version: 3 } }]);
  assert.deepEqual(out, { task_id: 'T-1', call_id: 'C-1', status: 'dialing' });
});

test('ac06_confirm_button_sends_the_chosen_option', async () => {
  const mcp = recordingMcp({ answer_pending_choice: { task_id: 'T-1', choice_id: 'pc-1', status: 'choice_recorded', option_id: 'opt_paper' } });
  await onConfirmChoice({ mcp, state: { task_id: 'T-1', card_version: 3, choice_id: 'pc-1', option_id: 'opt_paper' }, handoff: null });
  assert.deepEqual(mcp.calls, [{ name: 'answer_pending_choice', args: { task_id: 'T-1', choice_id: 'pc-1', option_id: 'opt_paper' } }]);
});

test('ac06_join_button_calls_join_call_then_asks_for_the_microphone_once', { timeout: 5000 }, async () => {
  const join = joinOut({ handoff_id: 'H-55', token: 'tok-55' });
  const mcp = recordingMcp({ join_call: join });
  const env = handoffEnv();
  const order = callLog(mcp, env.mediaDevices);
  const shown = [];
  const ctx = { mcp, state: { task_id: 'T-100' }, handoff: null, ...env, show: (html) => shown.push(html) };
  await onJoinCall(ctx);
  await settle(2);
  assert.deepEqual(mcp.calls, [{ name: 'join_call', args: { task_id: 'T-100' } }], 'only join_call');
  assert.deepEqual(order, ['tool:join_call', 'getUserMedia'], 'the microphone is requested once, after join_call');
  assert.equal(ctx.state.handoff_id, 'H-55');
  assert.ok(ctx.handoff && typeof ctx.handoff.stop === 'function', 'ctx.handoff holds the audio handle');
  assert.equal(FakeWebSocket.last()?.url, 'wss://phone-tunnel.example/ws/handoff/H-55?token=tok-55');
  assert.ok(shown.some((h) => textOf(h).includes('AI is not listening')), 'the handoff card is shown');
});

test('ac06_return_button_calls_return_to_agent_and_stops_the_audio', async () => {
  const mcp = recordingMcp({ return_to_agent: { task_id: 'T-1', call_id: 'C-1', status: 'resuming', media: { ai_media: 'connected', recording: false, transcription: true } } });
  const handle = recordingHandle();
  const ctx = { mcp, state: { task_id: 'T-1', handoff_id: 'H-9' }, handoff: handle };
  await onReturn(ctx);
  assert.deepEqual(mcp.calls, [{ name: 'return_to_agent', args: { task_id: 'T-1', handoff_id: 'H-9' } }]);
  assert.equal(handle.stops, 1, 'the handoff audio is stopped');
  assert.equal(ctx.handoff, null);
});

test('ac14_leaving_the_call_page_stops_tracks_and_closes_the_socket_without_tool_calls', { timeout: 5000 }, async () => {
  const join = joinOut({ handoff_id: 'H-77' });
  const mcp = recordingMcp({ join_call: join });
  const env = handoffEnv();
  const ctx = { mcp, state: { task_id: 'T-100' }, handoff: null, ...env, show: () => {} };
  await onJoinCall(ctx);
  const ws = FakeWebSocket.last();
  if (ws.readyState === FakeWebSocket.CONNECTING) ws.serverOpen();
  ws.serverSend({ type: 'ready', company_name: join.company_name, media: join.media });
  await settle(3);
  await onLeaveCallPage(ctx);
  assert.ok(env.mediaDevices.allTracksStopped(), 'every microphone track is stopped');
  assert.ok(ws.closeCalls.length >= 1, 'the handoff WebSocket is closed');
  assert.equal(ctx.handoff, null);
  assert.deepEqual(mcp.calls.map((c) => c.name), ['join_call'], 'leaving the page calls no MCP tool (return_to_agent is a button action)');
  assert.equal(env.mediaDevices.calls.length, 1);
});

test('ac07_ready_prepare_result_becomes_a_task_card', () => {
  const card = taskCard();
  const html = cardForToolResult({
    tool_use_id: 'tu-1', name: 'prepare_task', is_error: false,
    structured: { task_id: 'T-1', status: 'ready', task_card: card, missing: [], skills: [], decisions: [], message: 'ready' },
  });
  assert.equal(typeof html, 'string');
  assert.ok(textOf(html).includes(card.company.official_number));
  assert.equal(buttonsByText(html, /start call/i).length, 1, 'the task card with its Start call button');
});

test('ac07_non_ready_or_failed_prepare_result_shows_no_card', () => {
  const base = { tool_use_id: 'tu-2', name: 'prepare_task', is_error: false };
  const notReady = [
    { task_id: 'T-2', status: 'setup_required', task_card: null, missing: [], skills: [], decisions: [], message: 'The official number could not be verified.' },
    { task_id: 'T-2', status: 'unsupported', task_card: null, missing: [], skills: [], decisions: [], message: 'Not supported yet.' },
  ];
  for (const structured of notReady) assert.equal(cardForToolResult({ ...base, structured }), null, `${structured.status}: no task card (A-14)`);
  assert.equal(cardForToolResult({ ...base, is_error: true, structured: { code: 'unknown_task', message: 'x' } }), null, 'errors show no card');
});

test('ac10_case_result_tool_result_becomes_the_result_card', () => {
  const html = cardForToolResult({ tool_use_id: 'tu-3', name: 'get_case_result', is_error: false, structured: caseResult() });
  assert.equal(typeof html, 'string');
  assert.ok(headings(html).some((h) => /^confirmed/i.test(h.text)), 'rendered as the result card');
});
