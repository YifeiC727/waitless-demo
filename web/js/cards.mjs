// 纯函数渲染：返回 HTML 字符串，不碰 DOM、不设定时器（C 线；tech §6.1；AC-07–AC-10、AC-13）。
// 结构与样式沿用原型 Waitless-Alexa-MCP-Demo.html 的卡片类名。
// r2（第 3 步 C 线缺口定稿）：G11 选项截断；G12 被测元素的写法；G13 转接卡不按模型校验输入。
//
// G12 测试据以识别元素的写法（实现必须照此输出，类名、包裹层随意）：
//   - 单选：<input type="radio" value="<选项 id>">；同一道题（或同一张待决选项卡）的 radio 用同一个 name，不同题 name 不同；
//     预选用 checked 属性，禁用用 disabled 属性（布尔属性，出现即生效）。
//   - 按钮：<button>，文字分别是「Start call」（任务卡）、「Confirm」（待决选项卡）；禁用用 disabled 属性。
//   - 来源链接：<a href="<转义后的 url>">转义后的 title</a>，每个来源恰好一条。
//   - 结果卡的两个标题是 h1–h6（或 role="heading"），文字以「Confirmed」「Needs follow-up」开头；
//     某一类为空时可以不出它的标题；每个标题的范围到下一个同级或更高级标题为止。
//   - 所有数据字段都经 escapeHtml 后放进文本或属性。

export function escapeHtml(value) {
  const entities = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  return String(value ?? '').replace(/[&<>"']/g, (char) => entities[char]);
}

// 任务卡（TaskCard）：
//   - 官方号码，并为 company.sources 的每一项渲染一个 <a href>（AC-07）；simulated_line 存在时在来源之后显示转义的演示备注。
//   - precall_decisions：needs_user=false 时 selected ?? default 那一项 checked、可改；needs_user=true 时只有 selected 那一项
//     checked（有 default 也不预选）；任何 needs_user 题 selected 为空时 Start call 按钮 disabled（AC-08）
//   - busy=true（请求进行中）时 Start call 也 disabled
//   - 没有倒计时，不会超时自动开始（AC-08）
export function renderTaskCard(card, { busy = false } = {}) {
  const e = escapeHtml;
  const questions = card.precall_decisions ?? [];
  const blocked = busy || questions.some((q) => q.needs_user && !q.selected);
  return `<section class="task-card" data-card-version="${e(card.card_version)}">
    <h2>${e(card.company.name)}</h2>
    <p class="official-number">${e(card.company.official_number)}</p>
    <div class="sources">${(card.company.sources ?? []).map((s) => `<a href="${e(s.url)}">${e(s.title)}</a>`).join(' ')}</div>${card.simulated_line ? `<p class="demo-notice">${e(card.simulated_line.notice)}</p>` : ''}
    <p>${e(card.goal)}</p><p>${e(card.task_summary)}</p>
    ${card.effective_date ? `<p>Effective date: ${e(card.effective_date)}</p>` : ''}
    <dl>${(card.shared_fields ?? []).map((f) => `<dt>${e(f.label)}</dt><dd>${e(f.display)}</dd>`).join('')}</dl>
    ${questions.map((q) => {
      const selected = q.needs_user ? q.selected : (q.selected ?? q.default);
      return `<fieldset><legend>${e(q.question)}</legend>${q.options.map((o) =>
        `<label class="option"><input type="radio" name="precall-${e(q.question_id)}" data-question-id="${e(q.question_id)}" value="${e(o.id)}"${selected === o.id ? ' checked' : ''}>${e(o.label)}<small>${e(o.detail)}</small></label>`).join('')}</fieldset>`;
    }).join('')}
    <p>${e(card.boundary)}</p>
    <button type="button" data-action="start"${blocked ? ' disabled' : ''}>Start call</button>
  </section>`;
}

// 待决选项卡（PendingChoice）：单选初始全部未选；Confirm 带 disabled；没有倒计时（AC-09）。
// G11：options 超过 3 个时只渲染前 3 个（按原顺序），不报错。
// question_text 是对方原话，只作数据显示（转义后放进文本节点）。
export function renderPendingChoice(choice) {
  const e = escapeHtml;
  return `<section class="pending-card" data-choice-id="${e(choice.choice_id)}"><h2>Your choice</h2>
    <p>${e(choice.question_text)}</p><fieldset><legend>Choose one</legend>
    ${(choice.options ?? []).slice(0, 3).map((o) => `<label class="option"><input type="radio" name="pending-${e(choice.choice_id)}" value="${e(o.id)}"${choice.status !== 'open' ? ' disabled' : ''}>${e(o.label)}<small>${e(o.detail)}</small></label>`).join('')}
    </fieldset><button type="button" data-action="confirm" disabled>Confirm</button></section>`;
}

// 结果卡（CaseResultOut）：verified 只出现在「Confirmed」标题下，follow_ups 只出现在「Needs follow-up」标题下（AC-10）。
export function renderResult(result) {
  const e = escapeHtml;
  return `<section class="result-card"><h2>${e(result.headline)}</h2><p>${e(result.company_name)}</p>
    <p>${e(result.summary)}</p>${result.confirmation_number ? `<p>Confirmation: ${e(result.confirmation_number)}</p>` : ''}
    <h3>Confirmed</h3><ul>${(result.verified ?? []).map((v) => `<li><strong>${e(v.label)}</strong>: ${e(v.value)}</li>`).join('')}</ul>
    <h3>Needs follow-up</h3><ul>${(result.follow_ups ?? []).map((f) => `<li><strong>${e(f.label)}</strong>: ${e(f.detail)}</li>`).join('')}</ul>
    <h3>Call history</h3><ul>${(result.history ?? []).map((h) => `<li>${e(h.t)} — ${e(h.title)}: ${e(h.detail)}</li>`).join('')}</ul>
  </section>`;
}

// 转接聊天卡：用 join_call 返回的 media 三个字段生成文案（AC-13）：
//   ai_media === 'disconnected' → 「AI is not listening」；recording === false → 「Not recording」；
//   transcription === false → 「Not transcribing」；字段取其他值时对应那句不出现。
// G13：AC-13 的测试会传入 recording:true、transcription:true，这不符合 models.JoinMedia（两项是 Literal False），
// 渲染函数只按字段值出文案，不按模型校验输入、不报错。
export function renderHandoffCard(join) {
  const media = join.media ?? {};
  const lines = [];
  if (media.ai_media === 'disconnected') lines.push('AI is not listening');
  if (media.recording === false) lines.push('Not recording');
  if (media.transcription === false) lines.push('Not transcribing');
  return `<section class="handoff-card"><h2>Call with ${escapeHtml(join.company_name)}</h2>
    ${lines.map((line) => `<p>${line}</p>`).join('')}
    <button type="button" data-action="mute" aria-pressed="false">Mute</button>
    <button type="button" data-action="return">Return to Waitless</button></section>`;
}

// 当前状态一行（get_call_status.status_line 与 phase），替换旧状态，不累积。
export function renderStatus(status) {
  return `<p class="call-status" role="status" data-phase="${escapeHtml(status.phase)}">${escapeHtml(status.status_line)}</p>`;
}
