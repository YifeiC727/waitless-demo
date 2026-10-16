import test, { beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { boot } from '../../js/app.mjs';
import { renderHandoffCard } from '../../js/cards.mjs';
import { joinHandoffAudio } from '../../js/handoff-audio.mjs';
import { createFakeFetch, createFakeMcpServer, FakeWebSocket } from '../helpers.mjs';
import { handoffEnv, joinOut, recordingHandle, manualTimers, settle, elements, startTags, textOf, buttonsByText, turnOut } from '../c-support.mjs';

beforeEach((t) => {
  const original = { fetch: globalThis.fetch, WebSocket: globalThis.WebSocket };
  globalThis.fetch = () => { throw new Error('Unexpected network fetch'); };
  globalThis.WebSocket = class { constructor() { throw new Error('Unexpected network socket'); } };
  t.after(() => Object.assign(globalThis, original));
});

// Literal output captured from the task's starting version, including whitespace.
const originalCard = `<section class="handoff-card"><h2>Call with Lakeshore Energy</h2>
    <p>AI is not listening</p><p>Not recording</p><p>Not transcribing</p>
    <button type="button" data-action="mute" aria-pressed="false">Mute</button>
    <button type="button" data-action="return">Return to Waitless</button></section>`;
const keyButtons = (html) => elements(html, 'button').filter((b) => b.attrs.get('data-action') === 'key');

// Local DOM with rendered button lifetimes and a textContent feedback node.
// Uses the frozen parsers, without changing either shared test helper.
function dom() {
  const doc = new EventTarget(), nodes = new Map();
  function node(id) {
    if (nodes.has(id)) return nodes.get(id);
    const out = new EventTarget();
    let html = '';
    Object.assign(out, { hidden: false, value: '', controls: [], isConnected: true,
      remove() { this.isConnected = false; nodes.delete(id); },
      insertAdjacentHTML(_position, text) { html += text; parse(text); },
      querySelector(selector) { return selector === '.keypad-feedback' ? this.feedback : null; },
    });
    function parse(text) {
      const rest = text.replace(/^<div id="([^"]+)" class="tool-card">([\s\S]*)<\/div>$/g,
        (_all, slot, content) => { node(slot).innerHTML = content; return ''; });
      for (const { attrs } of startTags(rest, 'button')) {
        const control = { disabled: attrs.has('disabled'), isConnected: true,
          dataset: { action: attrs.get('data-action'), key: attrs.get('data-key') },
          closest: () => control,
        };
        out.controls.push(control);
      }
      if (/class="keypad-feedback"/.test(rest)) {
        out.feedback = {};
        Object.defineProperty(out.feedback, 'textContent', {
          get: () => elements(html, 'p').find((p) => p.attrs.get('class') === 'keypad-feedback')?.text ?? '',
          set(value) { html = html.replace(/(<p class="keypad-feedback"[^>]*>)[\s\S]*?(<\/p>)/, (_m, a, b) => a + value + b); },
        });
      }
    }
    Object.defineProperty(out, 'innerHTML', { get: () => html, set(text) {
      out.controls.forEach((c) => { c.isConnected = false; });
      out.controls = []; out.feedback = null; html = text; parse(text);
    } });
    nodes.set(id, out);
    return out;
  }
  for (const id of ['messages', 'callView', 'chatView', 'traceView', 'error', 'prompt', 'composer', 'hero', 'mode']) node(id);
  nodes.get('callView').hidden = true;
  nodes.get('error').hidden = true;
  doc.querySelectorAll = () => [...nodes.values()].flatMap((n) => n.controls);
  doc.querySelector = (selector) => {
    if (selector === '.keypad-feedback') return nodes.get('callView').feedback;
    const [id, child] = selector.slice(1).split(' ');
    return child ? nodes.get(id)?.querySelector(child) : nodes.get(id);
  };
  doc.defaultView = new EventTarget();
  function click(button) {
    assert.ok(button, 'rendered button exists');
    const event = new Event('click');
    Object.defineProperty(event, 'target', { value: button });
    doc.dispatchEvent(event);
  }
  return { doc, nodes, click,
    button(action, key) { return doc.querySelectorAll().find((b) => b.dataset.action === action && (key === undefined || b.dataset.key === key)); },
    text() { return textOf([...nodes.values()].map((n) => n.innerHTML).join(' ')); },
  };
}

const pageCleanup = Symbol('pageCleanup');
const readyStatus = { task_id: 'T-100', phase: 'handoff_ready', ended: false, status_line: 'Ready', transcript_tail: [],
  handoff: { state: 'ready', company_name: 'Lakeshore Energy', media: joinOut().media } };
