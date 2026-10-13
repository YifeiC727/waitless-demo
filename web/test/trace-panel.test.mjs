// AC-11、AC-12：debug=1 才有 Developer trace；追踪面板显示本轮 skill 名与 commit，并为每个决策点的每个候选画概率条
// （tech §6.1 debug、§6.2 decision、§7.1）。公开接口：isDebug、renderTracePanel、renderProbabilityBars。

import test from 'node:test';
import assert from 'node:assert/strict';

import { isDebug, renderTracePanel, renderProbabilityBars } from '../js/trace-panel.mjs';
import { COMMIT_A, COMMIT_B, textOf, widthPercents } from './c-support.mjs';

const BASE = 'http://waitless.test';

function close(a, b) {
  return Math.abs(a - b) < 0.05;
}

// expected ⊆ found（按多重集合，误差 0.05 个百分点）
function assertWidthsInclude(found, expected, msg) {
  const pool = [...found];
  for (const want of expected) {
    const i = pool.findIndex((w) => close(w, want));
    assert.ok(i >= 0, `${msg}: a bar with width ${want}% (found ${found.join(', ')})`);
    pool.splice(i, 1);
  }
}

test('ac11_is_debug_only_when_debug_equals_1', () => {
  assert.equal(isDebug(`${BASE}/?debug=1`), true);
  assert.equal(isDebug(new URL(`${BASE}/?case=x&debug=1`)), true, 'URL objects are accepted');
  assert.equal(isDebug(`${BASE}/`), false);
  assert.equal(isDebug(`${BASE}/?debug=0`), false);
  assert.equal(isDebug(`${BASE}/?debug=true`), false);
});

const ASSISTANT_DECISION = {
  decision_id: 'd-a1',
  point: 'assistant_next_action',
  choice: 'prepare_task',
  confidence: 0.72,
  probabilities: { prepare_task: 0.72, reply_only: 0.2, ask_clarify: 0.08 },
  policy_rule: 'auto_high_conf',
};

const CALL_ROUND_DECISION = {
  decision_id: 'd-c1',
  point: 'call_round',
  intent: 'press:1',
  action: 'press',
  reason_code: 'auto_high_conf',
  jev: {
    menu_choice: { type: 'choice', choice: 'opt_1', confidence: 0.91, probabilities: { opt_1: 0.91, opt_2: 0.06, none_of_the_above: 0.03 } },
    call_phase: { type: 'choice', choice: 'ivr', confidence: 0.64, probabilities: { ivr: 0.64, hold: 0.25, ai_agent: 0.11 } },
    needs_user: { type: 'noul', noul: 0.04 },
    stakes: { type: 'score', score: 1 },
  },
  latency_ms: 320,
};

test('ac12_panel_shows_skill_names_and_commits', () => {
  const skills = [{ name: 'assisted-call', commit: COMMIT_A }, { name: 'decide-option', commit: COMMIT_B }];
  const text = textOf(renderTracePanel({ entries: [], decisions: [], skills }));
  for (const s of skills) {
    assert.ok(text.includes(s.name), `skill ${s.name}`);
    assert.ok(text.includes(s.commit), `commit of ${s.name}`);
  }
});

test('ac12_probability_bars_width_is_probability_times_100', () => {
  const html = renderProbabilityBars({ prepare_task: 0.72, reply_only: 0.2, ask_clarify: 0.08 });
  const widths = widthPercents(html).filter((w) => w !== 100);
  assert.equal(widths.length, 3, `one bar per candidate, got ${widths.join(', ')}`);
  assertWidthsInclude(widths, [72, 20, 8], 'renderProbabilityBars');
  const text = textOf(html);
  for (const name of ['prepare_task', 'reply_only', 'ask_clarify']) assert.ok(text.includes(name), `bar label ${name}`);
});

test('ac12_panel_draws_bars_for_assistant_and_in_call_decisions', () => {
  const html = renderTracePanel({ entries: [], decisions: [ASSISTANT_DECISION, CALL_ROUND_DECISION], skills: [{ name: 'assisted-call', commit: COMMIT_A }] });
  assertWidthsInclude(widthPercents(html), [72, 20, 8], 'assistant_next_action');
  assertWidthsInclude(widthPercents(html), [91, 6, 3], 'call_round.menu_choice');
  assertWidthsInclude(widthPercents(html), [64, 25, 11], 'call_round.call_phase');
  assertWidthsInclude(widthPercents(html), [72, 20, 8, 91, 6, 3, 64, 25, 11], 'all candidates together');
  const text = textOf(html);
  for (const name of ['prepare_task', 'opt_1', 'none_of_the_above', 'ivr', 'ai_agent']) assert.ok(text.includes(name), `candidate ${name}`);
});
