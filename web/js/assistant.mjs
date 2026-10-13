// 助手循环：只执行服务端给的 next（C 线；tech §6.2；AC-05、AC-06；D11、K11）。
// 网页里没有选工具的逻辑：下一步永远来自 POST /assistant/turn 的 next。
// r2（第 3 步 C 线缺口定稿）：G7 出错时 structured 的内容；G8 用户动作工具对应的 ui；hooks.onToolResult。

// 这 4 个工具只由按钮的点击处理函数调用（app.mjs 的 onStartCall、onConfirmChoice、onJoinCall、onReturn）。
export const USER_ACTION_TOOLS = Object.freeze([
  'start_assisted_call',
  'answer_pending_choice',
  'join_call',
  'return_to_agent',
]);

// G8：next 里出现用户动作工具时，按它对应的界面交给 hooks.onAwaitUser（等用户点按钮）。
export const USER_ACTION_UI = Object.freeze({
  start_assisted_call: 'task_card',
  answer_pending_choice: 'pending_choice',
  join_call: 'handoff',
  return_to_agent: 'handoff',
});

// POST /assistant/turn（相对路径），body 为 §6.2 AssistantTurnIn，返回解析后的 AssistantTurnOut。
// content-type: application/json；非 2xx 抛错。
export async function postTurn(body, { fetchImpl, endpoint = '/assistant/turn' } = {}) {
  throw new Error('not implemented');
}

// 处理一次 /assistant/turn 的结果，循环直到 await_user / poll / done，不设定时器：
//   每个 AssistantTurnOut（含传进来的第一个）：say 非空时 hooks.onSay(say)；hooks.onDecision(decision)（追踪面板画概率条，AC-12）
//   next.kind === 'tool_call' 且不在 USER_ACTION_TOOLS → mcp.callTool(name, arguments)（参数原样），得到
//       toolResult = {tool_use_id, name, structured, is_error}：
//         成功：structured = callTool 的返回值，is_error=false
//         失败（G7）：structured = {code, message}，is_error=true；McpError 取 err.code、err.message，
//                    其他异常（断网、HTTP 失败）code 为 'transport_error'、message 为 err.message
//       先 hooks.onToolResult(toolResult)（app 据此出卡、更新 state，见 app.mjs cardForToolResult），
//       再 postTurn({conversation_id, tool_result: toolResult, tools}) 继续下一步（AC-05）
//   next.kind === 'tool_call' 且属于 USER_ACTION_TOOLS → 不执行、不回传，hooks.onAwaitUser(USER_ACTION_UI[name])，停（AC-06）
//   next.kind === 'await_user' → hooks.onAwaitUser(ui)，停
//   next.kind === 'poll'       → hooks.onPoll(task_id)（交给 status-poller），停
//   next.kind === 'done'       → 停
// 返回最后一次的 AssistantTurnOut。hooks 各项都可省略。
export async function runAssistantStep(turnOut, { mcp, postTurn: post, conversationId, tools, hooks = {} } = {}) {
  throw new Error('not implemented');
}
