import test from 'node:test';
import assert from 'node:assert/strict';
import { createStatusPoller } from '../../js/status-poller.mjs';
import { manualTimers, settle } from '../c-support.mjs';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

for (const oldOutcome of ['reject', 'resolve']) {
  test(`r2 restarted polling isolates the old ${oldOutcome} and retains its own in-flight request`, async () => {
    const timers = manualTimers();
    const old = deferred(), current = deferred();
    const calls = [], statuses = [];
    let ended = 0;
    const poller = createStatusPoller({
      mcp: { callTool(name, args) {
        calls.push({ name, args });
        return calls.length === 1 ? old.promise : current.promise;
      } },
      taskId: 'T-generation', pollMs: 777, ...timers,
      onStatus: (status) => statuses.push(status),
      onEnded: () => { ended += 1; },
    });
    poller.start();
    const oldTick = timers.runNext();
    // Observe rejection immediately so a failing implementation cannot produce an unhandled rejection.
    const oldObserved = oldTick.then(() => null, (error) => error);
    await settle(1);
    poller.stop();
    poller.start();
    const newTick = timers.runNext();
    const newObserved = newTick.then(() => null, (error) => error);
    await settle(1);
    const callsAfterRestart = calls.length;
    if (oldOutcome === 'reject') old.reject(new Error('network down (old request)'));
    else old.resolve({ ended: true, status_line: 'Old call ended' });
    await oldObserved;
    await settle(1);
    const sameGenerationRead = poller.pollOnce();
    const sameObserved = sameGenerationRead.then((status) => ({ status }), (error) => ({ error }));
    await settle(1);
    const callsAfterOldSettles = calls.length;
    const freshStatus = { ended: false, status_line: 'New call still running' };
    current.resolve(freshStatus);
    const newError = await newObserved;
    const sharedResult = await sameObserved;
    try {
      assert.equal(callsAfterRestart, 2, 'a new generation must start a fresh request while the old request is pending');
      assert.equal(callsAfterOldSettles, 2, 'old cleanup must not clear the current in-flight request');
      assert.equal(newError, null, 'old errors must not stop or reject the new generation');
      assert.deepEqual(sharedResult, { status: freshStatus });
      assert.deepEqual(statuses, [freshStatus], 'only the current generation may update the UI');
      assert.equal(ended, 0, 'an obsolete ended status must not finish the new generation');
      assert.equal(timers.pending.size, 1, 'new polling must continue after the old request settles');
      assert.ok(calls.every(({ name, args }) => name === 'get_call_status' && args.task_id === 'T-generation'));
    } finally { poller.stop(); }
    assert.equal(timers.pending.size, 0);
  });
}
