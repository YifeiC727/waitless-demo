import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';

import { boot } from '../../js/app.mjs';
import { createFakeFetch, createFakeMcpServer } from '../helpers.mjs';
import { elements, handoffEnv, settle, taskCard, turnOut } from '../c-support.mjs';
import { documentHarness } from './s5-dom.mjs';

const bankRequest = 'Call Bluefin Harbor Bank about two $84.50 charges from Quillmark Grocers on October 8. I think one is a duplicate.';
const indexHtml = () => readFileSync(new URL('../../index.html', import.meta.url), 'utf8');

test('u100_1_two_real_call_suggestions', () => {
  const buttons = elements(indexHtml(), 'button').filter((button) => button.attrs.has('data-request'));
  assert.equal(buttons.length, 2);
  assert.deepEqual(buttons.map((button) => button.attrs.get('id')), ['electricSuggestion', 'bankSuggestion']);
  const [electric, bank] = buttons;
  assert.equal(electric.attrs.get('data-request'), 'Start electricity at my new apartment on November 1.');
  assert.equal(electric.inner, 'Start electricity at my new apartment<span>Your local electric utility</span>');
  assert.equal(bank.attrs.get('data-request'), bankRequest);
  assert.ok(bank.text.includes('Ask Bluefin Harbor Bank about a duplicate charge'));
  assert.deepEqual(elements(bank.inner, 'span').map((span) => span.text), ['Simulated bank line']);
  assert.equal(bank.attrs.get('type'), electric.attrs.get('type'));
  assert.equal(bank.attrs.get('class'), electric.attrs.get('class'));
});

test('u100_2_no_scripted_cases', () => {
  assert.doesNotMatch(indexHtml(), /data-action="scripted"|scriptedCard|Scripted demo/);
  const app = readFileSync(new URL('../../js/app.mjs', import.meta.url), 'utf8');
  assert.doesNotMatch(app, /scripted-cases|SCRIPTED_CASES|scriptedResult|showScripted|'scripted/);
  assert.equal(existsSync(new URL('../../js/scripted-cases.mjs', import.meta.url)), false);
});

test('u100_3_bank_button_goes_through_submit', async () => {
  for (const id of ['electricSuggestion', 'bankSuggestion']) {
    const suggestion = elements(indexHtml(), 'button').find((button) => button.attrs.get('id') === id);
    assert.ok(suggestion, `${id} exists in the page`);
    const request = suggestion.attrs.get('data-request');
    if (id === 'bankSuggestion') assert.equal(request, bankRequest);
    const { doc, nodes, click } = documentHarness();
    const server = createFakeMcpServer({
      tools: [{ name: 'prepare_task', description: 'Prepare a call task' }],
      results: { prepare_task: { task_id: 'T-u100', status: 'ready', task_card: taskCard(), missing: [], skills: [] } },
    });
    const fetchImpl = createFakeFetch({ mcp: server, routes: {
      'GET /app-config': { mode: 'text-sim', poll_ms: 777 },
      'POST /assistant/turn': ({ body }) => turnOut(body.tool_result
        ? { kind: 'await_user', ui: 'task_card' }
        : { kind: 'tool_call', name: 'prepare_task', tool_use_id: 'prep-u100', arguments: { request: body.user_text } }),
    } });
    const ctx = await boot({ document: doc, fetchImpl, ...handoffEnv() });
    try {
      click(suggestion.attrs.get('data-action'), { request });
      await settle(5);
      assert.equal(nodes.get('error').hidden, true);
      assert.equal(nodes.has('hero'), false, 'submit removes the home suggestions');
      assert.equal(fetchImpl.callsTo('/assistant/turn')[0]?.body.user_text, request);
      assert.deepEqual(server.toolCalls(), ['prepare_task']);
      assert.deepEqual(server.requests.find((call) => call.method === 'tools/call').params.arguments, { request });
      assert.equal(ctx.state.task_id, 'T-u100');
    } finally {
      await ctx.dispose();
    }
  }
});
