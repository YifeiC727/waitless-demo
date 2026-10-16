import { pcm16ToFloat32 } from './handoff-audio.mjs';

export const LISTEN_MAX_LAG_S = 1;
export const LISTEN_PHASES = Object.freeze(['dialing', 'ivr', 'hold', 'ai_agent', 'live_rep', 'awaiting_user', 'resuming']);
export const LISTEN_STOP_PHASES = Object.freeze(['handoff_ready', 'in_handoff']);

export function listenAllowed(mode, status) {
  return mode === 'live-sim' && !!status && !status.ended && LISTEN_PHASES.includes(status.phase);
}

// Receive-only audio; the only outgoing frame is the explicit stop message.
export function startListenAudio({ ticket, context, location, WebSocketImpl, onEnded = () => {} } = {}) {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocketImpl(`${scheme}//${location.host}${ticket.ws_path}?token=${encodeURIComponent(ticket.token)}`);
  ws.binaryType = 'arraybuffer';
  let stopped = false, ready = false, sampleRate, closing;
  let playbackTime = context.currentTime;
  const playing = new Set();
  function clearPlaying() {
    for (const source of playing) { source.stop(); source.disconnect(); }
    playing.clear();
  }
  function stop() {
    if (stopped) return closing;
    stopped = true;
    ready = false;
    if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'stop' }));
    clearPlaying();
    if (ws.readyState !== 3) ws.close();
    closing = Promise.resolve(context.close());
    return closing;
  }
  function end(reason) {
    if (stopped) return;
    void stop();
    onEnded(reason);
  }
  ws.addEventListener('message', ({ data }) => {
    if (stopped) return;
    if (typeof data === 'string') {
      let message;
      try { message = JSON.parse(data); } catch { end('invalid_message'); return; }
      if (message?.type === 'ready') {
        if (message.format !== 'pcm_s16le' || message.channels !== 1 || !Number.isFinite(message.sample_rate) || message.sample_rate <= 0) {
          end('invalid_ready'); return;
        }
        sampleRate = message.sample_rate;
        ready = true;
      } else if (message?.type === 'ended') end(message.reason);
      return;
    }
    if (!ready) return;
    const bytes = ArrayBuffer.isView(data) ? new DataView(data.buffer, data.byteOffset, data.byteLength) : new DataView(data);
    if (bytes.byteLength === 0 || bytes.byteLength % 2) return;
    if (playbackTime - context.currentTime > LISTEN_MAX_LAG_S) {
      clearPlaying();
      playbackTime = context.currentTime;
    }
    const pcm = new Int16Array(bytes.byteLength / 2);
    for (let i = 0; i < pcm.length; i += 1) pcm[i] = bytes.getInt16(i * 2, true);
    const buffer = context.createBuffer(1, pcm.length, sampleRate);
    buffer.getChannelData(0).set(pcm16ToFloat32(pcm));
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    playing.add(source);
    source.onended = () => { playing.delete(source); source.disconnect(); };
    playbackTime = Math.max(context.currentTime, playbackTime);
    source.start(playbackTime);
    playbackTime += buffer.duration;
  });
  ws.addEventListener('close', ({ reason }) => end(reason || 'disconnected'));
  ws.addEventListener('error', () => end('transport_error'));
  return { stop };
}
