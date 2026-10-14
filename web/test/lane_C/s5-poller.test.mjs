import test from 'node:test';
import assert from 'node:assert/strict';
import { createStatusPoller } from '../../js/status-poller.mjs';
import { manualTimers, settle } from '../c-support.mjs';

test('K1 stopping during an awaited status callback suppresses the old ended callback', async () => {
  let release;
  let ended = 0;
  const poller = createStatusPoller({
    mcp: { callTool: async () => ({ ended: true }) }, taskId: 'T-cancel', pollMs: 777,
    onStatus: () => new Promise((resolve) => { release = resolve; }),
    onEnded: () => { ended += 1; },
  });
  const pending = poller.pollOnce();
  await settle(1);
  poller.stop();
  release();
  await pending;
  assert.equal(ended, 0);
});

test('K1 restarting with an old read in flight leaves exactly one scheduled timer', async () => {
  let release;
  const timers = manualTimers();
  const poller = createStatusPoller({
    mcp: { callTool: () => new Promise((resolve) => { release = resolve; }) },
    taskId: 'T-restart', pollMs: 777, ...timers,
  });
  poller.start();
  const oldTick = timers.runNext();
  await settle(1);
  poller.stop();
  poller.start();
  release({ ended: false });
  await oldTick;
  assert.equal(timers.pending.size, 1);
  poller.stop();
  assert.equal(timers.pending.size, 0);
});
