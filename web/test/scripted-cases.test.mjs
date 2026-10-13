// AC-17（C 线部分）：原型另两条脚本案例换成虚构品牌 Kestrel Wholesale Club、Bluefin Harbor Bank，
// web/ 下任何文件都不含第三方商标（与 `scripts/checks.sh trademarks` 同一组词，后者由集成检查线写）。
// 公开接口：SCRIPTED_CASES、scriptedResult。检查词由 c-support.mjs 拼出，本文件不含原词。

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { relative } from 'node:path';
import { fileURLToPath } from 'node:url';

import { SCRIPTED_CASES, scriptedResult } from '../js/scripted-cases.mjs';
import { trademarkPattern, listFiles, jsTokens } from './c-support.mjs';

const BRANDS = ['Kestrel Wholesale Club', 'Bluefin Harbor Bank'];

test('ac17_brands_replaced_and_no_file_under_web_contains_a_trademark', () => {
  const cases = Object.values(SCRIPTED_CASES);
  assert.equal(cases.length, 2, 'exactly the two scripted cases from the prototype');
  assert.deepEqual(cases.map((c) => c.provider).sort(), [...BRANDS].sort());
  for (const c of cases) assert.ok(typeof c.request === 'string' && c.request.trim(), `${c.provider}: a request text`);
  const json = JSON.stringify(SCRIPTED_CASES);
  const hit = json.match(trademarkPattern());
  assert.equal(hit, null, `scripted cases contain a third-party trademark: ${hit && hit[0]}`);
  // 与 `grep -rniE` 同一口径：web/ 下每个文件逐行检查
  const webDir = new URL('../', import.meta.url);
  const root = fileURLToPath(webDir);
  const re = trademarkPattern();
  const hits = [];
  for (const f of listFiles(webDir)) {
    readFileSync(f, 'latin1').split('\n').forEach((line, i) => { if (re.test(line)) hits.push(`web/${relative(root, f)}:${i + 1}`); });
  }
  assert.deepEqual(hits, [], 'grep -rniE over web/ must print nothing');
});

test('ac17_scripted_results_contain_no_third_party_trademark', () => {
  const ids = Object.keys(SCRIPTED_CASES);
  assert.equal(ids.length, 2);
  for (const id of ids) {
    for (const tool of ['prepare_task', 'get_case_result']) {
      const out = scriptedResult(id, tool, {});
      assert.ok(out && typeof out === 'object', `${id}/${tool}: a scripted result object`);
      const hit = JSON.stringify(out).match(trademarkPattern());
      assert.equal(hit, null, `${id}/${tool}: contains ${hit && hit[0]}`);
    }
  }
});

// G6（暂定，Jev 0.58）：schedule_callback、preview_callback 只是 Bluefin Harbor Bank 脚本案例里的本地演示步骤，
// 不进 MCP、不做真实回拨。
test('ac17_bluefin_callback_steps_are_local_demo_only', () => {
  const entry = Object.entries(SCRIPTED_CASES).find(([, c]) => c.provider === 'Bluefin Harbor Bank');
  assert.ok(entry, 'the Bluefin Harbor Bank scripted case');
  const [id] = entry;
  const scheduled = scriptedResult(id, 'schedule_callback', { date: 'Oct 15', time: '1:30 PM', timezone: 'America/Chicago' });
  assert.ok(scheduled && typeof scheduled === 'object');
  assert.equal(scheduled.real_booking, false, 'no real callback is booked');
  const preview = scriptedResult(id, 'preview_callback', {});
  assert.ok(preview && typeof preview === 'object');
  for (const out of [scheduled, preview]) assert.equal(JSON.stringify(out).match(trademarkPattern()), null);
  const jsDir = new URL('../js/', import.meta.url);
  const viaMcp = [];
  for (const f of readdirSync(jsDir).filter((n) => /\.m?js$/.test(n))) {
    const toks = jsTokens(readFileSync(new URL(f, jsDir), 'utf8'));
    toks.forEach((t, i) => {
      if (t.type === 'ident' && t.value === 'callTool' && toks[i + 1]?.value === '(' && ['schedule_callback', 'preview_callback'].includes(toks[i + 2]?.value)) {
        viaMcp.push(`${f}: callTool('${toks[i + 2].value}')`);
      }
    });
  }
  assert.deepEqual(viaMcp, [], 'callback steps never go through mcp.callTool');
});