async function page(t, mode = 'live-sim') {
  const timers = manualTimers();
  if (!t[pageCleanup]) {
    const original = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
    const contexts = t[pageCleanup] = [];
    t.after(async () => { try { for (const c of contexts) await c.dispose(); } finally { Object.assign(globalThis, original); } });
  }
  globalThis.setTimeout = timers.setTimer; globalThis.clearTimeout = timers.clearTimer;
  let ctx, status = readyStatus;
  const d = dom();
  const server = createFakeMcpServer({ results: {
    start_assisted_call: { task_id: 'T-100', call_id: 'C-1', status: 'dialing' },
    get_call_status: () => status,
    join_call: joinOut(),
  } });
  const fetchImpl = createFakeFetch({ mcp: server, routes: {
    'GET /app-config': { mode, poll_ms: 777 },
    'POST /assistant/turn': () => turnOut({ kind: 'done' }),
  } });
  ctx = await boot({ document: d.doc, fetchImpl, ...handoffEnv() });
  t[pageCleanup].push(ctx);
  Object.assign(ctx.state, { task_id: 'T-100', card_version: 2 });
  // An existing task card's Start button begins real app polling.
  d.nodes.get('messages').insertAdjacentHTML('beforeend', '<button data-action="start">Start call</button>');
  d.click(d.button('start')); await settle(3); await timers.runNext();
  return { ...d, ctx, timers, setStatus(value) { status = value; },
    async join() {
      d.click(d.button('join')); await settle(3);
      assert.ok(ctx.handoff);
      const ws = FakeWebSocket.last(); ws.serverOpen(); ws.serverSend({ type: 'ready' });
      return ws;
    },
  };
}

test('u102_1_card_has_twelve_keys_only_with_keypad_option', () => {
  const join = joinOut();
  assert.equal(renderHandoffCard(join), originalCard);
  assert.equal(renderHandoffCard(join, {}), originalCard);
  const html = renderHandoffCard(join, { keypad: true });
  const keys = keyButtons(html);
  assert.deepEqual(keys.map((b) => b.attrs.get('data-key')), [...'123456789*0#']);
  assert.ok(keys.every((b) => b.attrs.get('type') === 'button'));
  assert.equal(elements(html, 'p').filter((p) => p.attrs.get('role') === 'status' && p.attrs.get('aria-live') === 'polite').length, 1);
  assert.ok(keys.at(-1).end < buttonsByText(html, /^Mute$/)[0].start);
});

test('u102_2_send_keys_only_when_ready', async (t) => {
  const handle = await joinHandoffAudio({ join: joinOut(), ...handoffEnv() });
  t.after(() => handle.stop());
  const ws = FakeWebSocket.last();
  assert.equal(handle.sendKeys('5'), false); assert.deepEqual(ws.sentText, []);
  ws.serverOpen();
  assert.equal(handle.sendKeys('5'), false); assert.equal(ws.sentText.length, 1);
  ws.serverSend({ type: 'ready' });
  assert.equal(handle.sendKeys('5'), true);
  assert.equal(ws.sentText.at(-1), '{"type":"dtmf","keys":"5"}');
  const count = ws.sentText.length;
  for (const bad of ['5a', '', '1'.repeat(17), 5, null, undefined, {}, ['5']]) assert.equal(handle.sendKeys(bad), false);
  assert.equal(ws.sentText.length, count);
  assert.equal(handle.sendKeys('*#01234567890123'), true);
  ws.readyState = FakeWebSocket.CLOSING;
  assert.equal(handle.sendKeys('5'), false);
  await handle.stop();
  const stoppedCount = ws.sentText.length;
  assert.equal(handle.sendKeys('5'), false); assert.equal(ws.sentText.length, stoppedCount);
});

test('u102_3_ack_and_error_reach_on_keys', async (t) => {
  const keys = [], ended = [];
  const handle = await joinHandoffAudio({ join: joinOut(), ...handoffEnv(), onKeys: (m) => keys.push(m), onEnded: (r) => ended.push(r) });
  t.after(() => handle.stop());
  const ws = FakeWebSocket.last(); ws.serverOpen(); ws.serverSend({ type: 'ready' });
  const ack = { type: 'dtmf_ack', count: 1 }, error = { type: 'dtmf_error', error: 'rate_limited' };
  ws.serverSend(ack); ws.serverSend(error);
  assert.deepEqual(keys, [ack, error]); assert.deepEqual(ended, []);
  assert.equal(ws.readyState, FakeWebSocket.OPEN);
  ws.serverSend({ type: 'ended', reason: 'finished' });
  assert.deepEqual(ended, ['finished']);
  ws.serverSend(ack); assert.equal(keys.length, 2);
});

