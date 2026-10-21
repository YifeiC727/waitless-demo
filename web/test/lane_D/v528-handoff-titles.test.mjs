import test from 'node:test';
import assert from 'node:assert/strict';
import { renderHandoffReady } from '../../js/cards.mjs';
import { decodeEntities } from '../c-support.mjs';

const titles = {
  live_human: 'Someone picked up — join now',
  transfer_connected: "You're connected — join now",
  needs_user: 'Waitless needs you — join now',
};

for (const [kind, title] of [...Object.entries(titles), [undefined, 'Join the call now'],
  ['unknown', 'Join the call now'], ['__proto__', 'Join the call now'], ['toString', 'Join the call now']]) {
  test(`handoff heading follows reason_kind ${kind}`, () => {
    const html = renderHandoffReady({ reason_kind: kind, remaining_s: 0,
      company_name: '<img src=x>', reason: '<script>unsafe</script>', expires_at: '"<deadline>' }, { mode: 'live-sim' });
    assert.equal(decodeEntities(html.match(/<h2>(.*?)<\/h2>/s)[1]), title);
    assert.ok(html.includes('&lt;script&gt;unsafe&lt;/script&gt;'));
    assert.ok(html.includes('datetime="&quot;&lt;deadline&gt;"'));
    assert.match(html, /Join within 0 seconds/);
    assert.match(html, /data-action="join" disabled/);
    assert.doesNotMatch(html, /<script>|<img/);
  });
}

test('reconnect keeps deadline and missing/negative countdown behavior', () => {
  const html = renderHandoffReady({ reason_kind: 'needs_user', remaining_s: 17 }, { mode: 'live-sim', reconnecting: true });
  assert.match(html, /Reconnect within 17 seconds/);
  assert.match(html, />Rejoin call<\/button>/);
  assert.doesNotMatch(html, / disabled/);
  for (const remaining_s of [undefined, -1, 1.5, '5']) {
    assert.doesNotMatch(renderHandoffReady({ remaining_s }, { mode: 'live-sim' }), /within .* seconds/);
  }
});

test('replay company heading remains escaped', () => {
  const html = renderHandoffReady({ reason_kind: 'needs_user', company_name: '<company>' });
  assert.match(html, /<h2>&lt;company&gt; is ready<\/h2>/);
});

test('serialized status handoff payload renders all reason kinds and notifications', () => {
  const messages = {
    live_human: '有人接起了，请马上加入',
    transfer_connected: '转接后线路上在跟你说话，请马上加入',
    needs_user: '线路上的情况 Waitless 判断不了，请你接手',
  };
  for (const [reason_kind, reason] of Object.entries(messages)) {
    const status = JSON.parse(JSON.stringify({ phase: 'handoff_ready', handoff: { reason_kind, reason, remaining_s: 73 } }));
    const html = renderHandoffReady(status.handoff, { mode: 'live-sim' });
    assert.equal(decodeEntities(html.match(/<h2>(.*?)<\/h2>/s)[1]), titles[reason_kind]);
    assert.ok(html.includes(reason));
    assert.match(html, /Join within 73 seconds/);
  }
});
