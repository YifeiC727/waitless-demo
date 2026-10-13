// AC-07–AC-10、AC-13：卡片渲染是纯函数，返回 HTML 字符串（tech §6.1、§7.1）。
// 公开接口：renderTaskCard、renderPendingChoice、renderResult、renderHandoffCard、escapeHtml。
// 断言只看渲染出的字符串：链接、单选的 checked/disabled、按钮 disabled、标题下的内容、可见文字。

import test from 'node:test';
import assert from 'node:assert/strict';

import { renderTaskCard, renderPendingChoice, renderResult, renderHandoffCard, escapeHtml } from '../js/cards.mjs';
import { trackTimers } from './helpers.mjs';
import {
  taskCard, pendingChoice, caseResult, joinOut,
  elements, startTags, hasAttr, decodeEntities, textOf, sectionUnder, buttonsByText, count,
} from './c-support.mjs';

function radios(html) {
  return startTags(html, 'input').filter((t) => (t.attrs.get('type') || '').toLowerCase() === 'radio');
}

function radioFor(html, optionId) {
  const found = radios(html).filter((t) => t.attrs.get('value') === optionId);
  assert.equal(found.length, 1, `exactly one radio input with value="${optionId}"`);
  return found[0];
}

function startButton(html) {
  const found = buttonsByText(html, /start call/i);
  assert.equal(found.length, 1, 'exactly one "Start call" button');
  return found[0];
}

function confirmButton(html) {
  const found = buttonsByText(html, /^confirm\b/i);
  assert.equal(found.length, 1, 'exactly one "Confirm" button');
  return found[0];
}

