import test, { beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import * as app from '../../js/app.mjs';
import { renderHandoffCard, renderResult } from '../../js/cards.mjs';
import { runAssistantStep } from '../../js/assistant.mjs';
import { FakeWebSocket, createFakeFetch, createFakeMcpServer } from '../helpers.mjs';
import { handoffEnv, joinOut, caseResult, manualTimers, settle, turnOut, startTags, textOf, recordingMcp } from '../c-support.mjs';

beforeEach((t) => {
  const original = { fetch: globalThis.fetch, WebSocket: globalThis.WebSocket };
  globalThis.fetch = () => { throw new Error('Unexpected network fetch'); };
  globalThis.WebSocket = class { constructor() { throw new Error('Unexpected network socket'); } };
  t.after(() => Object.assign(globalThis, original));
});

// Render controls and their lifetimes, so disabled/submitting and stale clicks
// are exercised through the same delegated events as the real page.
function dom() {
  const doc = new EventTarget(), nodes = new Map();
  function node(id) {
    if (nodes.has(id)) return nodes.get(id);
    const out = new EventTarget();
    let html = '';
    Object.assign(out, { hidden: false, textContent: '', value: '', controls: [], isConnected: true,
      remove() { out.isConnected = false; out.controls.forEach((c) => { c.isConnected = false; }); nodes.delete(id); },
      insertAdjacentHTML(_where, text) { html += text; parse(text); },
    });
    function parse(text) {
      const rest = text.replace(/^<div id="([^"]+)" class="tool-card">([\s\S]*)<\/div>$/g,
        (_all, slot, content) => { node(slot).innerHTML = content; return ''; });
      for (const { tag, attrs } of startTags(rest, 'button|input')) {
        const control = { tag, attrs, disabled: attrs.has('disabled'), checked: attrs.has('checked'), isConnected: true,
          value: attrs.get('value') ?? '', dataset: Object.fromEntries([...attrs].filter(([k]) => k.startsWith('data-')).map(([k, v]) => [k.slice(5).replace(/-([a-z])/g, (_m, c) => c.toUpperCase()), v])),
          closest: () => control, matches: () => false,
        };
        out.controls.push(control);
      }
      if (rest.includes('keypad-feedback')) out.feedback = { textContent: '' };
    }
    Object.defineProperty(out, 'innerHTML', { get: () => html, set(text) {
      out.controls.forEach((c) => { c.isConnected = false; }); out.controls = []; html = text; parse(text);
    } });
    nodes.set(id, out); return out;
  }
  for (const id of ['messages', 'callView', 'chatView', 'error', 'prompt', 'composer', 'hero', 'mode']) node(id);
  nodes.get('error').hidden = true;
  doc.querySelectorAll = () => [...nodes.values()].flatMap((n) => n.controls);
  doc.querySelector = (selector) => {
    if (selector === '#callView .keypad-feedback') return nodes.get('callView').feedback;
    if (selector === '#resultConfirmation') return doc.querySelectorAll().find((c) => c.attrs.get('id') === 'resultConfirmation');
    const [id, child] = selector.slice(1).split(' ');
    return child ? nodes.get(id)?.controls.find((c) => c.dataset.action === child.match(/data-action="([^"]+)"/)?.[1]) : nodes.get(id);
  };
  doc.defaultView = new EventTarget();
  function dispatch(type, control) {
    assert.ok(control, `rendered control exists: ${type}`);
    if (control.disabled) return;
    const event = new Event(type); Object.defineProperty(event, 'target', { value: control }); doc.dispatchEvent(event);
  }
  return { doc, nodes, dispatch,
    button(action) { return doc.querySelectorAll().find((c) => c.dataset.action === action); },
    click(action) { dispatch('click', this.button(action)); },
    choose(outcome) { const c = nodes.get('resultCard')?.controls.find((c) => c.value === outcome); if (c) c.checked = true; dispatch('change', c); },
    number(value) { const c = doc.querySelector('#resultConfirmation'); assert.ok(c); c.value = value; dispatch('input', c); },
    text(id) { return textOf(nodes.get(id)?.innerHTML ?? ''); },
  };
}
const initial = { task_id: 'T-100', call_id: 'C-1', phase: 'handoff_ready', status_line: 'Ready', ended: false, transcript_tail: [],
  handoff: { state: 'ready', company_name: 'Lakeshore Energy', reason: 'Needs <img src=x>本人', reason_kind: 'live_human', generation: 1, remaining_s: 73, expires_at: '2026-10-20T12:01:30Z' } };
