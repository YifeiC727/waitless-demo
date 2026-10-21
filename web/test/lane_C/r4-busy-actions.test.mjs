import test from 'node:test';
import assert from 'node:assert/strict';
import { boot } from '../../js/app.mjs';
import { USER_ACTION_TOOLS } from '../../js/assistant.mjs';
import { createFakeFetch, createFakeMcpServer, FakeWebSocket } from '../helpers.mjs';
import { handoffEnv, joinOut, manualTimers, pendingChoice, settle, startTags, taskCard, turnOut } from '../c-support.mjs';

// Keep actual rendered controls, their disabled properties and connection lifetime.
// Earlier lane DOM stand-ins return no controls from querySelectorAll.
function dom() {
  const doc = new EventTarget(), nodes = new Map();
  function node(id) {
    if (nodes.has(id)) return nodes.get(id);
    const out = new EventTarget();
    let html = '';
    Object.assign(out, { hidden: false, value: '', controls: [], isConnected: true,
      remove() { this.isConnected = false; this.controls.forEach((c) => { c.isConnected = false; }); nodes.delete(id); },
      insertAdjacentHTML(_position, text) { html += text; parse(text); },
    });
    function parse(text) {
      const rest = text.replace(/^<div id="([^"]+)" class="tool-card">([\s\S]*)<\/div>$/g,
        (_all, slot, content) => { node(slot).innerHTML = content; return ''; });
      for (const { tag, attrs } of startTags(rest, 'button|input')) {
        const control = { tag, attrs, disabled: attrs.has('disabled'), checked: attrs.has('checked'),
          value: attrs.get('value') ?? '', isConnected: true,
          dataset: { action: attrs.get('data-action'), questionId: attrs.get('data-question-id') },
          closest: () => control,
          matches: (selector) => selector === '#pendingCard input[type="radio"]' && id === 'pendingCard' && tag === 'input',
        };
        out.controls.push(control);
      }
    }
    Object.defineProperty(out, 'innerHTML', { get: () => html, set(text) {
      out.controls.forEach((c) => { c.isConnected = false; });
      out.controls = []; html = text; parse(text);
    } });
    nodes.set(id, out);
    return out;
  }
  for (const id of ['messages', 'callView', 'chatView', 'error', 'prompt', 'composer', 'hero']) node(id);
  nodes.get('callView').hidden = true;
  doc.querySelectorAll = () => [...nodes.values()].flatMap((n) => n.controls);
  doc.querySelector = (selector) => {
    const [id, child] = selector.slice(1).split(' ');
    const parent = nodes.get(id);
    return child ? parent?.controls.find((c) => c.dataset.action === child.match(/data-action="([^"]+)"/)?.[1]) : parent;
  };
  doc.defaultView = new EventTarget();
  function dispatch(type, control) {
    assert.ok(control, `rendered control for ${type} exists`);
    if (control.disabled) return;
    const event = new Event(type);
    Object.defineProperty(event, 'target', { value: control });
    doc.dispatchEvent(event);
  }
  return { doc, nodes, button: (action) => doc.querySelectorAll().find((c) => c.dataset.action === action),
    click(action) { dispatch('click', this.button(action)); },
    choose() { const input = nodes.get('pendingCard').controls.find((c) => c.tag === 'input'); input.checked = true; dispatch('change', input); },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

const taskId = 'T-busy-r4';
const running = { task_id: taskId, call_id: 'C-busy-r4', phase: 'ivr', ended: false, transcript_tail: [], handoff: null };
const ready = { ...running, phase: 'handoff_ready', pending_choice: pendingChoice(),
  handoff: { state: 'ready', company_name: 'Lakeshore Energy', media: joinOut().media } };

async function page(t, assistant, card = taskCard()) {
  const timers = manualTimers();
  const originalSet = globalThis.setTimeout, originalClear = globalThis.clearTimeout;
  globalThis.setTimeout = timers.setTimer;
  globalThis.clearTimeout = timers.clearTimer;
  const d = dom();
  d.nodes.get('messages').insertAdjacentHTML('beforeend', '<button data-action="start">Start call</button>');
  let status = running, ctx;
  const server = createFakeMcpServer({ results: {
    start_assisted_call: { task_id: taskId, call_id: running.call_id, status: 'dialing' },
    get_call_status: () => status,
    join_call: joinOut({ task_id: taskId }),
    return_to_agent: { task_id: taskId, status: 'resuming' },
    answer_pending_choice: { task_id: taskId, status: 'choice_recorded' },
    prepare_task: { task_id: taskId, status: 'ready', task_card: card, missing: [] },
  } });
  const fetchImpl = createFakeFetch({ mcp: server, routes: {
    'GET /app-config': { mode: 'text-sim', poll_ms: 777 },
    'POST /assistant/turn': assistant,
  } });
  t.after(async () => {
    try { await ctx?.dispose(); }
    finally { globalThis.setTimeout = originalSet; globalThis.clearTimeout = originalClear; }
  });
  ctx = await boot({ document: d.doc, fetchImpl, ...handoffEnv() });
  Object.assign(ctx.state, { task_id: taskId, card_version: 2 });
  return { ...d, ctx, timers, server, fetchImpl, setStatus(value) { status = value; },
    userCalls() { return server.toolCalls().filter((name) => USER_ACTION_TOOLS.includes(name)); } };
}

test('I1-6 newly polled Join is disabled during an assistant turn, then one click joins once', async (t) => {
  const pending = deferred();
  const p = await page(t, () => pending.promise);
  t.after(() => pending.resolve(turnOut({ kind: 'done' })));
  p.click('start');
  await settle(3);
  p.setStatus(ready);
  await p.timers.runNext();
  assert.equal(p.button('join').disabled, true, 'new Join must visibly reject clicks while busy');
  p.click('join');
  assert.deepEqual(p.userCalls(), ['start_assisted_call']);
  pending.resolve(turnOut({ kind: 'tool_call', name: 'join_call', tool_use_id: 'blocked', arguments: { task_id: taskId } }));
  await settle(3);
  assert.equal(p.button('join').disabled, false);
  assert.deepEqual(p.userCalls(), ['start_assisted_call'], 'assistant cannot join on its own');
  p.click('join');
  await settle(3);
  assert.deepEqual(p.userCalls(), ['start_assisted_call', 'join_call']);
  const ws = FakeWebSocket.last();
  ws.serverOpen();
  ws.serverSend({ type: 'ready', company_name: ready.handoff.company_name, media: ready.handoff.media });
  await settle(3);
  assert.equal(p.ctx.mediaDevices.calls.length, 1);
  assert.equal(p.button('return').disabled, false);
});

test('I1-6 Return rendered while Join audio is connecting remains disabled until Join finishes', async (t) => {
  const p = await page(t, () => turnOut({ kind: 'done' }));
  const permission = deferred();
  const originalGetUserMedia = p.ctx.mediaDevices.getUserMedia.bind(p.ctx.mediaDevices);
  p.ctx.mediaDevices.getUserMedia = async (options) => { await permission.promise; return originalGetUserMedia(options); };
  t.after(async () => { permission.resolve(); await settle(3); });
  p.click('start');
  await settle(3);
  p.setStatus(ready);
  await p.timers.runNext();
  p.click('join');
  await settle(3);
  assert.equal(p.button('return').disabled, true, 'new Return cannot look clickable while join is busy');
  assert.deepEqual(p.userCalls(), ['start_assisted_call', 'join_call']);
  permission.resolve();
  await settle(3);
  const ws = FakeWebSocket.last();
  ws.serverOpen();
  ws.serverSend({ type: 'ready', company_name: ready.handoff.company_name, media: ready.handoff.media });
  await settle(3);
  assert.equal(p.button('return').disabled, false);
  p.click('return');
  await settle(3);
  assert.deepEqual(p.userCalls(), ['start_assisted_call', 'join_call', 'return_to_agent']);
});

for (const fails of [false, true]) {
  for (const replaced of [false, true]) {
  test(`I1-6 pending selection gating after assistant ${fails ? 'failure' : 'success'} (replaced=${replaced})`, async (t) => {
    const pending = deferred();
    const p = await page(t, ({ body }) => body.user_text ? pending.promise : turnOut({ kind: 'done' }));
    t.after(() => pending.resolve(turnOut({ kind: 'done' })));
    p.click('start');
    await settle(3);
    p.setStatus(ready);
    await p.timers.runNext();
    p.choose();
    const oldConfirm = p.button('confirm');
    assert.equal(oldConfirm.disabled, false);
    const submission = p.ctx.submit('Check the call');
    await settle(3);
    assert.equal(oldConfirm.disabled, true);
    if (replaced) p.setStatus({ ...ready, pending_choice: pendingChoice({ choice_id: 'pc-replacement' }) });
    await p.timers.runNext();
    assert.equal(oldConfirm.isConnected, !replaced);
    assert.equal(p.nodes.get('pendingCard').controls.find((c) => c.tag === 'input').disabled, true);
    assert.equal(p.button('confirm').disabled, true);
    pending.resolve(fails ? new Error('Assistant unavailable') : turnOut({ kind: 'done' }));
    await submission;
    assert.equal(p.button('join').disabled, false);
    assert.equal(p.button('confirm').disabled, replaced, 'restore existing selection; replacement stays unselected');
    assert.equal(p.nodes.get('pendingCard').controls.find((c) => c.tag === 'input').disabled, false);
    assert.equal(p.nodes.get('error').hidden, !fails);
    assert.deepEqual(p.userCalls(), ['start_assisted_call']);
    p.choose();
    assert.equal(p.button('confirm').disabled, false);
    p.click('confirm');
    await settle(3);
    assert.deepEqual(p.userCalls(), ['start_assisted_call', 'answer_pending_choice']);
  });
  }
}

for (const needsUser of [false, true]) {
  test(`I1-6 task card arriving mid-turn restores Start eligibility (needs_user=${needsUser})`, async (t) => {
    const pending = deferred();
    const p = await page(t, ({ body }) => body.tool_result ? pending.promise : turnOut({ kind: 'tool_call',
      name: 'prepare_task', tool_use_id: 'prepare-r4', arguments: { request: 'Start service' } }), taskCard({
        precall_decisions: [{ question_id: 'approval', question: 'Approve?', options: [], selected: null, needs_user: needsUser }],
      }));
    t.after(() => pending.resolve(turnOut({ kind: 'done' })));
    const submission = p.ctx.submit('Start service');
    await settle(3);
    const start = p.nodes.get('taskCard').controls.find((c) => c.dataset.action === 'start');
    assert.equal(start.disabled, true, 'new Start must be disabled until the turn ends');
    pending.resolve(turnOut({ kind: 'done' }));
    await submission;
    assert.equal(start.disabled, needsUser, 'restore eligibility, not unconditional enablement');
    assert.deepEqual(p.userCalls(), []);
  });
}
