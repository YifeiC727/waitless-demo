// next.kind=poll 的执行者（tech §6.1、§6.2；AC-05 的 poll 分支）与 debug=1 时 get_call_status 带 debug=true（AC-12 数据来源，§7.1）。
// 公开接口：createStatusPoller({mcp, taskId, pollMs, onStatus, onEnded, location, setTimer, clearTimer})（G5：location 缺省读全局）。

import test from 'node:test';
import assert from 'node:assert/strict';

import { createStatusPoller } from '../js/status-poller.mjs';
import { recordingMcp, manualTimers, settle } from './c-support.mjs';

function status(overrides = {}) {
  return {
    task_id: 'T-1', call_id: 'C-1', phase: 'ivr', status_line: 'Navigating the phone menu', transcript_tail: [],
    pending_choice: null, handoff: null, interrupts_used: 0, elapsed_s: 12.5, ended: false, debug: null, ...overrides,
  };
}

const PAGE = new URL('http://waitless.test/');
const DEBUG_PAGE = new URL('http://waitless.test/?debug=1');

test('ac05_poll_calls_only_get_call_status_for_the_task', async () => {
  const s = status();
  const mcp = recordingMcp({ get_call_status: s });
  const seen = [];
  const timers = manualTimers();
  const poller = createStatusPoller({ mcp, taskId: 'T-1', pollMs: 777, onStatus: (x) => seen.push(x), onEnded: () => {}, location: PAGE, ...timers });
  await poller.pollOnce();
  assert.equal(mcp.calls.length, 1);
  assert.equal(mcp.calls[0].name, 'get_call_status');
  assert.equal(mcp.calls[0].args.task_id, 'T-1');
  assert.notEqual(mcp.calls[0].args.debug, true, 'debug only when the page URL has debug=1');
  assert.deepEqual(seen, [s]);
});

test('ac05_poller_uses_poll_ms_and_stops_when_call_ended', async () => {
  const mcp = recordingMcp({ get_call_status: [status(), status({ phase: 'ended', ended: true, status_line: 'Call ended' })] });
  const timers = manualTimers();
  let ended = 0;
  const poller = createStatusPoller({ mcp, taskId: 'T-1', pollMs: 777, onStatus: () => {}, onEnded: () => { ended += 1; }, location: PAGE, ...timers });
  poller.start();
  await settle(3);
  for (let i = 0; i < 6 && ended === 0; i += 1) {
    if (!(await timers.runNext())) break;
  }
  assert.equal(ended, 1, 'onEnded once the status says ended');
  assert.equal(mcp.calls.length, 2);
  assert.ok(timers.delays.length >= 1, 'polling is scheduled through the injected timer');
  assert.ok(timers.delays.every((ms) => ms === 777), `interval comes from poll_ms, got ${timers.delays}`);
  assert.equal(timers.pending.size, 0, 'no timer left after the call ended');
});

test('ac12_poller_asks_for_debug_when_page_has_debug_1', async () => {
  const mcp = recordingMcp({ get_call_status: status({ debug: { mode: 'text-sim', skills: [], last_decision: null, guard: { blocked: 0, released: 0 } } }) });
  const timers = manualTimers();
  const poller = createStatusPoller({ mcp, taskId: 'T-1', pollMs: 500, onStatus: () => {}, onEnded: () => {}, location: DEBUG_PAGE, ...timers });
  await poller.pollOnce();
  assert.deepEqual(mcp.calls[0], { name: 'get_call_status', args: { task_id: 'T-1', debug: true } });
});
