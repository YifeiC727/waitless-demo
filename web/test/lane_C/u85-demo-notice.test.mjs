import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { renderTaskCard } from '../../js/cards.mjs';
import { taskCard, elements, count } from '../c-support.mjs';

test('u85_6_task_card_renders_demo_notice', () => {
  const card = {
    ...taskCard(),
    simulated_line: {
      name: 'Simulated utility line',
      notice: 'Demo: real <lookup> & simulated call',
    },
  };
  const html = renderTaskCard(card);
  const escaped = 'Demo: real &lt;lookup&gt; &amp; simulated call';
  assert.equal(count(html, escaped), 1);
  const notices = elements(html, 'p').filter((p) =>
    (p.attrs.get('class') ?? '').split(/\s+/).includes('demo-notice'));
  assert.equal(notices.length, 1);
  assert.equal(notices[0].inner, escaped);
  const sources = elements(html, 'div').find((div) =>
    (div.attrs.get('class') ?? '').split(/\s+/).includes('sources'));
  assert.ok(sources);
  assert.ok(notices[0].start >= sources.end, 'notice follows the source links');
  const number = elements(html, 'p').find((p) =>
    (p.attrs.get('class') ?? '').split(/\s+/).includes('official-number'));
  assert.ok(number);
  assert.equal(number.text, card.company.official_number);
  const links = elements(html, 'a');
  assert.equal(links.length, 2);
  assert.deepEqual(links.map((a) => ({ url: a.attrs.get('href'), title: a.text })), card.company.sources);
});

test('u85_6_no_notice_without_simulated_line', () => {
  const html = renderTaskCard(taskCard());
  const nullHtml = renderTaskCard({ ...taskCard(), simulated_line: null });
  assert.doesNotMatch(html, /demo-notice/);
  assert.doesNotMatch(nullHtml, /demo-notice/);
  assert.equal(nullHtml, html);
});

test('u85_6_electric_suggestion_names_no_company', () => {
  const html = readFileSync(new URL('../../index.html', import.meta.url), 'utf8');
  const buttons = elements(html, 'button').filter((button) => button.attrs.get('id') === 'electricSuggestion');
  assert.equal(buttons.length, 1);
  const button = buttons[0];
  const spans = elements(button.inner, 'span');
  assert.equal(spans.length, 1);
  assert.equal(spans[0].text, 'Your local electric utility');
  assert.doesNotMatch(html.slice(button.start, button.end), /Lakeshore/);
  assert.equal(button.attrs.get('data-request'), 'Start electricity at my new apartment on November 1.');
});
