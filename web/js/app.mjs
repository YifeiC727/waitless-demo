// 启动、视图切换、事件绑定（C 线；tech §6.1、§6.2、§8；AC-05、AC-06、AC-07、AC-14）。
// 用户动作工具只在对应按钮处理函数里调用（含 v5.27 的挂断、结果提交），
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
import { createMcpClient } from './mcp-client.mjs';
import { postTurn, runAssistantStep } from './assistant.mjs';
import { escapeHtml, renderTaskCard, renderPendingChoice, renderResult, resultCallId, renderHandoffReady, renderHandoffCard, renderStatus, renderListenControl } from './cards.mjs';
import { createStatusPoller } from './status-poller.mjs';
import { joinHandoffAudio } from './handoff-audio.mjs';
import { startListenAudio, listenAllowed, LISTEN_STOP_PHASES } from './listen-audio.mjs';
import { isDebug, renderTracePanel } from './trace-panel.mjs';
import { startSimulatedVoice } from './voice.mjs';

// boot(ctx)：GET /app-config（相对路径）→ createMcpClient → initialize → listTools → 绑定聊天输入与按钮；
// debug=1 时插入 Developer trace 入口（AC-11）；pagehide 与页内离开通话视图时调 onLeaveCallPage。
// ctx = { document, location, fetchImpl, WebSocketImpl, mediaDevices, AudioContextImpl, AudioWorkletNodeImpl }
export async function boot(ctx) {
  const document = ctx.document ?? globalThis.document;
  const page = ctx.location ?? globalThis.location;
  const fetchImpl = ctx.fetchImpl ?? globalThis.fetch;
  ctx.fetchImpl = fetchImpl;
  const configResponse = await fetchImpl('/app-config');
  if (!configResponse.ok) throw new Error(`App config HTTP ${configResponse.status}`);
  const config = await configResponse.json();
  if (!config.mode || !Number.isFinite(config.poll_ms) || config.poll_ms <= 0) throw new Error('Missing or invalid app configuration');
  const query = (selector) => document.querySelector(selector);
  const messages = query('#messages');
  const callView = query('#callView');
  const chatView = query('#chatView');
  const traceView = query('#traceView');
  const debug = isDebug(page);
  const entries = [], decisions = [], skills = new Map();
  const state = ctx.state = { conversation_id: globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`, task_id: null, card_version: null, choice_id: null, option_id: null, handoff_id: null };
  ctx.location = page;
  ctx.mediaDevices ??= globalThis.navigator?.mediaDevices;
  ctx.WebSocketImpl ??= globalThis.WebSocket;
  ctx.AudioContextImpl ??= globalThis.AudioContext ?? globalThis.webkitAudioContext;
  ctx.AudioWorkletNodeImpl ??= globalThis.AudioWorkletNode;
  // Persist only the task pointer. Results and eligibility are read from the
  // server after refresh; private keypad input and form answers stay in memory.
  let storage = ctx.storage;
  try { storage ??= document.defaultView?.sessionStorage ?? globalThis.sessionStorage; } catch { /* Storage may be unavailable. */ }
  function rememberTask(taskId) { try { storage?.setItem('waitless.task_id', taskId); } catch { /* Optional recovery. */ } }
  ctx.handoff = null;
  ctx.listen = null;
  let lastStatus = null;
  function renderListen() {
    slot('listenControl', ctx.listen || listenAllowed(config.mode, lastStatus) ? renderListenControl({ listening: !!ctx.listen }) : null);
  }
  ctx._onListenEnded = renderListen;
  let poller, pollingTask, voice, busy = false, disposed = false;
  // Assistant next cannot revive an ended call or undo a page-leave cancellation.
  let endedTask = null, pollingPaused = false;
  let pendingSignature = null, handoffSignature = null;
  const notifiedGenerations = new Set();
  let resultData = null, resultDraft = {}, resultSequence = 0;
  // Include controls rendered after an action starts; preserve each control's
  // own eligibility so unselected choices stay disabled when the action ends.
  const busyControls = new Map();
  function disableBusyControls() {
    if (!busy) return;
    for (const control of document.querySelectorAll?.('button, input') ?? []) {
      if (!busyControls.has(control)) busyControls.set(control, control.disabled);
      control.disabled = true;
    }
  }
  function setControlDisabled(control, disabled) {
    if (busy) busyControls.set(control, disabled);
    control.disabled = busy || disabled;
  }
  const bindings = [];
  const bind = (target, type, listener) => {
    if (!target?.addEventListener) return;
    target.addEventListener(type, listener);
    bindings.push(() => target.removeEventListener(type, listener));
  };
  const errorBox = query('#error');
  function showError(error) { if (errorBox) { errorBox.hidden = false; errorBox.textContent = error.message ?? String(error); } }
  function append(html) {
    if (disposed) return;
    messages?.insertAdjacentHTML('beforeend', html);
    disableBusyControls();
    const chat = query('#chat');
    if (chat) chat.scrollTop = chat.scrollHeight;
  }
  function slot(id, html) {
    if (disposed) return;
    const current = query(`#${id}`);
    if (current) { if (html === null) current.remove(); else current.innerHTML = html; }
    else if (html !== null) append(`<div id="${id}" class="tool-card">${html}</div>`);
    disableBusyControls();
  }
  function showTrace() {
    if (debug && traceView) traceView.innerHTML = renderTracePanel({ entries, decisions, skills: [...skills.values()] });
  }
  function addSkills(items = []) { for (const skill of items) skills.set(skill.name, skill); }
  function addDecision(decision, skill) {
    if (skill) addSkills([skill]);
    if (decision && !decisions.some((d) => d.decision_id === decision.decision_id)) decisions.push(decision);
    showTrace();
  }
  function displayChat() {
    if (chatView) chatView.hidden = false;
    if (callView) callView.hidden = true;
  }
  ctx.show = (html) => {
    voice?.cancel();
    if (callView) { callView.hidden = false; callView.innerHTML = `<button type="button" data-action="leave">Back to chat</button>${html}`; }
    disableBusyControls();
    if (chatView) chatView.hidden = true;
  };
  const mcp = ctx.mcp = createMcpClient({ fetchImpl, onTrace(kind, label, payload) {
    entries.push({ kind, label, payload });
    showTrace();
  } });
  await mcp.initialize();
  const tools = (await mcp.listTools()).map(({ name, description }) => ({ name, description }));
  ctx.tools = tools;
  const hooks = {
    onSay: (text) => append(`<div class="msg assistant"><div class="bubble"><div class="assistant-label">Alexa+ simulation</div>${escapeHtml(text)}</div></div>`),
    onDecision: addDecision,
    onAwaitUser: () => {},
    onToolResult: acceptToolResult,
    onPoll: startPolling,
  };
  async function turn(fields = {}) {
    if (disposed) return;
    const out = await postTurn({ conversation_id: state.conversation_id, tools, ...(state.task_id ? { task_id: state.task_id } : {}), ...fields }, { fetchImpl });
    if (disposed) return;
    return runAssistantStep(out, { mcp, postTurn: (body) => postTurn(body, { fetchImpl }), conversationId: state.conversation_id, tools, hooks });
  }
  let userToolSequence = 0;
  function turnWithUserToolResult(name, structured) {
    return turn({ tool_result: {
      tool_use_id: `${state.conversation_id}:user:${++userToolSequence}`,
      name, structured, is_error: false,
    } });
  }
  ctx._afterStart = (name, structured) => {
    slot('taskCard', null);
    endedTask = null;
    pollingPaused = false;
    state.call_id = structured.call_id;
    ctx._rejoining = false;
    ctx._hangupTarget = null;
    resultData = null;
    resultDraft = {};
    resultSequence += 1;
    slot('resultCard', null);
    slot('hangupNotice', null);
    // A successful user action starts status reads before waiting for assistant next.
    if (!structured.ended && structured.status !== 'ended') startPolling(structured.task_id);
    return turnWithUserToolResult(name, structured);
  };
  ctx._afterReturn = (name, structured) => {
    displayChat();
    endedTask = null;
    pollingPaused = false;
    if (!structured.ended && structured.status !== 'ended') startPolling(structured.task_id);
    return turnWithUserToolResult(name, structured);
  };
  function showResult(data, error) {
    if (data.task_id && state.task_id !== data.task_id) return;
    const callId = resultCallId(data);
    if (resultData?.result_source === 'user' && !resultData.awaiting_user_result &&
        resultCallId(resultData) === callId && data.awaiting_user_result) return;
    if (!resultData || resultCallId(resultData) !== callId) resultDraft = {};
    resultData = { ...data, task_id: state.task_id };
    resultDraft.error = error;
    slot('resultCard', renderResult(resultData, resultDraft));
  }
  async function refreshResult(taskId = state.task_id, error) {
    const sequence = ++resultSequence;
    const data = await mcp.callTool('get_case_result', { task_id: taskId });
    if (disposed || taskId !== state.task_id || sequence !== resultSequence) return;
    showResult(data, error);
  }
  ctx._afterHangup = async (result, error) => {
    displayChat();
    const taskId = ctx._hangupTarget?.task_id;
    if (disposed || taskId !== state.task_id) return;
    const retry = error || result.remote_hangup_pending;
    slot('hangupNotice', retry ? `<p role="status">${error ? 'Hang up failed. Please retry.' : 'Call ended locally. Remote hangup pending.'}</p><button type="button" data-action="hangup" data-call-id="${escapeHtml(ctx._hangupTarget.call_id)}">Retry hang up</button>` : null);
    pollingPaused = false;
    if (!endedTask) startPolling(taskId);
    if (result && config.mode === 'live-sim') {
      try { await refreshResult(taskId); } catch (failure) { showError(failure); }
    }
  };
  ctx._onHandoffEnded = () => {
    if (disposed) return;
    ctx._rejoining = true;
    handoffSignature = null;
    displayChat();
    if (!document.hidden) {
      pollingPaused = false;
      startPolling(state.task_id);
      void poller?.pollOnce().catch(showError);
    }
  };
  function acceptToolResult(result) {
    if (disposed || result.is_error) return;
    const data = result.structured;
    if (result.name === 'prepare_task' || result.name === 'update_task') {
      if (data.task_id !== state.task_id) {
        poller?.stop();
        poller = null;
        pollingTask = null;
        endedTask = null;
        pollingPaused = false;
        pendingSignature = null;
        handoffSignature = null;
        notifiedGenerations.clear();
        resultData = null;
        resultDraft = {};
        resultSequence += 1;
        ctx._rejoining = false;
        ctx._hangupTarget = null;
        state.choice_id = null;
        state.option_id = null;
        state.handoff_id = null;
        for (const id of ['callStatus', 'listenControl', 'transcript', 'pendingCard', 'handoffReady', 'resultCard', 'hangupNotice']) slot(id, null);
        void onLeaveCallPage(ctx);
        displayChat();
      }
      state.task_id = data.task_id;
      rememberTask(state.task_id);
      state.task_card = data.status === 'ready' ? data.task_card : null;
      state.card_version = state.task_card?.card_version ?? null;
      addSkills(data.skills);
      slot('taskCard', cardForToolResult(result));
      if (data.status !== 'ready' && data.missing?.length) {
        append(`<p class="notice">Please provide: ${data.missing.map((f) => escapeHtml(f.label)).join(', ')}. You can type these details below.</p>`);
      }
    } else if (result.name === 'get_case_result') {
      showResult(data);
      rememberTask(state.task_id);
      slot('pendingCard', null);
      slot('handoffReady', null);
      displayChat();
    } else if (result.name === 'get_call_status') acceptStatus(data);
    showTrace();
  }
  async function acceptStatus(status) {
    if (disposed) return;
    if (status.task_id && status.task_id !== state.task_id) return;
    state.call_id = status.call_id ?? state.call_id;
    if (ctx._hangupTarget?.call_id && status.call_id && ctx._hangupTarget.call_id !== status.call_id) {
      ctx._hangupTarget = null;
      slot('hangupNotice', null);
    }
    slot('callStatus', renderStatus(status));
    lastStatus = status;
    if (ctx.listen && (status.ended || LISTEN_STOP_PHASES.includes(status.phase))) void onStopListening(ctx);
    renderListen();
    slot('transcript', status.phase === 'in_handoff' ? null : status.transcript_tail?.length ? status.transcript_tail.map((line) => `<p><strong>${escapeHtml(line.speaker)}</strong>: ${escapeHtml(line.text)}</p>`).join('') : null);
    if (debug && status.debug) { addSkills(status.debug.skills); addDecision(status.debug.last_decision); }
    const pending = status.pending_choice?.status === 'open' ? status.pending_choice : null;
    const signature = pending ? JSON.stringify(pending) : null;
    if (signature !== pendingSignature) {
      pendingSignature = signature;
      state.choice_id = pending?.choice_id ?? null;
      state.option_id = null;
      slot('pendingCard', pending ? renderPendingChoice(pending) : null);
    }
    const handoff = status.handoff;
    if ((ctx.handoff || ctx._joining) && handoff?.state === 'ready' &&
        ctx._serverHandoffGeneration !== undefined && handoff.generation !== undefined &&
        handoff.generation !== ctx._serverHandoffGeneration) {
      await onLeaveCallPage(ctx);
      ctx._rejoining = true;
      displayChat();
    }
    state.handoff_generation = handoff?.generation;
    const handoffKey = handoff?.state === 'ready' ? JSON.stringify(handoff) : null;
    if (handoffKey !== handoffSignature) {
      handoffSignature = handoffKey;
      slot('handoffReady', handoffKey ? renderHandoffReady(handoff, { mode: config.mode, reconnecting: ctx._rejoining }) : null);
      if (handoffKey) {
        const notification = `${state.task_id}:${status.call_id ?? ''}:${handoff.generation ?? 0}`;
        if (config.mode !== 'live-sim' || !notifiedGenerations.has(notification)) {
          notifiedGenerations.add(notification);
          try { globalThis.navigator?.vibrate?.(200); } catch { /* Vibration is optional. */ }
        }
      }
    }
    if ((ctx.handoff || ctx._joining) && (!handoff || handoff.state === 'ended' || status.ended)) {
      await onLeaveCallPage(ctx);
      displayChat();
    }
  }
  function startPolling(taskId) {
    if (!taskId || disposed || pollingPaused || endedTask === taskId) return;
    if (poller && pollingTask === taskId) { poller.start(); return; }
    poller?.stop();
    pollingTask = taskId;
    state.task_id = taskId;
    rememberTask(taskId);
    poller = createStatusPoller({ mcp, taskId, pollMs: config.poll_ms, location: page, onStatus: acceptStatus,
      onEnded: async () => {
        endedTask = taskId;
        poller = null;
        pollingTask = null;
        await onLeaveCallPage(ctx);
        displayChat();
        if (config.mode === 'live-sim') {
          try { await refreshResult(taskId); } catch (error) { showError(error); }
        }
        await turn({ event: 'call_ended' });
      },
    });
    poller.start();
  }
  async function action(work) {
    if (busy || disposed) return;
    busy = true;
    if (errorBox) errorBox.hidden = true;
    disableBusyControls();
    try { await work(); } catch (error) { showError(error); }
    finally {
      busy = false;
      for (const [control, disabled] of busyControls) {
        if (control.isConnected) control.disabled = disabled;
      }
      busyControls.clear();
    }
  }
  async function submit(text) {
    if (!text.trim()) return;
    await action(async () => {
      voice?.cancel();
      query('#hero')?.remove();
      append(`<div class="msg user"><div class="bubble">${escapeHtml(text)}</div></div>`);
      if (query('#prompt')) query('#prompt').value = '';
      await turn({ user_text: text });
    });
  }
  bind(query('#composer'), 'submit', (event) => { event.preventDefault(); void submit(query('#prompt')?.value ?? ''); });
  bind(document, 'change', (event) => {
    const input = event.target;
    if (input.dataset?.resultOutcome && resultData?.awaiting_user_result && !resultDraft.submitting) {
      if (!['confirmed', 'failed', 'needs_follow_up'].includes(input.value)) return;
      resultDraft.outcome = input.value;
      const submitResult = query('#resultCard [data-action="report-result"]');
      if (submitResult) setControlDisabled(submitResult, false);
    } else if (input.matches?.('#pendingCard input[type="radio"]')) {
      state.option_id = input.value;
      const confirm = query('#pendingCard [data-action="confirm"]');
      if (confirm) setControlDisabled(confirm, false);
    } else if (input.dataset?.questionId && state.task_card) {
      const question = state.task_card.precall_decisions.find((q) => q.question_id === input.dataset.questionId);
      if (!question?.options.some((o) => o.id === input.value)) return;
      void action(async () => {
        const structured = await mcp.callTool('update_task', { task_id: state.task_id, decisions: { [question.question_id]: input.value } });
        acceptToolResult({ name: 'update_task', structured, is_error: false });
      });
    }
  });
  bind(document, 'input', (event) => {
    if (event.target === query('#resultConfirmation') && resultData?.awaiting_user_result && !resultDraft.submitting) resultDraft.confirmation_number = event.target.value;
  });
  bind(document, 'click', (event) => {
    const button = event.target.closest?.('button');
    if (!button || button.disabled) return;
    const name = button.dataset.action;
    if (name === 'start') void action(async () => { await onStartCall(ctx); });
    else if (name === 'confirm') void action(async () => { const result = await onConfirmChoice(ctx); if (result) { state.option_id = null; slot('pendingCard', null); pendingSignature = null; if (result.status === 'expired') append('<p class="notice">This choice has expired.</p>'); } });
    else if (name === 'join') void action(async () => { await onJoinCall(ctx); });
    else if (name === 'key') {
      if (!ctx.handoff?.sendKeys(button.dataset.key)) {
        const feedback = query('#callView .keypad-feedback');
        if (feedback) feedback.textContent = 'Key not sent';
      }
    }
    else if (name === 'listen') void action(async () => { await onListen(ctx); renderListen(); });
    else if (name === 'stop-listening') { void onStopListening(ctx); renderListen(); }
    else if (name === 'return') void action(async () => { try { await onReturn(ctx); } finally { displayChat(); } });
    else if (name === 'hangup') void action(async () => {
      if (button.isConnected === false) return;
      if (button.dataset.callId && button.dataset.callId !== ctx._hangupTarget?.call_id) return;
      await onHangup(ctx);
    });
    else if (name === 'report-result') void action(async () => {
      const taskId = state.task_id, callId = resultCallId(resultData ?? {});
      if (!resultData?.awaiting_user_result || resultDraft.submitting || !resultDraft.outcome ||
          button.dataset.callId !== callId || button.dataset.taskId !== taskId || button.isConnected === false) return;
      const submittedDraft = resultDraft;
      resultSequence += 1;
      resultDraft.submitting = true;
      resultDraft.error = null;
      resultDraft.confirmation_number = query('#resultConfirmation')?.value ?? resultDraft.confirmation_number ?? '';
      slot('resultCard', renderResult(resultData, resultDraft));
      try {
        const saved = await onReportResult(ctx, { call_id: callId, outcome: submittedDraft.outcome, confirmation_number: submittedDraft.confirmation_number });
        if (disposed || state.task_id !== taskId || resultCallId(resultData ?? {}) !== callId) return;
        // A same-call status/result read may overlap this write. Invalidate its
        // older snapshot, but keep a successful write bound to the current call.
        resultSequence += 1;
        showResult({ ...resultData, ...saved });
      } catch (error) {
        if (disposed || state.task_id !== taskId || resultCallId(resultData ?? {}) !== callId) return;
        submittedDraft.submitting = false;
        if (error.code === 'result_conflict') await refreshResult(taskId, 'This result was already submitted or changed. The stored result is shown.');
        else {
          submittedDraft.error = error.code === 'invalid_option' ? 'Check the confirmation number format and try again.' : error.code === 'result_not_allowed' ? 'This call is no longer eligible. Refresh the page to see its result.' : 'Result could not be submitted. Please retry.';
          slot('resultCard', renderResult(resultData, submittedDraft));
        }
      } finally { submittedDraft.submitting = false; }
    });
    else if (name === 'leave') { pollingPaused = true; poller?.stop(); void onLeaveCallPage(ctx); displayChat(); }
    else if (name === 'mute') { const muted = button.getAttribute('aria-pressed') !== 'true'; ctx.handoff?.setMuted(muted); button.setAttribute('aria-pressed', String(muted)); button.textContent = muted ? 'Unmute' : 'Mute'; }
    else if (name === 'trace' && debug) { showTrace(); traceView.hidden = !traceView.hidden; }
    else if (name === 'voice') { voice?.cancel(); voice = startSimulatedVoice({ text: config.replay_request_text ?? (query('#prompt')?.value || query('#electricSuggestion')?.dataset.request || ''), insertText: (text) => { if (query('#prompt')) query('#prompt').value = text; }, showLabel: (text) => { if (query('#voiceLabel')) query('#voiceLabel').textContent = text; } }); }
    else if (name === 'reset') {
      try { storage?.removeItem('waitless.task_id'); } catch { /* Storage may be unavailable. */ }
      void dispose(); page.reload?.();
    }
    else if (button.dataset.request) void submit(button.dataset.request);
  });
  if (query('#mode')) query('#mode').textContent = config.mode;
  if (config.replay_request_text && query('#prompt')) query('#prompt').value = config.replay_request_text;
  if (debug && query('#topActions')) query('#topActions').insertAdjacentHTML('beforeend', '<button type="button" data-action="trace">Developer trace</button>');
  const pageEvents = document.defaultView ?? globalThis.window;
  bind(pageEvents, 'pagehide', () => { void dispose(); });
  bind(document, 'visibilitychange', () => {
    if (document.hidden) {
      if (ctx.handoff || ctx._joining) ctx._rejoining = true;
      pollingPaused = true; poller?.stop(); voice?.cancel(); void onLeaveCallPage(ctx); displayChat();
      handoffSignature = null;
    } else if (state.task_id) {
      pollingPaused = false;
      startPolling(state.task_id);
      void poller?.pollOnce().catch(showError);
    }
  });
  bind(pageEvents, 'unhandledrejection', (event) => { showError(event.reason); event.preventDefault(); });
  async function dispose() {
    if (disposed) return;
    disposed = true;
    voice?.cancel();
    poller?.stop();
    bindings.splice(0).forEach((unbind) => unbind());
    await onLeaveCallPage(ctx);
  }
  ctx.submit = submit;
  ctx.dispose = dispose;
  ctx.config = config;
  let restoredTask;
  try { restoredTask = storage?.getItem('waitless.task_id'); } catch { /* Optional recovery. */ }
  if (restoredTask) {
    startPolling(restoredTask);
    void poller?.pollOnce().catch(showError);
  }
  showTrace();
  return ctx;
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
  if (toolResult.is_error) return null;
  const { name, structured } = toolResult;
  if (name === 'prepare_task' || name === 'update_task') {
    return structured.status === 'ready' && structured.task_card ? renderTaskCard(structured.task_card, { busy }) : null;
  }
  return name === 'get_case_result' ? renderResult(structured) : null;
}