const awaiting = () => caseResult({ outcome: 'needs_follow_up', headline: 'Call ended', confirmation_number: null, verified: [], result_source: 'transcript', confirmation_source: null, awaiting_user_result: true });
const reported = (outcome = 'confirmed', number = null) => ({ call_id: 'C-1', outcome, confirmation_number: number, verified: [], follow_ups: [], summary: 'Saved', result_source: 'user', confirmation_source: number ? 'user' : null, awaiting_user_result: false });
function deferred() { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; }

async function page(t, { mode = 'live-sim', result = awaiting(), hangup, report, storage, restore = false } = {}) {
  const timers = manualTimers(), original = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  Object.assign(globalThis, { setTimeout: timers.setTimer, clearTimeout: timers.clearTimer });
  let status = initial, ctx, currentResult = result;
  const d = dom();
  const server = createFakeMcpServer({ results: {
    get_call_status: () => status,
    get_case_result: () => currentResult,
    join_call: joinOut(),
    hangup_call: hangup ?? { status: 'hung_up', call_id: 'C-1', remote_hangup_pending: false },
    report_call_result: report ?? ((args) => reported(args.outcome, args.confirmation_number)),
    return_to_agent: { task_id: 'T-100', call_id: 'C-1', status: 'resuming' },
  } });
  const fetchImpl = createFakeFetch({ mcp: server, routes: {
    'GET /app-config': { mode, poll_ms: 777 },
    'POST /assistant/turn': ({ body }) => body.user_text === 'result'
      ? turnOut({ kind: 'tool_call', name: 'get_case_result', tool_use_id: 'result', arguments: { task_id: 'T-100' } })
      : body.tool_result?.name === 'get_case_result' ? turnOut({ kind: 'done' }) : turnOut({ kind: 'poll', task_id: 'T-100' }),
  } });
  t.after(async () => { try { await ctx?.dispose(); } finally { Object.assign(globalThis, original); } });
  ctx = await app.boot({ document: d.doc, fetchImpl, storage, ...handoffEnv() });
  if (!restore) { ctx.state.task_id = 'T-100'; await ctx.submit('status'); await timers.runNext(); }
  return { ...d, ctx, timers, server, fetchImpl,
    setStatus(value) { status = value; }, setResult(value) { currentResult = value; },
    async result() { await ctx.submit('result'); },
    async join() { this.click('join'); await settle(3); const ws = FakeWebSocket.last(); assert.ok(ws); ws.serverOpen(); ws.serverSend({ type: 'ready' }); return ws; },
  };
}

test('u119 join-now uses server seconds/deadline, escapes reason and vibrates once per generation', async (t) => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'navigator'), vibrations = [];
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { vibrate: (n) => vibrations.push(n) } });
  t.after(() => { if (descriptor) Object.defineProperty(globalThis, 'navigator', descriptor); else delete globalThis.navigator; });
  const p = await page(t);
  assert.match(p.text('handoffReady'), /Someone picked up — join now/);
  assert.match(p.text('handoffReady'), /73 seconds/);
  assert.match(p.nodes.get('handoffReady').innerHTML, /Needs &lt;img src=x&gt;/);
  assert.match(p.nodes.get('handoffReady').innerHTML, /2026-10-20T12:01:30Z/);
  p.setStatus({ ...initial, handoff: { ...initial.handoff, remaining_s: 72 } }); await p.timers.runNext();
  assert.match(p.text('handoffReady'), /72 seconds/); assert.deepEqual(vibrations, [200]);
  p.setStatus({ ...initial, handoff: null }); await p.timers.runNext();
  p.setStatus(initial); await p.timers.runNext(); assert.deepEqual(vibrations, [200]);
  p.setStatus({ ...initial, handoff: { ...initial.handoff, generation: 2, remaining_s: 0 } }); await p.timers.runNext();
  assert.deepEqual(vibrations, [200, 200]);
  assert.ok(!p.server.toolCalls().includes('hangup_call'), 'only the server enforces expiry');
  assert.equal(p.button('join').disabled, true);
});

