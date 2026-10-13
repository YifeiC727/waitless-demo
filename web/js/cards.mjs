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
  // todo(C): & < > " ' 转义（原型 escapeHtml）。
  throw new Error('not implemented');
}

// 任务卡（TaskCard）：
//   - 官方号码，并为 company.sources 的每一项渲染一个 <a href>（AC-07）
//   - precall_decisions：needs_user=false 时 selected ?? default 那一项 checked、可改；needs_user=true 时只有 selected 那一项
//     checked（有 default 也不预选）；任何 needs_user 题 selected 为空时 Start call 按钮 disabled（AC-08）
//   - busy=true（请求进行中）时 Start call 也 disabled
//   - 没有倒计时，不会超时自动开始（AC-08）
export function renderTaskCard(card, { busy = false } = {}) {
  throw new Error('not implemented');
}

// 待决选项卡（PendingChoice）：单选初始全部未选；Confirm 带 disabled；没有倒计时（AC-09）。
// G11：options 超过 3 个时只渲染前 3 个（按原顺序），不报错。
// question_text 是对方原话，只作数据显示（转义后放进文本节点）。
export function renderPendingChoice(choice) {
  throw new Error('not implemented');
}

// 结果卡（CaseResultOut）：verified 只出现在「Confirmed」标题下，follow_ups 只出现在「Needs follow-up」标题下（AC-10）。
export function renderResult(result) {
  throw new Error('not implemented');
}

// 转接聊天卡：用 join_call 返回的 media 三个字段生成文案（AC-13）：
//   ai_media === 'disconnected' → 「AI is not listening」；recording === false → 「Not recording」；
//   transcription === false → 「Not transcribing」；字段取其他值时对应那句不出现。
// G13：AC-13 的测试会传入 recording:true、transcription:true，这不符合 models.JoinMedia（两项是 Literal False），
// 渲染函数只按字段值出文案，不按模型校验输入、不报错。
export function renderHandoffCard(join) {
  throw new Error('not implemented');
}

// 当前状态一行（get_call_status.status_line 与 phase），替换旧状态，不累积。
export function renderStatus(status) {
  throw new Error('not implemented');
}