// Start call 按钮：start_assisted_call({task_id, confirmed: true, card_version})（R13 网页侧守门）。
// state.task_id 或 state.card_version 缺失时不调用，返回 null。返回 StartCallOut。
export async function onStartCall(ctx) {
  const { task_id, card_version } = ctx.state;
  if (!task_id || card_version === null || card_version === undefined) return null;
  if (ctx.state.task_card?.precall_decisions.some((q) => q.needs_user && !q.selected)) return null;
  const result = await ctx.mcp.callTool('start_assisted_call', { task_id, confirmed: true, card_version });
  await ctx._afterStart?.('start_assisted_call', result);
  return result;
}

// 待决选项 Confirm 按钮：answer_pending_choice({task_id, choice_id, option_id})。
// state.option_id 为空（用户还没选）时不调用，返回 null（Confirm 本就禁用，这里再守一道，AC-09）。返回 AnswerChoiceOut。
export async function onConfirmChoice(ctx) {
  const { task_id, choice_id, option_id } = ctx.state;
  if (!task_id || !choice_id || !option_id) return null;
  return ctx.mcp.callTool('answer_pending_choice', { task_id, choice_id, option_id });
}

// Join 按钮：join_call({task_id}) → state.handoff_id = join.handoff_id → show(renderHandoffCard(join))
// → ctx.handoff = await joinHandoffAudio({join, location, mediaDevices, WebSocketImpl, AudioContextImpl, AudioWorkletNodeImpl, ...})。
// getUserMedia 只在 join_call 成功之后、在这次点击里调用一次（AC-14；AudioContext 也在这次点击里创建，NFR-6）。
// join_call 失败时不请求麦克风。返回 JoinCallOut。
export async function onJoinCall(ctx) {
  if (!ctx.state.task_id || ctx.handoff || ctx._joining) return null;
  void onStopListening(ctx);
  ctx._joining = true;
  const generation = ctx._handoffGeneration ?? 0;
  ctx._serverHandoffGeneration = ctx.state.handoff_generation;
  let audioEnded = false;
  try {
    const join = await ctx.mcp.callTool('join_call', { task_id: ctx.state.task_id });
    if ((ctx._handoffGeneration ?? 0) !== generation) return join;
    ctx.state.handoff_id = join.handoff_id;
    ctx.state.call_id = join.call_id;
    ctx.show?.(renderHandoffCard(join, { mode: ctx.config?.mode, keypad: ctx.config?.mode === 'live-sim' }));
    let sentKeys = 0;
    const handle = await joinHandoffAudio({ ...ctx, join, onKeys(message) {
      if ((ctx._handoffGeneration ?? 0) !== generation) return;
      const feedback = (ctx.document ?? globalThis.document)?.querySelector('#callView .keypad-feedback');
      if (message.type === 'dtmf_ack' && Number.isInteger(message.count) && message.count > 0 && message.count <= 16) {
        sentKeys += message.count;
        if (feedback) feedback.textContent = `Sent ${sentKeys} keys`;
      } else if (message.type === 'dtmf_error' && feedback) feedback.textContent = 'Key not sent';
    }, onEnded() {
      audioEnded = true;
      if ((ctx._handoffGeneration ?? 0) !== generation) return;
      ctx.handoff = null;
      ctx._onHandoffEnded?.();
    } });
    if ((ctx._handoffGeneration ?? 0) !== generation || audioEnded) await handle.stop();
    else ctx.handoff = handle;
    return join;
  } catch (error) {
    if ((ctx._handoffGeneration ?? 0) === generation) ctx._onHandoffEnded?.();
    throw error;
  } finally { ctx._joining = false; }
}