test('u102_4_app_key_click_sends_and_feedback_has_no_digits', async (t) => {
  const p = await page(t), ws = await p.join();
  assert.equal(keyButtons(p.nodes.get('callView').innerHTML).length, 12);
  const real = p.ctx.handoff, calls = [], handle = recordingHandle();
  handle.stop = () => real.stop();
  handle.sendKeys = (key) => { calls.push(key); const sent = real.sendKeys(key); if (sent) ws.serverSend({ type: 'dtmf_ack', count: key.length }); return sent; };
  p.ctx.handoff = handle;
  for (const key of '86753091') p.click(p.button('key', key));
  assert.deepEqual(calls, [...'86753091'], 'rapid synchronous clicks must all bypass busy action');
  assert.equal(p.doc.querySelector('#callView .keypad-feedback').textContent, 'Sent 8 keys');
  assert.doesNotMatch(p.text(), /86753091|8675|3091/);
  const disabled = p.button('key', '5'); disabled.disabled = true; p.click(disabled);
  assert.equal(calls.length, 8); disabled.disabled = false;
  ws.serverSend({ type: 'dtmf_error', error: 'rate_limited' });
  assert.equal(p.doc.querySelector('#callView .keypad-feedback').textContent, 'Key not sent');
  handle.sendKeys = () => false; p.click(disabled);
  assert.equal(p.doc.querySelector('#callView .keypad-feedback').textContent, 'Key not sent');
  ws.serverSend({ type: 'dtmf_ack', count: 2 });
  assert.equal(p.doc.querySelector('#callView .keypad-feedback').textContent, 'Sent 10 keys');
});

test('u102_5_no_keypad_outside_live_sim', async (t) => {
  for (const mode of ['text-sim', 'replay']) {
    const p = await page(t, mode); await p.join();
    assert.equal(keyButtons(p.nodes.get('callView').innerHTML).length, 0);
    await p.ctx.dispose();
  }
});

test('u102_6_join_prompt_shows_reason_escaped_and_vibrates_once', async (t) => {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'navigator'), vibrations = [];
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { vibrate: (ms) => vibrations.push(ms) } });
  t.after(() => { if (original) Object.defineProperty(globalThis, 'navigator', original); else delete globalThis.navigator; });
  const p = await page(t);
  assert.deepEqual(vibrations, [200]);
  await p.timers.runNext(); assert.deepEqual(vibrations, [200]);
  p.setStatus({ ...readyStatus, handoff: null }); await p.timers.runNext();
  p.setStatus({ ...readyStatus, handoff: { ...readyStatus.handoff, reason: 'Needs <b>date</b> of birth' } });
  await p.timers.runNext();
  assert.match(p.nodes.get('handoffReady').innerHTML, /<p class="handoff-reason">Needs &lt;b&gt;date&lt;\/b&gt; of birth<\/p>/);
  await p.timers.runNext(); assert.deepEqual(vibrations, [200, 200]);
  for (const reason of [undefined, null, '']) {
    p.setStatus({ ...readyStatus, handoff: { ...readyStatus.handoff, reason } }); await p.timers.runNext();
    assert.doesNotMatch(p.nodes.get('handoffReady').innerHTML, /handoff-reason/);
  }
  delete globalThis.navigator;
  p.setStatus({ ...readyStatus, handoff: null }); await p.timers.runNext();
  p.setStatus(readyStatus); await p.timers.runNext();
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { vibrate() { throw new Error('Unavailable'); } } });
  p.setStatus({ ...readyStatus, handoff: { ...readyStatus.handoff, reason: 'New request' } }); await p.timers.runNext();
  assert.equal(p.nodes.get('error').hidden, true);
});

test('u102_7_keypad_goes_away_when_call_ends', async (t) => {
  const p = await page(t); await p.join();
  const old = p.button('key', '5'), real = p.ctx.handoff, handle = recordingHandle(), calls = [];
  handle.stop = () => real.stop(); handle.sendKeys = (key) => { calls.push(key); return true; };
  p.ctx.handoff = handle;
  p.setStatus({ ...readyStatus, ended: true, phase: 'ended', handoff: { state: 'ended' } });
  await p.timers.runNext();
  assert.equal(p.nodes.get('callView').hidden, true); assert.equal(p.ctx.handoff, null);
  if (old) p.click(old);
  assert.deepEqual(calls, []);
});
