import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { elements, textOf } from '../c-support.mjs';

const root = new URL('../../../', import.meta.url);
const pages = ['docs/index.html', 'Waitless-Alexa-MCP-Demo.html'];
const source = file => readFileSync(new URL(file, root), 'utf8');
const serverSource = source('waitless-mcp-demo-server.mjs');
const server = vm.createContext({});
// Evaluate only the pure dispatcher: no imports, HTTP server, or listening socket.
vm.runInContext(serverSource.slice(serverSource.indexOf('function resultFor('), serverSource.indexOf('\nconst server =')), server);
const resultFor = (name, args = {}) => server.resultFor(name, { scenario: 'electric_service_demo', ...args });
const expected = holder => holder === 'agree'
  ? { status: 'resolved', deposit: 'agreed_not_paid', confirmation: 'LE-48291' }
  : holder === 'decline'
    ? { status: 'needs_follow_up', deposit: 'declined' }
    : { status: 'pending', deposit: 'pending_account_holder' };

function assertResult(result, holder) {
  const wanted = expected(holder);
  assert.equal(result.status, wanted.status);
  assert.equal(result.deposit, wanted.deposit);
  assert.equal(Object.hasOwn(result, 'confirmation'), Object.hasOwn(wanted, 'confirmation'));
  assert.equal(result.confirmation, wanted.confirmation);
  assert.doesNotMatch(JSON.stringify(result), /waived/i);
}

function lineFor(html, marker) {
  const line = html.split('\n').find(line => line.includes(marker));
  assert.ok(line, `page contains ${marker}`);
  return line;
}

function pageVM(file, { holder, selected = null, hosted = false, response } = {}) {
  const html = source(file), renders = [], events = [], toasts = [], calls = [], companion = [];
  const nodes = new Map();
  const context = vm.createContext({
    state: { caseId: 'electricity', task: 'WL-2048', selected, holder_deposit: holder, resolved: false, logs: [], requestId: 0 },
    invalidProfile: () => [], maskedPhone: () => 'masked', escapeHtml: String,
    $: selector => {
      if (!nodes.has(selector)) nodes.set(selector, { textContent: '', classList: { add() {}, remove() {}, toggle() {} } });
      return nodes.get(selector);
    },
    addEvent: (...args) => events.push(args), renderTask: (...args) => renders.push(args),
    toast: message => toasts.push(message), showComp: html => companion.push(html),
    stopCallClock() {}, setInterval() {}, icon: () => '', backAlexa() {}, pause: async () => {},
    location: { hostname: '' }, renderTrace() {},
    fetch: () => { throw new Error('tests must never access the network'); },
  });
  vm.runInContext('window=globalThis; let flowVersion=0; let callClock;', context);
  vm.runInContext(html.slice(html.indexOf('  const CASES='), html.indexOf('  const state=')), context);
  vm.runInContext('const currentCase=()=>CASES[state.caseId];', context);
  for (const marker of ['function caseOutcome()', 'const localResult=', 'function log(', 'async function rpc(', 'function approvalContent()', 'window.answerBridge=', 'window.demoTone=', 'window.finishHandoff=', 'window.resolveCase=']) {
    vm.runInContext(lineFor(html, marker), context);
  }
  context.callTool = async (name, args) => {
    calls.push({ name, args });
    if (hosted) return { structuredContent: response ?? resultFor(name, args) };
    return context.rpc('tools/call', { name, arguments: { ...args, scenario: 'electric_service_demo' } });
  };
  return { context, renders, events, toasts, calls, companion, nodes, eval: code => vm.runInContext(code, context) };
}

test('u112_c_1_server_three_states', () => {
  for (const holder of ['agree', 'decline', undefined, null, 'other']) {
    for (const approved_change of [undefined, 'deposit', 'autopay', 'approve', 'ask_me']) {
      assertResult(resultFor('get_case_result', { holder_deposit: holder, approved_change }), holder);
    }
  }
});

test('u112_c_2_server_no_autopay_default', () => {
  assert.equal(resultFor('get_call_status').action, 'choose_deposit_consent');
  assert.equal(resultFor('answer_pending_choice').answer, null);
  for (const answer of ['approve', 'ask_me']) assert.equal(resultFor('answer_pending_choice', { answer }).answer, answer);
});

test('u112_c_3_pages_fallback_matches_server', () => {
  for (const file of pages) {
    const page = pageVM(file);
    assert.equal(page.eval("localResult('get_call_status').action"), 'choose_deposit_consent');
    assert.equal(page.eval("localResult('answer_pending_choice').answer"), null);
    for (const holder of ['agree', 'decline', undefined, 'other']) {
      for (const approved_change of ['deposit', 'autopay', 'approve']) {
        page.context.args = { holder_deposit: holder, approved_change };
        const local = page.eval("localResult('get_case_result', args)");
        const remote = resultFor('get_case_result', page.context.args);
        assertResult(local, holder);
        assert.equal(local.status, remote.status);
        assert.equal(local.confirmation, remote.confirmation);
      }
    }
  }
});

