// C 线（手机网页）测试的共用工具（第 3 步写，随可见测试冻结）。不是测试文件：
// `node --test "web/test/**/*.test.mjs"` 不会把它当测试跑。
//
//   HTML：startTags、elements、attrsOf、hasAttr、decodeEntities、textOf、headings、sectionUnder、widthPercents、
//         buttonsByText、hasRawUnescaped
//   源码：jsTokens（去掉注释，识别字符串、模板、正则）、functionBodyRange、arrayLiteralRange
//   其他：trademarkPattern（AC-17 的检查词，本文件里不出现原词）、listFiles、settle、fixtures
//   转接：openHandoff、sendReady、handleOf、emitSine、byteLen、int16Of（驱动 joinHandoffAudio 与 helpers 的假对象）
//   按钮：handoffEnv（app 处理函数 ctx 的环境字段）、recordingHandle（假转接句柄）、callLog（跨对象记录调用顺序）

import assert from 'node:assert/strict';
import { readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  FakeWebSocket, FakeAudioContext, FakeAudioWorkletNode, createFakeMediaDevices, float32Sine,
} from './helpers.mjs';

// ---------------------------------------------------------------------------
// HTML（渲染函数返回字符串；没有 DOM，只做足够严格的解析）
// ---------------------------------------------------------------------------

const NAMED = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };

export function decodeEntities(s) {
  return String(s).replace(/&(#x[0-9a-f]+|#[0-9]+|[a-z]+);/gi, (m, e) => {
    if (e[0] === '#') {
      const cp = e[1] === 'x' || e[1] === 'X' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      return Number.isFinite(cp) ? String.fromCodePoint(cp) : m;
    }
    return Object.prototype.hasOwnProperty.call(NAMED, e.toLowerCase()) ? NAMED[e.toLowerCase()] : m;
  });
}

