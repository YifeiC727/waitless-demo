import test from 'node:test';
import assert from 'node:assert/strict';
import { createStatusPoller } from '../../js/status-poller.mjs';
import { boot, onReturn, onJoinCall, onLeaveCallPage } from '../../js/app.mjs';
import { joinHandoffAudio, downsampleToPcm16 } from '../../js/handoff-audio.mjs';
import { createFakeFetch, createFakeMcpServer, FakeWebSocket, FakeAudioContext, FakeAudioWorkletNode, float32Sine } from '../helpers.mjs';
import { handoffEnv, joinOut, recordingMcp, recordingHandle, openHandoff, handleOf, emitSine, settle, taskCard, turnOut, caseResult, COMMIT_A } from '../c-support.mjs';

test('poller accepts a string page URL and retains debug=true', async () => {
  const page = handoffEnv().location;
  page.searchParams.set('debug', '1');
  const mcp = recordingMcp({ get_call_status: { ended: false } });
  const poller = createStatusPoller({ mcp, taskId: 'T-edge', pollMs: 777, location: page.href });
  await poller.pollOnce();
  assert.equal(mcp.calls[0].args.debug, true);
});

test('Return stops the microphone immediately while the tool request is pending', async () => {
  let finish;
  const handle = recordingHandle();
  const ctx = { state: { task_id: 'T-edge', handoff_id: 'H-edge' }, handoff: handle,
    mcp: recordingMcp({ return_to_agent: () => new Promise((resolve) => { finish = resolve; }) }) };
  const returned = onReturn(ctx);
  await settle(1);
  const stoppedBeforeResponse = handle.stops;
  finish({ status: 'resuming' });
  await returned;
  assert.equal(stoppedBeforeResponse, 1);
  assert.equal(handle.stops, 1);
  assert.equal(ctx.handoff, null);
});

test('leaving during microphone permission stops the eventual stream', async () => {
  const env = handoffEnv();
  const original = env.mediaDevices.getUserMedia.bind(env.mediaDevices);
  let allow;
  env.mediaDevices.getUserMedia = () => new Promise((resolve) => { allow = async () => resolve(await original({ audio: true })); });
  const ctx = { ...env, state: { task_id: 'T-edge' }, handoff: null, mcp: recordingMcp({ join_call: joinOut() }) };
  const joining = onJoinCall(ctx);
  await settle(1);
  await onLeaveCallPage(ctx);
  await allow();
  await joining;
  assert.equal(ctx.handoff, null);
  assert.ok(env.mediaDevices.allTracksStopped());
  assert.equal(FakeWebSocket.last().readyState, FakeWebSocket.CLOSED);
});

test('microphone denial and worklet failure leave no open resources', async () => {
  let env = handoffEnv({ deny: true });
  await assert.rejects(joinHandoffAudio({ join: joinOut(), ...env }), { name: 'NotAllowedError' });
  assert.equal(FakeWebSocket.instances.length, 0);
  assert.equal(FakeAudioContext.instances.length, 0);
  env = handoffEnv();
  class BrokenContext extends FakeAudioContext {
    constructor() { super(); this.audioWorklet.addModule = async () => { throw new Error('worklet failed'); }; }
  }
  await assert.rejects(joinHandoffAudio({ join: joinOut(), ...env, AudioContextImpl: BrokenContext }), /worklet failed/);
  assert.ok(env.mediaDevices.allTracksStopped());
  assert.equal(FakeAudioContext.instances.at(-1).state, 'closed');
  assert.equal(FakeWebSocket.instances.length, 0);
});

test('128-sample chunks retain a second of audio at both input rates', async () => {
  for (const rate of [44100, 48000]) {
    const s = await openHandoff(joinHandoffAudio, { join: joinOut(), location: handoffEnv().location });
    s.ctx.sampleRate = rate;
    emitSine(s.node, { sampleRate: rate, seconds: 1 });
    assert.ok(Math.abs(s.ws.sentBinary.length * 320 - 16000) <= 320, `${rate}: frame count`);
    const handle = handleOf(s);
    await handle.stop();
    const sent = s.ws.sentBinary.length;
    emitSine(s.node, { sampleRate: rate, seconds: 0.1 });
    assert.equal(s.ws.sentBinary.length, sent);
  }
});

