// 轮询 get_call_status（C 线；tech §6.1、§6.2 next.kind=poll）。间隔取 GET /app-config 的 poll_ms，不写死。
// r2（第 3 步 C 线缺口定稿）：G5 加 location 参数。

// 返回 { start(), stop(), pollOnce() }。setTimer/clearTimer 可注入（测试用假定时器）。
//   location：页面地址（URL 或 window.location），缺省读 globalThis.location；没有时视为非 debug。
//   pollOnce()：调一次 mcp.callTool('get_call_status', {task_id: taskId, debug: isDebug(location)})，onStatus(status)，
//               status.ended 为真时 stop() 并 onEnded()；返回 status。
//   start()：用 setTimer(fn, pollMs) 逐次排下一轮（每轮间隔都是 pollMs）；stop()：clearTimer 待发的那一轮，之后不再轮询。
// 轮询器只读状态：pending_choice 出现、handoff.state=ready 时只交给 onStatus（app 显示待决选项卡或转接卡），
// 绝不代用户调 answer_pending_choice、join_call（AC-06）。
// ended=true 后 app 以 event=call_ended 走一轮 /assistant/turn（§8 第 12 步）。
export function createStatusPoller({
  mcp,
  taskId,
  pollMs,
  onStatus,
  onEnded,
  location = globalThis.location,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
} = {}) {
  return {
    start() { throw new Error('not implemented'); },
    stop() { throw new Error('not implemented'); },
    async pollOnce() { throw new Error('not implemented'); },
  };
}
