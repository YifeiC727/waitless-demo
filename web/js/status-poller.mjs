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
  if (!Number.isFinite(pollMs) || pollMs <= 0) throw new Error('poll_ms must be a positive number');
  const debug = new URLSearchParams(location?.search ?? (location ? new URL(location).search : '')).get('debug') === '1';
  let running = false;
  let timer = null;
  let generation = 0;
  let ended = false;
  let inFlight;
  function stop() {
    running = false;
    generation += 1;
    if (timer !== null) clearTimer(timer);
    timer = null;
  }
  async function pollOnce() {
    if (inFlight) return inFlight;
    const currentGeneration = generation;
    inFlight = (async () => {
      const status = await mcp.callTool('get_call_status', { task_id: taskId, debug });
      if (currentGeneration !== generation) return status;
      await onStatus?.(status);
      if (status.ended && !ended) {
        ended = true;
        stop();
        await onEnded?.();
      }
      return status;
    })();
    try { return await inFlight; } finally { inFlight = null; }
  }
  function schedule() {
    if (!running || ended) return;
    timer = setTimer(async () => {
      timer = null;
      try { await pollOnce(); }
      catch (error) { stop(); throw error; }
      schedule();
    }, pollMs);
  }
  return {
    start() { if (!running && !ended) { running = true; schedule(); } },
    stop,
    pollOnce,
  };
}