// These two tools require explicit clicks, just like Join and text Return.
export async function onHangup(ctx) {
  const taskId = ctx.state.task_id;
  if (!taskId) return null;
  ctx._hangupTarget ??= { task_id: taskId, call_id: ctx.state.call_id };
  const target = ctx._hangupTarget;
  if (target.task_id !== taskId || (ctx.state.call_id && target.call_id && ctx.state.call_id !== target.call_id)) return null;
  const cleanup = onLeaveCallPage(ctx);
  let result;
  try {
    result = await ctx.mcp.callTool('hangup_call', { task_id: taskId });
    if (result.call_id) target.call_id = result.call_id;
  } catch (error) {
    await cleanup;
    if (ctx._hangupTarget === target && ctx.state.task_id === taskId) await ctx._afterHangup?.(null, error);
    throw error;
  }
  await cleanup;
  if (ctx._hangupTarget === target && ctx.state.task_id === taskId) await ctx._afterHangup?.(result);
  return result;
}

export async function onReportResult(ctx, { call_id, outcome, confirmation_number } = {}) {
  if (!ctx.state.task_id || !call_id || !['confirmed', 'failed', 'needs_follow_up'].includes(outcome)) return null;
  return ctx.mcp.callTool('report_call_result', {
    task_id: ctx.state.task_id, call_id, outcome,
    ...(confirmation_number?.trim() ? { confirmation_number: confirmation_number.trim() } : {}),
  });
}