const ATTR_RE = /([^\s"'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;

export function attrsOf(attrText) {
  const out = new Map();
  const re = new RegExp(ATTR_RE.source, 'g');
  let m;
  while ((m = re.exec(attrText))) {
    const name = m[1].toLowerCase();
    const raw = m[2] ?? m[3] ?? m[4];
    if (!out.has(name)) out.set(name, raw === undefined ? '' : decodeEntities(raw));
  }
  return out;
}

const TAG_BODY = `((?:[^>"']|"[^"]*"|'[^']*')*)`;

// 开始标签（含 void 元素，如 input）：[{tag, attrs:Map, raw, start, end}]
export function startTags(html, tag) {
  const re = new RegExp(`<(${tag})\\b${TAG_BODY}>`, 'gi');
  const out = [];
  let m;
  while ((m = re.exec(html))) {
    out.push({ tag: m[1].toLowerCase(), attrs: attrsOf(m[2]), raw: m[0], start: m.index, end: m.index + m[0].length });
  }
  return out;
}

// 成对元素（不处理同名嵌套）：[{tag, attrs, inner, text, start, end}]
export function elements(html, tag) {
  const re = new RegExp(`<(${tag})\\b${TAG_BODY}>([\\s\\S]*?)<\\/\\1\\s*>`, 'gi');
  const out = [];
  let m;
  while ((m = re.exec(html))) {
    out.push({
      tag: m[1].toLowerCase(),
      attrs: attrsOf(m[2]),
      inner: m[3],
      text: textOf(m[3]),
      start: m.index,
      end: m.index + m[0].length,
    });
  }
  return out;
}

export function hasAttr(el, name) {
  return el.attrs.has(name.toLowerCase());
}

// 可见文字：去掉标签、解码实体、合并空白
export function textOf(html) {
  return decodeEntities(
    String(html)
      .replace(/<script\b[\s\S]*?<\/script\s*>/gi, ' ')
      .replace(/<style\b[\s\S]*?<\/style\s*>/gi, ' ')
      .replace(/<[^>]*>/g, ' '),
  )
    .replace(/\s+/g, ' ')
    .trim();
}

// 标题：h1–h6，或 role="heading"（层级取 aria-level，缺省 2）
export function headings(html) {
  const out = [];
  for (const el of elements(html, 'h[1-6]')) out.push({ level: Number(el.tag[1]), text: el.text, start: el.start, end: el.end });
  const re = new RegExp(`<([a-z][a-z0-9-]*)\\b${TAG_BODY}>([\\s\\S]*?)<\\/\\1\\s*>`, 'gi');
  let m;
  while ((m = re.exec(html))) {
    const attrs = attrsOf(m[2]);
    if ((attrs.get('role') || '').toLowerCase() !== 'heading') continue;
    const level = Number(attrs.get('aria-level')) || 2;
    out.push({ level, text: textOf(m[3]), start: m.index, end: m.index + m[0].length });
  }
  return out.sort((a, b) => a.start - b.start);
}

// 某个标题下的内容：标题文字以 title 开头（不分大小写）；范围从该标题结束到下一个同级或更高级标题开始（或字符串末尾）。找不到返回 null。
export function sectionUnder(html, title) {
  const hs = headings(html);
  const want = title.trim().toLowerCase();
  const idx = hs.findIndex((h) => h.text.trim().toLowerCase().startsWith(want));
  if (idx < 0) return null;
  const h = hs[idx];
  const next = hs.slice(idx + 1).find((x) => x.start >= h.end && x.level <= h.level);
  return html.slice(h.end, next ? next.start : html.length);
}

// 内联样式里的 width:NN%（概率条，AC-12）
export function widthPercents(html) {
  return [...String(html).matchAll(/width\s*:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*%/gi)].map((m) => Number(m[1]));
}

export function buttonsByText(html, re) {
  return elements(html, 'button').filter((b) => re.test(b.text));
}

// 原样出现了未转义的危险片段（用于 XSS/转义检查）
export function hasRawUnescaped(html, fragment) {
  return String(html).includes(fragment);
}

export function count(haystack, needle) {
  if (!needle) return 0;
  let n = 0;
  let i = 0;
  for (;;) {
    i = haystack.indexOf(needle, i);
    if (i < 0) return n;
    n += 1;
    i += needle.length;
  }
}

// ---------------------------------------------------------------------------
// 源码扫描（AC-06：用户动作工具只在按钮处理函数里出现）
// ---------------------------------------------------------------------------

const REGEX_KEYWORDS = new Set(['return', 'typeof', 'instanceof', 'in', 'of', 'new', 'delete', 'void', 'throw', 'case', 'do', 'else', 'yield', 'await']);

function regexAllowedAfter(prev) {
  if (!prev) return true;
  if (prev.type === 'punct') return !(prev.value === ')' || prev.value === ']');
  if (prev.type === 'ident') return REGEX_KEYWORDS.has(prev.value);
  return false;
}

// 词法切分：去掉注释；字符串和不带 ${} 的模板 → type 'string'（value 是解码后的内容）；
// 带 ${} 的模板 → 'template'；正则字面量 → 'regex'；其余 ident、number、punct。
export function jsTokens(src) {
  const toks = [];
  const n = src.length;
  let i = 0;
  while (i < n) {
    const c = src[i];
    if (c === ' ' || c === '\t' || c === '\n' || c === '\r' || c === '\f' || c === '\v' || c === ' ' || c === '﻿') { i += 1; continue; }
    if (c === '/' && src[i + 1] === '/') { while (i < n && src[i] !== '\n') i += 1; continue; }
    if (c === '/' && src[i + 1] === '*') { const e = src.indexOf('*/', i + 2); i = e < 0 ? n : e + 2; continue; }
    if (c === '"' || c === "'") {
      let j = i + 1;
      let v = '';
      while (j < n && src[j] !== c) {
        if (src[j] === '\\') { v += src[j + 1] ?? ''; j += 2; } else { v += src[j]; j += 1; }
      }
      toks.push({ type: 'string', value: v, start: i, end: j + 1 });
      i = j + 1;
      continue;
    }
    if (c === '`') {
      let j = i + 1;
      let v = '';
      let hasExpr = false;
      while (j < n && src[j] !== '`') {
        if (src[j] === '\\') { v += src[j + 1] ?? ''; j += 2; continue; }
        if (src[j] === '$' && src[j + 1] === '{') {
          hasExpr = true;
          let depth = 1;
          j += 2;
          while (j < n && depth > 0) {
            if (src[j] === '{') depth += 1;
            else if (src[j] === '}') depth -= 1;
            j += 1;
          }
          continue;
        }
        v += src[j];
        j += 1;
      }
      toks.push({ type: hasExpr ? 'template' : 'string', value: v, start: i, end: j + 1 });
      i = j + 1;
      continue;
    }
    if (c === '/' && regexAllowedAfter(toks[toks.length - 1])) {
      let j = i + 1;
      let inClass = false;
      while (j < n) {
        const d = src[j];
        if (d === '\\') { j += 2; continue; }
        if (d === '\n') break;
        if (d === '[') inClass = true;
        else if (d === ']') inClass = false;
        else if (d === '/' && !inClass) break;
        j += 1;
      }
      j += 1;
      while (j < n && /[a-z]/i.test(src[j])) j += 1;
      toks.push({ type: 'regex', value: src.slice(i, j), start: i, end: j });
      i = j;
      continue;
    }
    if (/[A-Za-z_$]/.test(c)) {
      let j = i + 1;
      while (j < n && /[A-Za-z0-9_$]/.test(src[j])) j += 1;
      toks.push({ type: 'ident', value: src.slice(i, j), start: i, end: j });
      i = j;
      continue;
    }
    if (/[0-9]/.test(c)) {
      let j = i + 1;
      while (j < n && /[0-9A-Za-z_.]/.test(src[j])) j += 1;
      toks.push({ type: 'number', value: src.slice(i, j), start: i, end: j });
      i = j;
      continue;
    }
    if (c === '=' && src[i + 1] === '>') { toks.push({ type: 'punct', value: '=>', start: i, end: i + 2 }); i += 2; continue; }
    toks.push({ type: 'punct', value: c, start: i, end: i + 1 });
    i += 1;
  }
  return toks;
}

function matchClose(tokens, openIdx, open, close) {
  let depth = 0;
  for (let k = openIdx; k < tokens.length; k += 1) {
    const v = tokens[k].type === 'punct' ? tokens[k].value : null;
    if (v === open) depth += 1;
    else if (v === close) {
      depth -= 1;
      if (depth === 0) return k;
    }
  }
  return -1;
}

// 名为 name 的函数（function 声明、`name = function/箭头`、对象方法 `name(...) {`、`name: ...`）的函数体字符范围。
export function functionBodyRange(tokens, name) {
  for (let k = 0; k < tokens.length; k += 1) {
    const t = tokens[k];
    if (t.type !== 'ident' || t.value !== name) continue;
    const prev = tokens[k - 1];
    const next = tokens[k + 1];
    const isDecl = (prev && prev.type === 'ident' && prev.value === 'function')
      || (next && next.type === 'punct' && (next.value === '=' || next.value === ':' || next.value === '('));
    if (!isDecl) continue;
    let m = k + 1;
    while (m < tokens.length && !(tokens[m].type === 'punct' && tokens[m].value === '(')) m += 1;
    const closeParen = matchClose(tokens, m, '(', ')');
    if (closeParen < 0) continue;
    let b = closeParen + 1;
    if (tokens[b] && tokens[b].value === '=>') b += 1;
    if (!tokens[b] || tokens[b].type !== 'punct' || tokens[b].value !== '{') continue;
    const end = matchClose(tokens, b, '{', '}');
    if (end < 0) continue;
    return { start: tokens[b].start, end: tokens[end].end };
  }
  return null;
}

// `name = [ ... ]` 或 `name = Object.freeze([ ... ])` 的数组字面量范围
export function arrayLiteralRange(tokens, name) {
  for (let k = 0; k < tokens.length; k += 1) {
    const t = tokens[k];
    if (t.type !== 'ident' || t.value !== name) continue;
    const next = tokens[k + 1];
    if (!next || next.value !== '=') continue;
    let m = k + 2;
    while (m < tokens.length && !(tokens[m].type === 'punct' && (tokens[m].value === '[' || tokens[m].value === ';'))) m += 1;
    if (!tokens[m] || tokens[m].value !== '[') continue;
    const end = matchClose(tokens, m, '[', ']');
    if (end < 0) continue;
    return { start: tokens[m].start, end: tokens[end].end };
  }
  return null;
}

export function within(range, tok) {
  return !!range && tok.start >= range.start && tok.end <= range.end;
}

// ---------------------------------------------------------------------------
// 其他
// ---------------------------------------------------------------------------

// AC-17：与 checks.sh trademarks 相同的三个词（不区分大小写，第三个按整词）。拆开拼接，
// 保证本文件和引用它的测试文件自身不会被 `grep -rniE` 命中。
export function trademarkPattern() {
  return new RegExp(['cost' + 'co', 'bank of ' + 'amer' + 'ica', '\\b' + 'b' + 'oa' + '\\b'].join('|'), 'i');
}

export function listFiles(dirUrl) {
  const root = fileURLToPath(dirUrl);
  const out = [];
  const walk = (p) => {
    for (const e of readdirSync(p, { withFileTypes: true })) {
      const full = join(p, e.name);
      if (e.isDirectory()) walk(full);
      else out.push(full);
    }
  };
  walk(root);
  return out;
}

// 让出若干轮事件循环（setImmediate 不在 trackTimers 统计内，也不是真实等待）
export async function settle(times = 1) {
  for (let i = 0; i < times; i += 1) await new Promise((r) => setImmediate(r));
}

export const COMMIT_A = '3f9c2e1d4b5a69788796a5b4c3d2e1f0a9b8c7d6';
export const COMMIT_B = '0123456789abcdef0123456789abcdef01234567';

// §6.1 TaskCard（字段与 models.TaskCard 一致）
export function taskCard(overrides = {}) {
  return {
    card_version: 2,
    company: {
      company_id: 'lakeshore-energy',
      name: 'Lakeshore Energy',
      official_number: '1-800-555-0142',
      sources: [
        { url: 'https://lakeshore-energy.example/contact', title: 'Contact Lakeshore Energy' },
        { url: 'https://lakeshore-energy.example/help/start-service', title: 'Start or move service' },
      ],
    },
    goal: 'Start electric service',
    task_summary: 'Start electricity service on November 1',
    effective_date: '2026-11-01',
    shared_fields: [
      { key: 'name', label: 'Name', display: 'Alex M.' },
      { key: 'phone_number', label: 'Phone number', display: '••• ••• 0147' },
    ],
    precall_decisions: [
      {
        question_id: 'deposit_consent',
        question: 'A refundable deposit may be required. The account holder will confirm it on the call. Should Waitless tell the company you are willing to pay it?',
        options: [
          { id: 'approve', label: 'Yes, I am willing to pay a refundable deposit', detail: 'You still confirm it yourself on the call' },
          { id: 'ask_me', label: 'No, I will decide on the call', detail: 'Waitless does not agree to a deposit for you' },
        ],
        default: null,
        selected: null,
        needs_user: true,
      },
      {
        question_id: 'paperless',
        question: 'Switch to paperless billing?',
        options: [
          { id: 'paperless_yes', label: 'Yes, paperless', detail: 'Bills by email' },
          { id: 'paperless_no', label: 'No, paper bills', detail: 'Bills by mail' },
        ],
        default: null,
        selected: null,
        needs_user: true,
      },
    ],
    boundary: 'Final changes need your approval.',
    ...overrides,
  };
}

// §6.1 PendingChoice
export function pendingChoice(overrides = {}) {
  return {
    choice_id: 'pc-1',
    question_text: 'We can waive the $25 setup fee if you enroll in paperless billing today. Which do you want?',
    options: [
      { id: 'opt_paperless', label: 'Enroll in paperless', detail: 'Setup fee waived' },
      { id: 'opt_paper', label: 'Keep paper bills', detail: '$25 setup fee applies' },
      { id: 'opt_later', label: 'Decide later', detail: 'Ask again on the next call' },
    ],
    created_at: '2026-10-14T15:00:00Z',
    expires_at: '2026-10-14T15:00:20Z',
    status: 'open',
    ...overrides,
  };
}

// §6.1 get_case_result 输出（CaseResultOut）
export function caseResult(overrides = {}) {
  return {
    task_id: 'T-100',
    company_name: 'Lakeshore Energy',
    outcome: 'confirmed',
    headline: 'Electric service is scheduled',
    verified: [
      { label: 'Service start date', value: 'Begins Nov 1 at 8 AM', evidence: { call_id: 'C-1', turn_seq: 14 } },
      { label: 'Account opened', value: 'Account ending 7731', evidence: { call_id: 'C-1', turn_seq: 15 } },
    ],
    follow_ups: [
      { label: 'Deposit refund', detail: 'Refund requested, not yet received' },
    ],
    confirmation_number: 'LE-48291',
    summary: 'Waitless called Lakeshore Energy and scheduled service.',
    calls: [
      {
        call_id: 'C-1', scenario_id: 'baseline', started_at: '2026-10-14T15:00:00Z', ended_at: '2026-10-14T15:04:30Z',
        duration_s: 270, outcome: 'confirmed', interrupts_used: 1,
      },
    ],
    history: [{ t: '2026-10-14T15:04:30Z', title: 'Call ended', detail: 'Representative hung up' }],
    ...overrides,
  };
}

// §6.1 join_call 输出（JoinCallOut）
export function joinOut(overrides = {}) {
  const handoffId = overrides.handoff_id || 'H-7f3a';
  return {
    task_id: 'T-100',
    call_id: 'C-1',
    handoff_id: handoffId,
    ws_path: `/ws/handoff/${handoffId}`,
    token: 'tok-1a2b3c',
    expires_at: '2026-10-14T15:05:00Z',
    company_name: 'Lakeshore Energy',
    media: { ai_media: 'disconnected', recording: false, transcription: false },
    ...overrides,
  };
}

// §6.2 AssistantTurnOut
export function turnOut(next, { say = null, probabilities = { prepare_task: 0.82, reply_only: 0.1, ask_clarify: 0.08 }, conversationId = 'conv-1' } = {}) {
  const choice = next.kind === 'tool_call' ? next.name : next.kind;
  return {
    conversation_id: conversationId,
    say,
    next,
    decision: {
      decision_id: `d-${Math.random().toString(16).slice(2, 10)}`,
      point: 'assistant_next_action',
      choice,
      confidence: 0.82,
      probabilities,
      policy_rule: 'auto_high_conf',
    },
    skill: { name: 'assisted-call', commit: COMMIT_A },
  };
}

// 记录调用的 mcp 替身：results[name] = 结构化结果 | Error | (args) => 结果
export function recordingMcp(results = {}) {
  const calls = [];
  return {
    calls,
    async initialize() { return {}; },
    async listTools() { return []; },
    async callTool(name, args) {
      calls.push({ name, args });
      let r = results[name];
      if (Array.isArray(r)) r = r.shift();
      if (typeof r === 'function') r = await r(args);
      if (r instanceof Error) throw r;
      if (r === undefined) throw new Error(`recordingMcp: no result for ${name}`);
      return r;
    },
  };
}

// 可手动推进的定时器替身（status-poller 的 setTimer/clearTimer 注入）
export function manualTimers() {
  const pending = new Map();
  const delays = [];
  let seq = 0;
  return {
    pending,
    delays,
    setTimer(fn, ms) { seq += 1; pending.set(seq, fn); delays.push(ms); return seq; },
    clearTimer(h) { pending.delete(h); },
    async runNext() {
      const first = pending.entries().next();
      if (first.done) return false;
      const [h, fn] = first.value;
      pending.delete(h);
      await fn();
      await settle(3);
      return true;
    },
  };
}

// ---------------------------------------------------------------------------
// 转接音频会话驱动（AC-14、AC-15）
// ---------------------------------------------------------------------------

// 调 joinHandoffAudio，等它建好 WebSocket 后模拟服务器 open；ready=true 时再发 ready 帧并等它返回句柄。
// 不论 joinHandoffAudio 在 open 前、open 后还是 ready 后才 resolve 都能驱动。
export async function openHandoff(joinHandoffAudio, { join, location, deny = false, ready = true, workletUrl } = {}) {
  FakeWebSocket.reset();
  FakeAudioContext.reset();
  FakeAudioWorkletNode.reset();
  const mediaDevices = createFakeMediaDevices({ deny });
  const events = { ready: [], ended: [] };
  const state = { settled: false, handle: null, error: null };
  const opts = {
    join,
    location,
    mediaDevices,
    WebSocketImpl: FakeWebSocket,
    AudioContextImpl: FakeAudioContext,
    AudioWorkletNodeImpl: FakeAudioWorkletNode,
    onReady: (...a) => events.ready.push(a),
    onEnded: (reason) => events.ended.push(reason),
  };
  if (workletUrl !== undefined) opts.workletUrl = workletUrl;
  Promise.resolve()
    .then(() => joinHandoffAudio(opts))
    .then((h) => { state.settled = true; state.handle = h; }, (e) => { state.settled = true; state.error = e; });
  let ws = null;
  for (let i = 0; i < 60; i += 1) {
    await settle(1);
    ws = FakeWebSocket.last() || null;
    if (ws || state.settled) break;
  }
  if (ws && ws.readyState === FakeWebSocket.CONNECTING) {
    ws.serverOpen();
    await settle(3);
  }
  const session = {
    join, mediaDevices, events, state, ws,
    get ctx() { return FakeAudioContext.instances[FakeAudioContext.instances.length - 1] || null; },
    get node() { return FakeAudioWorkletNode.instances[FakeAudioWorkletNode.instances.length - 1] || null; },
  };
  if (ready && ws && !state.error) await sendReady(session);
  else for (let i = 0; i < 5; i += 1) await settle(1);
  return session;
}

export async function sendReady(session, media) {
  assert.ok(session.ws, 'joinHandoffAudio must open a WebSocket');
  session.ws.serverSend({ type: 'ready', company_name: session.join.company_name, media: media || session.join.media });
  for (let i = 0; i < 60 && !session.state.settled; i += 1) await settle(1);
  await settle(2);
}

export function handleOf(session) {
  if (session.state.error) throw session.state.error;
  assert.ok(session.state.settled, 'joinHandoffAudio must resolve once the server is ready');
  assert.ok(session.state.handle && typeof session.state.handle.stop === 'function', 'joinHandoffAudio returns { stop() }');
  return session.state.handle;
}

// 按真实 AudioWorklet 的 128 帧一块，把正弦送进采集节点的 port
export function emitSine(node, { sampleRate, seconds, freq = 440, amplitude = 0.5, chunk = 128 }) {
  const x = float32Sine({ freq, sampleRate, seconds, amplitude });
  for (let i = 0; i < x.length; i += chunk) node.port.emit(x.slice(i, i + chunk));
  return x.length;
}

export function byteLen(d) {
  if (d instanceof ArrayBuffer) return d.byteLength;
  if (ArrayBuffer.isView(d)) return d.byteLength;
  if (d && typeof d.size === 'number') return d.size;
  return -1;
}

export function int16Of(d) {
  if (d instanceof ArrayBuffer) return new Int16Array(d);
  if (ArrayBuffer.isView(d)) return new Int16Array(d.buffer, d.byteOffset, Math.floor(d.byteLength / 2));
  throw new TypeError('binary frame must be an ArrayBuffer or a typed array');
}

// ---------------------------------------------------------------------------
// app.mjs 按钮处理函数（G4 定稿的 ctx）
// ---------------------------------------------------------------------------

// ctx 的环境字段：复位 helpers 的假对象并返回它们（onJoinCall 把它们转交 joinHandoffAudio）
export function handoffEnv({ location = new URL('https://phone-tunnel.example/'), deny = false } = {}) {
  FakeWebSocket.reset();
  FakeAudioContext.reset();
  FakeAudioWorkletNode.reset();
  return {
    location,
    mediaDevices: createFakeMediaDevices({ deny }),
    WebSocketImpl: FakeWebSocket,
    AudioContextImpl: FakeAudioContext,
    AudioWorkletNodeImpl: FakeAudioWorkletNode,
  };
}

// 假转接句柄：记录 stop / setMuted
export function recordingHandle() {
  const h = { stops: 0, muted: [] };
  h.stop = async () => { h.stops += 1; };
  h.setMuted = (m) => { h.muted.push(m); };
  return h;
}

// 把 mcp.callTool 与 mediaDevices.getUserMedia 的调用记到同一条时间线上
export function callLog(mcp, mediaDevices) {
  const log = [];
  const call = mcp.callTool.bind(mcp);
  mcp.callTool = async (name, args) => { log.push(`tool:${name}`); return call(name, args); };
  if (mediaDevices) {
    const gum = mediaDevices.getUserMedia.bind(mediaDevices);
    mediaDevices.getUserMedia = async (c) => { log.push('getUserMedia'); return gum(c); };
  }
  return log;
}