test('u112_c_4_resolve_case_only_confirms_on_agree', async () => {
  for (const file of pages) for (const hosted of [true, false]) {
    for (const holder of ['agree', 'decline', undefined]) for (const selected of ['approve', 'ask_me', null]) {
      const page = pageVM(file, { holder, selected, hosted });
      await page.context.resolveCase();
      const visible = textOf(page.renders.flat().join(' '));
      const confirmations = page.events.filter(([title]) => title === 'Confirmation received');
      assert.equal(page.context.state.resolved, holder === 'agree', `${file} hosted=${hosted} holder=${holder} selected=${selected}`);
      assert.equal(page.calls[0].args.holder_deposit, holder);
      assert.equal(confirmations.length, holder === 'agree' ? 1 : 0);
      if (holder === 'agree') {
        assert.match(visible, /LE-48291/);
        assert.equal(confirmations[0][1], 'LE-48291');
      } else {
        assert.doesNotMatch(visible, /LE-/);
        assert.ok(visible.includes(holder === 'decline' ? 'Deposit not approved' : 'Waiting for the account holder to decide on the deposit'));
        if (holder === 'decline') assert.ok(visible.includes('The account holder can call back to continue.'));
      }
      assert.equal(page.context.state.resolving, false);
    }
    // A local agreement must not override an incomplete or negative provider response.
    for (const response of [{ status: 'resolved' }, { status: 'pending', confirmation: 'LE-48291' }, { status: 'needs_follow_up', confirmation: 'LE-48291' }]) {
      const page = pageVM(file, { holder: 'agree', hosted: true, response });
      await page.context.resolveCase();
      assert.equal(page.context.state.resolved, false);
      assert.doesNotMatch(textOf(page.renders.flat().join(' ')), /LE-/);
      assert.equal(page.events.filter(([title]) => title === 'Confirmation received').length, 0);
    }
    // Render the confirmation supplied by the response, rather than the case's sample number.
    const page = pageVM(file, { holder: 'agree', hosted: true, response: { status: 'resolved', confirmation: 'PROVIDER-42' } });
    await page.context.resolveCase();
    assert.match(textOf(page.renders.flat().join(' ')), /PROVIDER-42/);
    assert.doesNotMatch(textOf(page.renders.flat().join(' ')), /LE-48291/);
  }
});

test('u112_c_5_no_autopay_waiver_text', () => {
  const markers = ['electricity:{', 'function caseOutcome()', 'const localResult=', 'function approvalContent()', 'window.answerBridge=', 'window.demoTone=', 'window.finishHandoff=', 'window.resolveCase='];
  const snippets = pages.map(file => markers.map(marker => lineFor(source(file), marker)).join('\n'));
  assert.equal(snippets[0], snippets[1], 'electricity fragments match byte for byte');
  for (const file of [...pages, 'waitless-mcp-demo-server.mjs']) assert.doesNotMatch(source(file), /Deposit waived/i);
  for (const snippet of snippets) assert.doesNotMatch(snippet, /autopay/i);
  const electricityServer = serverSource.slice(serverSource.indexOf('  if (args?.scenario === "electric_service_demo")'), serverSource.indexOf('  const base = { demo: true, task_id: args?.task_id || "WL-2049"'));
  assert.doesNotMatch(electricityServer, /autopay/i);
  for (const file of pages) {
    const page = pageVM(file);
    assert.equal(page.eval('currentCase().approvalTitle'), 'A deposit may be required');
    assert.deepEqual(JSON.parse(page.eval('JSON.stringify(currentCase().options.map(o=>[o.id,o.title,o.detail]))')), [
      ['approve', 'Yes, I am willing to pay a refundable deposit', 'You still confirm it yourself on the call'],
      ['ask_me', 'No, I will decide on the call', 'Waitless does not agree to a deposit for you'],
    ]);
    assert.doesNotMatch(page.eval('approvalContent()'), /\bchecked\b/);
  }
});

test('u112_c_6_keypad_choice_not_shown', async () => {
  for (const file of pages) for (const [key, holder] of [['1', 'agree'], ['2', 'decline']]) {
    const page = pageVM(file);
    page.context.state.holder_deposit = 'agree';
    page.context.answerBridge();
    assert.equal(page.context.state.holder_deposit, null, 'a new bridge starts with no deposit answer');
    const initial = page.companion.join('');
    assert.match(textOf(initial), /Press 1 to agree to the deposit, 2 to decline/);
    assert.match(initial, /demoTone\(/);
    const button = elements(initial, 'button').find(button => button.text === key);
    assert.ok(button, 'the keypad contains the requested button');
    page.context.pressedButton = { textContent: key };
    page.eval(`(function(){${button.attrs.get('onclick')}}).call(pressedButton)`);
    assert.equal(page.context.state.holder_deposit, holder);
    assert.equal(page.companion.join(''), initial, 'keypad labels and instructions do not echo the pressed key');
    assert.doesNotMatch(JSON.stringify([...page.toasts, ...page.events]), /[12]/, 'new visible messages and activity contain no key value');
    assert.equal(page.context.state.logs.length, 0, 'keypad does not write a protocol trace');
    page.context.demoTone('9');
    assert.equal(page.context.state.holder_deposit, holder, 'other keys do not change consent');
    assert.equal(page.toasts.at(-1), 'Demo tone · no data sent');
    await page.context.finishHandoff();
    const visible = textOf(page.renders.flat().join(' '));
    assert.equal(page.context.state.resolved, holder === 'agree');
    if (holder === 'decline') assert.doesNotMatch(visible, /LE-/);
    for (const { payload } of page.context.state.logs) {
      assert.doesNotMatch(JSON.stringify(payload), /"(?:keys?|digits|dtmf)"\s*:/i, 'protocol trace has no raw keypad field');
    }
  }
});