// Return to Waitless 按钮：return_to_agent({task_id, handoff_id})，并 stop() 转接音频、ctx.handoff = null；
// return_to_agent 失败也照样停掉音频，再把错误抛出。返回 ReturnOut。
export async function onReturn(ctx) {
  const { task_id, handoff_id } = ctx.state;
  const cleanup = onLeaveCallPage(ctx);
  let result;
  try {
    if (!task_id || !handoff_id) return null;
    result = await ctx.mcp.callTool('return_to_agent', { task_id, handoff_id });
  } finally { await cleanup; }
  await ctx._afterReturn?.('return_to_agent', result);
  return result;
}

// 离开通话页（pagehide、页内返回聊天视图）：stop() 转接音频、ctx.handoff = null（AC-14）。
// 不调用任何 MCP 工具：return_to_agent 是用户动作工具，只由 Return 按钮调用；WebSocket 不经 return 断开后，
// live 在服务端重连宽限内等待重新加入；text 沿用接回语义。停转接与旁听；可重复调用。
export async function onLeaveCallPage(ctx) {
  const listening = onStopListening(ctx);
  ctx._handoffGeneration = (ctx._handoffGeneration ?? 0) + 1;
  const handoff = ctx.handoff;
  ctx.handoff = null;
  await handoff?.stop();
  await listening;
}

