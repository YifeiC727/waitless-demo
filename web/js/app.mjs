// 启动、视图切换、事件绑定（C 线；tech §6.1、§6.2、§8；AC-05、AC-06、AC-07、AC-14）。
// 用户动作工具（start_assisted_call、answer_pending_choice、join_call、return_to_agent）只在下面 4 个按钮处理函数里调用，
// 处理函数体内直接写工具名字面量（AC-06 的源码检查据此核对位置）。
// r2（第 3 步 C 线缺口定稿）：G4 处理函数的 ctx；新增 cardForToolResult（A-14 非 ready 不出卡）与 onLeaveCallPage（AC-14）。
//
// 按钮处理函数的 ctx（G4）：
//   mcp       createMcpClient() 的返回值
//   state     { task_id, card_version, choice_id, option_id, handoff_id }：app 维护的当前值，处理函数可以改写
//             （onJoinCall 写入 handoff_id）
//   handoff   joinHandoffAudio() 返回的句柄 { stop(), setMuted() }；没有转接时为 null。onJoinCall 写入，
//             onReturn、onLeaveCallPage 停掉后置为 null
//   以及 boot(ctx) 收到的环境字段（onJoinCall 转交 joinHandoffAudio）：location、mediaDevices、WebSocketImpl、
//   AudioContextImpl、AudioWorkletNodeImpl
//   show(html) 可选：把卡片 HTML 放进当前视图（boot 用 document 实现；测试里记录调用）
// 每个处理函数只调用自己的那一个工具，不调其他工具；工具出错时抛出 McpError（由 app 显示）。

// boot(ctx)：GET /app-config（相对路径）→ createMcpClient → initialize → listTools → 绑定聊天输入与按钮；
// debug=1 时插入 Developer trace 入口（AC-11）；pagehide 与页内离开通话视图时调 onLeaveCallPage。
// ctx = { document, location, fetchImpl, WebSocketImpl, mediaDevices, AudioContextImpl, AudioWorkletNodeImpl }
export async function boot(ctx) {
  throw new Error('not implemented');
}

// 一次工具结果（runAssistantStep 的 hooks.onToolResult 收到的 {tool_use_id, name, structured, is_error}）对应要放进
// 聊天区的卡片 HTML；不该出卡时返回 null。纯函数。
//   is_error=true → null
//   prepare_task、update_task：status === 'ready' 且 task_card 非空 → renderTaskCard(task_card, {busy})；
//                              setup_required、unsupported 或 task_card 为空 → null（A-14、A-19 定稿：非 ready 不出卡，
//                              只显示服务端 say 的说明）
//   get_case_result → renderResult(structured)
//   其他工具 → null
// app 同时用 ready 的结果更新 state.task_id、state.card_version。
export function cardForToolResult(toolResult, { busy = false } = {}) {
  throw new Error('not implemented');
}

// Start call 按钮：start_assisted_call({task_id, confirmed: true, card_version})（R13 网页侧守门）。
// state.task_id 或 state.card_version 缺失时不调用，返回 null。返回 StartCallOut。
export async function onStartCall(ctx) {
  throw new Error('not implemented');
}

// 待决选项 Confirm 按钮：answer_pending_choice({task_id, choice_id, option_id})。
// state.option_id 为空（用户还没选）时不调用，返回 null（Confirm 本就禁用，这里再守一道，AC-09）。返回 AnswerChoiceOut。
export async function onConfirmChoice(ctx) {
  throw new Error('not implemented');
}

// Join 按钮：join_call({task_id}) → state.handoff_id = join.handoff_id → show(renderHandoffCard(join))
// → ctx.handoff = await joinHandoffAudio({join, location, mediaDevices, WebSocketImpl, AudioContextImpl, AudioWorkletNodeImpl, ...})。
// getUserMedia 只在 join_call 成功之后、在这次点击里调用一次（AC-14；AudioContext 也在这次点击里创建，NFR-6）。
// join_call 失败时不请求麦克风。返回 JoinCallOut。
export async function onJoinCall(ctx) {
  throw new Error('not implemented');
}

// Return to Waitless 按钮：return_to_agent({task_id, handoff_id})，并 stop() 转接音频、ctx.handoff = null；
// return_to_agent 失败也照样停掉音频，再把错误抛出。返回 ReturnOut。
export async function onReturn(ctx) {
  throw new Error('not implemented');
}

// 离开通话页（pagehide、页内返回聊天视图）：stop() 转接音频、ctx.handoff = null（AC-14）。
// 不调用任何 MCP 工具：return_to_agent 是用户动作工具，只由 Return 按钮调用；WebSocket 不经 return 断开后，
// 引擎满 handoff.resume_grace_ms 自动接回（§6.4、AC-78）。没有转接时什么都不做；可重复调用。
export async function onLeaveCallPage(ctx) {
  throw new Error('not implemented');
}