test('u119 foreground resumes polling and offers reconnect with authoritative grace seconds', async (t) => {
  const p = await page(t); await p.join();
  p.doc.hidden = true; p.doc.dispatchEvent(new Event('visibilitychange')); await settle(3);
  assert.equal(p.ctx.handoff, null); assert.equal(p.timers.pending.size, 0);
  p.setStatus({ ...initial, handoff: { ...initial.handoff, generation: 2, remaining_s: 51 } });
  p.doc.hidden = false; p.doc.dispatchEvent(new Event('visibilitychange')); await settle(3);
  assert.ok(p.timers.pending.size > 0);
  assert.match(p.text('handoffReady'), /Reconnect within 51 seconds/);
  assert.match(p.text('handoffReady'), /Rejoin call/);
  await p.join(); assert.ok(p.ctx.handoff);
});

test('u119 unexpected WS close clears the dead handle and permits Join again', async (t) => {
  const p = await page(t), ws = await p.join();
  ws.serverClose(1006); await settle(3);
  assert.equal(p.ctx.handoff, null);
  p.setStatus({ ...initial, handoff: { ...initial.handoff, generation: 2, remaining_s: 44 } }); await p.timers.runNext();
  assert.match(p.text('handoffReady'), /44 seconds/); await p.join();
  assert.equal(p.server.toolCalls().filter((n) => n === 'join_call').length, 2);
});

test('u119 live uses Hang up; text Return and privacy/keypad remain', async (t) => {
  const p = await page(t); await p.join();
  assert.ok(p.button('hangup')); assert.equal(p.button('return'), undefined);
  assert.equal(p.nodes.get('callView').controls.filter((c) => c.dataset.action === 'key').length, 12);
  p.click('hangup'); await settle(3);
  assert.equal(p.ctx.handoff, null); assert.ok(p.server.toolCalls().includes('hangup_call'));
  assert.ok(!p.server.toolCalls().includes('return_to_agent'));
  const text = renderHandoffCard(joinOut(), { mode: 'text-sim' });
  assert.match(text, /Return to Waitless/); assert.doesNotMatch(text, />Hang up</);
});

for (const failure of ['remote', 'transport']) test(`u119 ${failure} hangup remains retryable after local audio stops`, async (t) => {
  let count = 0;
  const p = await page(t, { hangup: () => {
    count += 1;
    if (failure === 'transport' && count === 1) throw new Error('Offline');
    return { status: count === 1 ? 'hung_up' : 'already_ended', call_id: 'C-1', remote_hangup_pending: count === 1 };
  } });
  await p.join(); p.click('hangup'); await settle(4);
  assert.equal(p.ctx.handoff, null); assert.match(p.text('hangupNotice'), /Retry hang up/);
  if (failure === 'remote') assert.match(p.text('hangupNotice'), /Remote hangup pending/);
  p.click('hangup'); await settle(4); assert.equal(count, 2);
  assert.equal(p.nodes.has('hangupNotice'), false);
});

