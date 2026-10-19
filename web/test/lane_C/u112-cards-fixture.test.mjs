import test from 'node:test';
import assert from 'node:assert/strict';
import { renderTaskCard } from '../../js/cards.mjs';
import { taskCard, startTags, hasAttr } from '../c-support.mjs';

test('u112_c_7_product_fixture_not_preselected', () => {
  const card = taskCard(), question = card.precall_decisions[0];
  assert.equal(question.question_id, 'deposit_consent');
  assert.equal(question.question, 'A refundable deposit may be required. The account holder will confirm it on the call. Should Waitless tell the company you are willing to pay it?');
  assert.deepEqual(question.options, [
    { id: 'approve', label: 'Yes, I am willing to pay a refundable deposit', detail: 'You still confirm it yourself on the call' },
    { id: 'ask_me', label: 'No, I will decide on the call', detail: 'Waitless does not agree to a deposit for you' },
  ]);
  assert.equal(question.default, null);
  assert.equal(question.selected, null);
  assert.equal(question.needs_user, true);
  const options = html => startTags(html, 'input').filter(input => ['approve', 'ask_me'].includes(input.attrs.get('value')));
  const unselected = options(renderTaskCard(card));
  assert.equal(unselected.length, 2);
  assert.ok(unselected.every(input => !hasAttr(input, 'checked')));
  question.selected = 'approve';
  const selected = options(renderTaskCard(card));
  assert.deepEqual(selected.filter(input => hasAttr(input, 'checked')).map(input => input.attrs.get('value')), ['approve']);
});