test('ac07_escape_html_escapes_the_five_characters', () => {
  const raw = `Tom & Jerry's <b>"deal"</b>`;
  const out = escapeHtml(raw);
  assert.doesNotMatch(out, /[<>"']/);
  assert.doesNotMatch(out, /&(?!(?:[a-z]+|#[0-9]+|#x[0-9a-f]+);)/i, 'every & starts an entity');
  assert.equal(decodeEntities(out), raw);
});

test('ac07_task_card_shows_official_number_and_one_link_per_source', () => {
  const card = taskCard();
  const html = renderTaskCard(card);
  assert.ok(textOf(html).includes(card.company.official_number), 'official number is shown');
  const links = elements(html, 'a');
  for (const src of card.company.sources) {
    const matching = links.filter((a) => a.attrs.get('href') === src.url);
    assert.equal(matching.length, 1, `one <a href> for ${src.url}`);
    assert.equal(matching[0].text, src.title, 'link text is the source title');
  }
});

test('ac08_default_option_is_checked_and_changeable_needs_user_is_not_preselected', () => {
  const html = renderTaskCard(taskCard());
  const def = radioFor(html, 'deposit_120');
  const alt = radioFor(html, 'autopay_waive');
  assert.ok(hasAttr(def, 'checked'), 'the default option is preselected');
  assert.ok(!hasAttr(alt, 'checked'));
  assert.ok(!hasAttr(def, 'disabled') && !hasAttr(alt, 'disabled'), 'the default can be changed');
  for (const id of ['paperless_yes', 'paperless_no']) {
    assert.ok(!hasAttr(radioFor(html, id), 'checked'), `${id}: ask_user questions are not preselected`);
  }
  assert.ok(hasAttr(startButton(html), 'disabled'), 'Start call stays disabled until every ask_user question is answered');
});

test('ac08_selected_overrides_default_and_start_enabled_when_all_answered', () => {
  const card = taskCard();
  card.precall_decisions[0].selected = 'autopay_waive';
  card.precall_decisions[1].selected = 'paperless_no';
  const html = renderTaskCard(card);
  assert.ok(hasAttr(radioFor(html, 'autopay_waive'), 'checked'));
  assert.ok(!hasAttr(radioFor(html, 'deposit_120'), 'checked'));
  assert.ok(hasAttr(radioFor(html, 'paperless_no'), 'checked'));
  assert.ok(!hasAttr(startButton(html), 'disabled'), 'Start call is enabled once every ask_user question has a choice');
});

test('ac08_task_card_sets_no_timers', () => {
  const timers = trackTimers();
  try {
    renderTaskCard(taskCard());
  } finally {
    timers.restore();
  }
  assert.equal(timers.count(), 0, 'no countdown and no auto start');
});

test('ac09_pending_choice_radios_unchecked_confirm_disabled', () => {
  const choice = pendingChoice();
  const html = renderPendingChoice(choice);
  const rs = radios(html);
  assert.equal(rs.length, 3);
  assert.deepEqual(rs.map((r) => r.attrs.get('value')), choice.options.map((o) => o.id));
  assert.ok(rs.every((r) => !hasAttr(r, 'checked')), 'no option is preselected');
  assert.equal(new Set(rs.map((r) => r.attrs.get('name'))).size, 1, 'one single-choice group');
  assert.ok(hasAttr(confirmButton(html), 'disabled'), 'Confirm is disabled before a choice');
  assert.ok(textOf(html).includes(choice.question_text), "the company's question is shown as text");
});

test('ac09_pending_choice_question_text_is_data_not_markup', () => {
  const html = renderPendingChoice(pendingChoice({ question_text: 'Pick one <img src=x onerror=alert(1)> now' }));
  assert.equal(startTags(html, 'img').length, 0, 'question_text is escaped');
  assert.ok(textOf(html).includes('Pick one <img src=x onerror=alert(1)> now'));
});

test('ac09_more_than_three_options_are_cut_to_the_first_three', () => {
  const choice = pendingChoice({
    options: ['a', 'b', 'c', 'd'].map((id) => ({ id: `opt_${id}`, label: `Option ${id.toUpperCase()}`, detail: `Detail ${id}` })),
  });
  const html = renderPendingChoice(choice);
  assert.deepEqual(radios(html).map((r) => r.attrs.get('value')), ['opt_a', 'opt_b', 'opt_c'], 'G11: only the first 3 options, in order');
  assert.ok(!textOf(html).includes('Option D'), 'the 4th option is not shown');
});

test('ac09_pending_choice_sets_no_timers', () => {
  const timers = trackTimers();
  try {
    renderPendingChoice(pendingChoice());
  } finally {
    timers.restore();
  }
  assert.equal(timers.count(), 0, 'no countdown that submits on its own');
});

test('ac10_verified_only_under_confirmed_follow_ups_only_under_needs_follow_up', () => {
  const result = caseResult();
  const html = renderResult(result);
  const confirmed = sectionUnder(html, 'Confirmed');
  const followUp = sectionUnder(html, 'Needs follow-up');
  assert.ok(confirmed !== null, 'a "Confirmed" heading');
  assert.ok(followUp !== null, 'a "Needs follow-up" heading');
  for (const item of result.verified) {
    for (const s of [item.label, item.value]) {
      const total = count(textOf(html), s);
      assert.ok(total >= 1, `${s} is shown`);
      assert.equal(count(textOf(confirmed), s), total, `${s} appears only under Confirmed`);
    }
  }
  for (const item of result.follow_ups) {
    for (const s of [item.label, item.detail]) {
      const total = count(textOf(html), s);
      assert.ok(total >= 1, `${s} is shown`);
      assert.equal(count(textOf(followUp), s), total, `${s} appears only under Needs follow-up`);
    }
  }
});

const ALL_OFF = { ai_media: 'disconnected', recording: false, transcription: false };
const LINES = { ai_media: 'AI is not listening', recording: 'Not recording', transcription: 'Not transcribing' };

test('ac13_handoff_card_privacy_lines_follow_media_fields', () => {
  const cases = [
    { media: { ...ALL_OFF }, expect: { ai_media: true, recording: true, transcription: true } },
    { media: { ...ALL_OFF, ai_media: 'connected' }, expect: { ai_media: false, recording: true, transcription: true } },
    { media: { ...ALL_OFF, recording: true }, expect: { ai_media: true, recording: false, transcription: true } },
    { media: { ...ALL_OFF, transcription: true }, expect: { ai_media: true, recording: true, transcription: false } },
  ];
  for (const { media, expect } of cases) {
    const text = textOf(renderHandoffCard(joinOut({ media })));
    for (const [field, line] of Object.entries(LINES)) {
      assert.equal(text.includes(line), expect[field], `media=${JSON.stringify(media)}: "${line}" ${expect[field] ? 'shown' : 'not shown'}`);
    }
  }
});