test('inbound PCM is queued for playback and never sent back', async () => {
  const s = await openHandoff(joinHandoffAudio, { join: joinOut(), location: handoffEnv().location });
  const samples = downsampleToPcm16(float32Sine({ sampleRate: 48000, seconds: 0.02 }), 48000);
  s.ws.serverSend(samples.buffer);
  s.ws.serverSend(samples.buffer);
  assert.equal(s.ctx.buffers.length, 2);
  assert.equal(s.ctx.buffers[0].sampleRate, 16000);
  assert.ok(s.ctx.buffers[0].getChannelData(0).some((value) => value !== 0));
  assert.equal(s.ws.sentBinary.length, 0);
  s.ws.serverClose();
  await settle(1);
  assert.ok(s.mediaDevices.allTracksStopped());
  assert.equal(s.ctx.state, 'closed');
  await handleOf(s).stop();
});

function documentHarness() {
  const doc = new EventTarget();
  const ids = ['messages', 'callView', 'chatView', 'traceView', 'error', 'chat', 'topActions', 'mode', 'prompt', 'composer', 'hero'];
  const nodes = new Map(ids.map((id) => {
    const node = new EventTarget();
    Object.assign(node, { innerHTML: '', textContent: '', value: '', hidden: true, isConnected: true,
      insertAdjacentHTML(_position, html) { this.innerHTML += html; }, remove() { nodes.delete(id); this.isConnected = false; } });
    return [id, node];
  }));
  doc.querySelector = (selector) => nodes.get(selector.slice(1)) ?? null;
  doc.querySelectorAll = () => [];
  doc.defaultView = new EventTarget();
  return { doc, nodes };
}

test('boot drives prepare and result cards through actual MCP and assistant requests', async () => {
  const { doc, nodes } = documentHarness();
  const page = handoffEnv().location;
  page.searchParams.set('debug', '1');
  const card = taskCard();
  const tools = ['prepare_task', 'get_case_result'].map((name) => ({ name, description: name }));
  const server = createFakeMcpServer({ tools, results: {
    prepare_task: { task_id: 'T-boot', status: 'ready', task_card: card, skills: [{ name: 'assisted-call', commit: COMMIT_A }], missing: [] },
    get_case_result: caseResult(),
  } });
  const fetchImpl = createFakeFetch({ mcp: server, routes: {
    'GET /app-config': { mode: 'replay', poll_ms: 777, replay_request_text: 'Recorded request' },
    'POST /assistant/turn': [
      turnOut({ kind: 'tool_call', name: 'prepare_task', tool_use_id: 'tu-prep', arguments: { request: 'Recorded request' } }),
      turnOut({ kind: 'await_user', ui: 'task_card' }, { say: 'Please review your plan.' }),
      turnOut({ kind: 'tool_call', name: 'get_case_result', tool_use_id: 'tu-result', arguments: { task_id: 'T-boot' } }),
      turnOut({ kind: 'done' }),
    ],
  } });
  const ctx = await boot({ document: doc, location: page, fetchImpl });
  assert.deepEqual(server.methods(), ['initialize', 'notifications/initialized', 'tools/list']);
  assert.equal(nodes.get('prompt').value, 'Recorded request');
  await ctx.submit('Recorded request');
  assert.equal(ctx.state.task_id, 'T-boot');
  assert.equal(ctx.state.card_version, card.card_version);
  assert.ok(nodes.get('messages').innerHTML.includes(card.company.official_number));
  assert.ok(nodes.get('topActions').innerHTML.includes('Developer trace'));
  assert.ok(nodes.get('traceView').innerHTML.includes(COMMIT_A));
  assert.ok(nodes.get('traceView').innerHTML.includes('width:82'));
  await ctx.submit('Show the result');
  assert.deepEqual(server.toolCalls(), ['prepare_task', 'get_case_result']);
  assert.ok(nodes.get('messages').innerHTML.includes('Needs follow-up'));
  assert.ok(fetchImpl.callsTo('/assistant/turn').every((c) => c.body.tools.length === 2));
  await ctx.dispose();
});
