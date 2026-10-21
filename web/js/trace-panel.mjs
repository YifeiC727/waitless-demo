// debug 追踪面板与 Jev 概率条（C 线；tech §6.1 debug、§6.2 decision；AC-11、AC-12）。纯函数，返回 HTML 字符串。
// r2（第 3 步 C 线缺口定稿）：G9 entries 形状；decisions 由 app 过滤 null；G12 概率条写法。
import { escapeHtml } from './cards.mjs';

// 只有 URL 查询参数 debug 恰为 "1" 时为 true（AC-11）。url 可以是字符串或 URL 对象（含 window.location）。
export function isDebug(url) {
  if (!url) return false;
  try { return new URLSearchParams(typeof url.search === 'string' ? url.search : new URL(url).search).get('debug') === '1'; }
  catch { return false; }
}

// entries：MCP 流量，每项 {kind, label, payload}（mcp-client 的 onTrace 三个参数，G9）；label 与 payload 转义后显示。
// decisions：/assistant/turn 的 decision（顶层 probabilities）与 get_call_status.debug.last_decision（jev.<qid>.probabilities）；
//            last_decision 为 null 时由 app 过滤掉，不传进来（G9；A-09 定稿：取本任务最近一条决策，拨号前也可能有）。
// skills：[{name, commit}]。
// 渲染本轮 skill 名和 commit；对每个决策的每组 probabilities 的每个候选画一条概率条（AC-12）；
// 只有 noul/score 的题不画条。
export function renderTracePanel({ entries = [], decisions = [], skills = [] } = {}) {
  const e = escapeHtml;
  return `<section class="trace-panel"><h2>Developer trace</h2>
    <h3>Skills</h3><ul>${skills.map((s) => `<li>${e(s.name)} <code>${e(s.commit)}</code></li>`).join('')}</ul>
    <h3>Jev decisions</h3>${decisions.filter(Boolean).map((d) => `<article><h4>${e(d.point)} ${e(d.decision_id)}</h4>
      ${d.probabilities ? renderProbabilityBars(d.probabilities) : ''}
      ${Object.entries(d.jev ?? {}).map(([qid, answer]) => `<h5>${e(qid)}</h5>${answer.probabilities ? renderProbabilityBars(answer.probabilities) : `<p>${e(answer.noul ?? answer.score ?? '')}</p>`}`).join('')}
      <p>${e(d.policy_rule ?? d.reason_code)}</p></article>`).join('')}
    <h3>MCP traffic</h3>${entries.map((entry) => `<details><summary>${e(entry.kind)} ${e(entry.label)}</summary><pre>${e(JSON.stringify(entry.payload, null, 2))}</pre></details>`).join('')}
  </section>`;
}

// 单个决策的概率条：probabilities {候选: 概率} → 每个候选一条带内联样式 width:NN% 的元素（NN = 概率 × 100），
// 旁边写候选名（转义）。G12：面板里其他元素的 width:% 样式数量不随决策数变化。
export function renderProbabilityBars(probabilities) {
  return Object.entries(probabilities ?? {}).map(([candidate, probability]) => {
    const width = Number(probability) * 100;
    if (!Number.isFinite(width)) throw new TypeError('Invalid probability');
    return `<div class="probability"><span>${escapeHtml(candidate)}</span><div class="probability-track"><div class="probability-bar" style="width:${width}%"></div></div><span>${escapeHtml(width)}%</span></div>`;
  }).join('');
}