export async function onListen(ctx) {
  if (!ctx.state?.task_id || ctx.listen || ctx.handoff || ctx._joining) return null;
  const generation = ctx._listenGeneration = (ctx._listenGeneration ?? 0) + 1;
  let context;
  try { context = new ctx.AudioContextImpl({ sampleRate: 16000 }); }
  catch { context = new ctx.AudioContextImpl(); }
  if (context.state === 'suspended') void context.resume();
  try {
    const response = await (ctx.fetchImpl ?? globalThis.fetch)('/listen', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ task_id: ctx.state.task_id }),
    });
    const ticket = await response.json();
    if (!response.ok) throw new Error(ticket.message || ticket.code);
    if (ctx._listenGeneration !== generation) {
      void context.close();
      return null;
    }
    const handle = startListenAudio({ ticket, context, location: ctx.location, WebSocketImpl: ctx.WebSocketImpl,
      onEnded: (reason) => {
        if (ctx.listen === handle) {
          ctx.listen = null;
          ctx._onListenEnded?.(reason);
        }
      },
    });
    ctx.listen = handle;
    return ticket;
  } catch (error) {
    void context.close();
    throw error;
  }
}

export function onStopListening(ctx) {
  ctx._listenGeneration = (ctx._listenGeneration ?? 0) + 1;
  const listen = ctx.listen;
  ctx.listen = null;
  return Promise.resolve(listen?.stop());
}
