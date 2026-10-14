import test from 'node:test';
import assert from 'node:assert/strict';
import { boot } from '../../js/app.mjs';
import { createFakeFetch, createFakeMcpServer } from '../helpers.mjs';
import { handoffEnv, buttonsByText, settle } from '../c-support.mjs';
import { documentHarness } from './s5-dom.mjs';

test('K4 the membership script requires an explicit option before showing a result', async () => {
  const { doc, nodes, click } = documentHarness();
  const server = createFakeMcpServer();
  const fetchImpl = createFakeFetch({ mcp: server, routes: { 'GET /app-config': { mode: 'replay', poll_ms: 777 } } });
  const ctx = await boot({ document: doc, fetchImpl, ...handoffEnv() });
  try {
    click('scripted', { case: 'kestrel' });
    const card = nodes.get('scriptedCard');
    const initial = card.innerHTML;
    const show = buttonsByText(initial, /Show scripted result/)[0];
    assert.ok(show.attrs.has('disabled'), 'without a chosen option the result button is disabled');
    click('scripted-result', { case: 'kestrel' });
    assert.equal(card.innerHTML, initial, 'a stale event must not imply cancellation consent');
    const resultButton = { disabled: true };
    nodes.set('scriptedCard [data-action="scripted-result"]', resultButton);
    nodes.set('scriptedCard input:checked', { value: 'stop_renewal' });
    const event = new Event('change');
    Object.defineProperty(event, 'target', { value: {
      value: 'stop_renewal', matches: (selector) => selector === '#scriptedCard input[type="radio"]',
    } });
    doc.dispatchEvent(event);
    assert.equal(resultButton.disabled, false);
    click('scripted-result', { case: 'kestrel' });
    await settle(1);
    assert.match(card.innerHTML, /Auto-renewal turned off/);
    assert.match(card.innerHTML, /Active for its current term/);
    assert.deepEqual(server.toolCalls(), []);
  } finally { await ctx.dispose(); }
});