for (const name of ['hangup_call', 'report_call_result']) test(`u119 assistant cannot automatically invoke ${name}`, async () => {
  const calls = [], waiting = [];
  await runAssistantStep(turnOut({ kind: 'tool_call', name, tool_use_id: 'blocked', arguments: {} }), {
    mcp: { callTool: async (...a) => { calls.push(a); return {}; } },
    postTurn: async () => turnOut({ kind: 'done' }), hooks: { onAwaitUser: (ui) => waiting.push(ui) },
  });
  assert.deepEqual(calls, []); assert.equal(waiting.length, 1);
});

test('u119 result form offers exactly three outcomes and an optional number bound to latest call', () => {
  const html = renderResult(awaiting()), inputs = startTags(html, 'input');
  assert.deepEqual(inputs.filter((c) => c.attrs.get('type') === 'radio').map((c) => c.attrs.get('value')), ['confirmed', 'failed', 'needs_follow_up']);
  assert.match(textOf(html), /Did it get done\?.*Done.*Not done.*Needs follow-up/);
  assert.match(html, /data-call-id="C-1"/);
  assert.ok(inputs.some((c) => c.attrs.get('id') === 'resultConfirmation' && !c.attrs.has('required')));
});

for (const outcome of ['confirmed', 'failed', 'needs_follow_up']) test(`u119 reports ${outcome} with explicit task/call binding`, async (t) => {
  const p = await page(t); await p.result(); p.choose(outcome); p.number('LE-48291');
  p.click('report-result'); await settle(4);
  const call = p.server.requests.find((r) => r.params?.name === 'report_call_result');
  assert.deepEqual(call?.params.arguments, { task_id: 'T-100', call_id: 'C-1', outcome, confirmation_number: 'LE-48291' });
  assert.match(p.text('resultCard'), /User reported/); assert.equal(p.button('report-result'), undefined);
});

test('u119 a user may report done without a confirmation number', async (t) => {
  const p = await page(t); await p.result(); p.choose('confirmed'); p.click('report-result'); await settle(4);
  const args = p.server.requests.find((r) => r.params?.name === 'report_call_result')?.params.arguments;
  assert.equal(args?.outcome, 'confirmed'); assert.ok(!args.confirmation_number);
  assert.match(p.text('resultCard'), /User reported/);
});

test('u119 submission is disabled while pending and double clicks submit once', async (t) => {
  const pending = deferred(); t.after(() => pending.resolve(reported()));
  const p = await page(t, { report: () => pending.promise }); await p.result(); p.choose('confirmed');
  p.click('report-result'); await settle(3);
  assert.match(p.text('resultCard'), /Submitting/); assert.equal(p.button('report-result').disabled, true);
  p.click('report-result'); assert.equal(p.server.toolCalls().filter((n) => n === 'report_call_result').length, 1);
  pending.resolve(reported()); await settle(4); assert.match(p.text('resultCard'), /User reported/);
});

test('u119 invalid company number format is shown safely and can be corrected', async (t) => {
  let count = 0;
  const p = await page(t, { report: (args) => ++count === 1 ? { isError: true, code: 'invalid_option', message: 'Invalid <script>number</script>' } : reported(args.outcome, args.confirmation_number) });
  await p.result(); p.choose('confirmed'); p.number('bad-number'); p.click('report-result'); await settle(4);
  assert.match(p.text('resultCard'), /Check the confirmation number format/);
  assert.doesNotMatch(p.nodes.get('resultCard').innerHTML, /<script>/);
  p.number('LE-48291'); p.click('report-result'); await settle(4); assert.equal(count, 2);
});

test('u119 result_conflict refreshes the stored result without overwriting it', async (t) => {
  const p = await page(t, { report: () => ({ isError: true, code: 'result_conflict', message: 'Conflict' }) });
  await p.result(); p.choose('failed'); p.setResult({ ...awaiting(), ...reported('confirmed'), calls: awaiting().calls });
  p.click('report-result'); await settle(5);
  assert.match(p.text('resultCard'), /already submitted|changed/);
  assert.match(p.text('resultCard'), /User reported/); assert.equal(p.button('report-result'), undefined);
});

test('u119 stale result button cannot report for another call or task', async (t) => {
  const p = await page(t); await p.result(); p.choose('confirmed'); const stale = p.button('report-result');
  p.setResult({ ...awaiting(), calls: [{ ...awaiting().calls[0], call_id: 'C-2' }] }); await p.result();
  p.dispatch('click', stale); await settle(3);
  assert.ok(!p.server.toolCalls().includes('report_call_result'));
});

test('u119 result form is restored from server after a page refresh without storing answers', async (t) => {
  const data = new Map(), storage = { getItem: (k) => data.get(k) ?? null, setItem: (k, v) => data.set(k, v) };
  const p = await page(t, { storage }); await p.result(); p.choose('failed'); p.number('LE-48291');
  assert.ok([...data.values()].includes('T-100')); assert.ok(!JSON.stringify([...data]).includes('LE-48291'));
  await p.ctx.dispose();
  const q = await page(t, { storage, restore: true }); await settle(4);
  assert.equal(q.ctx.state.task_id, 'T-100');
  q.setStatus({ ...initial, phase: 'ended', ended: true, handoff: null }); await q.timers.runNext(); await settle(4);
  assert.match(q.text('resultCard'), /Did it get done\?/);
  assert.match(q.nodes.get('resultCard').innerHTML, /data-call-id="C-1"/);
});

test('u119 sources label user reports and user numbers separately from company transcript', () => {
  const user = textOf(renderResult({ ...caseResult(), result_source: 'user', confirmation_source: 'user' }));
  assert.match(user, /User reported/); assert.match(user, /Confirmation \(user entered\)/);
  assert.doesNotMatch(user, /Company confirmed|Confirmed/);
  const mixed = textOf(renderResult({ ...caseResult(), result_source: 'transcript', confirmation_source: 'user' }));
  assert.match(mixed, /Confirmed/); assert.match(mixed, /Confirmation \(user entered\)/);
});

test('u119 DTMF key values never enter DOM or console, including hostile acknowledgements', async (t) => {
  const logs = [], original = { log: console.log, info: console.info, debug: console.debug };
  for (const name of Object.keys(original)) console[name] = (...args) => logs.push(args);
  t.after(() => Object.assign(console, original));
  const p = await page(t), ws = await p.join();
  assert.equal(p.ctx.handoff.sendKeys('86753091'), true);
  ws.serverSend({ type: 'dtmf_ack', count: 8, keys: '86753091' });
  ws.serverSend({ type: 'dtmf_error', error: '86753091' });
  assert.doesNotMatch([...p.nodes.values()].map((n) => n.innerHTML + n.textContent).join(' '), /86753091/);
  assert.doesNotMatch(JSON.stringify(logs), /86753091/);
});

test('u119 microphone permission and expiry races never issue a browser hangup', async (t) => {
  const p = await page(t), pending = deferred(), original = p.ctx.mediaDevices.getUserMedia.bind(p.ctx.mediaDevices);
  p.ctx.mediaDevices.getUserMedia = async (...args) => { await pending.promise; return original(...args); };
  const joining = app.onJoinCall(p.ctx); await settle(2);
  p.setStatus({ ...initial, phase: 'ended', ended: true, handoff: null }); await p.timers.runNext();
  pending.resolve(); await joining;
  assert.equal(p.ctx.handoff, null); assert.ok(p.ctx.mediaDevices.allTracksStopped());
  assert.ok(!p.server.toolCalls().includes('hangup_call'));
});

test('u119 hangup handler sends only the authorized tool and tolerates no active call', async () => {
  const mcp = recordingMcp({ hangup_call: { status: 'no_active_call', call_id: null, remote_hangup_pending: false } });
  assert.equal(typeof app.onHangup, 'function');
  await app.onHangup({ mcp, state: { task_id: 'T-100' } });
  assert.deepEqual(mcp.calls, [{ name: 'hangup_call', args: { task_id: 'T-100' } }]);
});
