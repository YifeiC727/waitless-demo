// 转接音频：getUserMedia、/ws/handoff、重采样（C 线；tech §6.4；AC-14、AC-15；K7）。
// WebSocket 地址由 join_call 的 ws_path 与当前页面的 location 拼出（https → wss），不写死主机名。
// r2（第 3 步 C 线缺口定稿）：G3 返回类型；G10 句柄返回时机与 setMuted；G15 处理器名常量。

export const HANDOFF_SAMPLE_RATE = 16000;
export const HANDOFF_FRAME_SAMPLES = 320; // 20 ms × 16 kHz = 640 字节 PCM16

// G15：pcm-worklet.js 里 registerProcessor 用的名字，AudioWorkletNode 必须用同一个。
export const PCM_WORKLET_PROCESSOR = 'pcm-capture';

// 只在 join 之后调用一次 getUserMedia（AC-14）；AudioContext 在用户点击 Join 时创建（iOS，NFR-6）。
// 流程：getUserMedia({audio}) → AudioContext → audioWorklet.addModule(workletUrl) → new AudioWorkletNode(ctx, PCM_WORKLET_PROCESSOR)
//       → 麦克风 source.connect(node) → 每帧 downsampleToPcm16 → 攒够 320 个样本发一个 640 字节的 WebSocket 二进制帧。
// WebSocket：wsUrl = (location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + join.ws_path
//            + '?token=' + encodeURIComponent(join.token)；open 后先发文本帧
//            {"type": "hello", "format": "pcm_s16le", "sample_rate": 16000, "channels": 1}。
// 收到 {"type":"ready"} 后才开始送麦克风音频，并 onReady(msg)；二进制帧是对方音频（PCM16 16 kHz），排队播放，
// 不回送；{"type":"ended"} 时 onEnded(reason)。
//
// G10 句柄返回时机：getUserMedia 成功、worklet 就绪、WebSocket 对象已创建后立即 resolve，不等 open 和 ready；
//   返回 { stop(), setMuted(muted) }：
//     stop()          停止全部 track、关闭 WebSocket 与 AudioContext；可重复调用；之后不再发送任何帧
//                     （点 Return 或离开通话页时调用，AC-14）
//     setMuted(bool)  静音 = 停止发送麦克风音频（§6.4），不停 track、不关 WebSocket；false 恢复发送；stop 之后调用无效果
// getUserMedia 被拒绝时 reject（原错误），不留下打开的 WebSocket 或运行中的 AudioContext。
export async function joinHandoffAudio({
  join,
  location,
  mediaDevices,
  WebSocketImpl,
  AudioContextImpl,
  AudioWorkletNodeImpl,
  onReady = () => {},
  onEnded = () => {},
  workletUrl = '/js/pcm-worklet.js',
} = {}) {
  let stream, context, node, source, silentOutput, ws;
  let stopped = false;
  let muted = false;
  let ready = false;
  let closing;
  let captured = new Float32Array(0);
  let position = 0;
  let frame = new Int16Array(HANDOFF_FRAME_SAMPLES);
  let frameSize = 0;
  let playbackTime = 0;
  const playing = new Set();
  function resetCapture() { captured = new Float32Array(0); position = 0; frameSize = 0; }
  function stop() {
    if (stopped) return closing;
    stopped = true;
    ready = false;
    resetCapture();
    stream?.getTracks().forEach((track) => track.stop());
    if (node) { node.port.onmessage = null; node.port.close?.(); node.disconnect(); }
    source?.disconnect();
    silentOutput?.disconnect();
    for (const player of playing) { player.stop(); player.disconnect(); }
    playing.clear();
    if (ws && ws.readyState !== 3) ws.close();
    closing = context ? Promise.resolve(context.close()) : Promise.resolve();
    return closing;
  }
  function end(reason) {
    if (stopped) return;
    void stop();
    onEnded(reason);
  }
  try {
    stream = await mediaDevices.getUserMedia({ audio: { channelCount: 1 }, video: false });
    context = new AudioContextImpl();
    if (context.state === 'suspended') await context.resume();
    await context.audioWorklet.addModule(workletUrl);
    node = new AudioWorkletNodeImpl(context, PCM_WORKLET_PROCESSOR, { channelCount: 1 });
    source = context.createMediaStreamSource(stream);
    source.connect(node);
    // Keep the worklet in the rendering graph without monitoring the microphone.
    silentOutput = context.createGain();
    silentOutput.gain.value = 0;
    node.connect(silentOutput);
    silentOutput.connect(context.destination);
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocketImpl(`${scheme}//${location.host}${join.ws_path}?token=${encodeURIComponent(join.token)}`);
    ws.binaryType = 'arraybuffer';
    ws.addEventListener('open', () => {
      if (!stopped) ws.send(JSON.stringify({ type: 'hello', format: 'pcm_s16le', sample_rate: HANDOFF_SAMPLE_RATE, channels: 1 }));
    });
    ws.addEventListener('message', ({ data }) => {
      if (stopped) return;
      if (typeof data === 'string') {
        let message;
        try { message = JSON.parse(data); } catch { end('invalid_message'); return; }
        if (message.type === 'ready') { resetCapture(); ready = true; onReady(message); }
        else if (message.type === 'ended') end(message.reason);
        return;
      }
      if (!ready) return;
      const bytes = ArrayBuffer.isView(data) ? new DataView(data.buffer, data.byteOffset, data.byteLength) : new DataView(data);
      if (bytes.byteLength === 0 || bytes.byteLength % 2) return;
      const pcm = new Int16Array(bytes.byteLength / 2);
      for (let i = 0; i < pcm.length; i += 1) pcm[i] = bytes.getInt16(i * 2, true);
      const buffer = context.createBuffer(1, pcm.length, HANDOFF_SAMPLE_RATE);
      buffer.getChannelData(0).set(pcm16ToFloat32(pcm));
      const player = context.createBufferSource();
      player.buffer = buffer;
      player.connect(context.destination);
      playing.add(player);
      player.onended = () => { playing.delete(player); player.disconnect(); };
      playbackTime = Math.max(context.currentTime, playbackTime);
      player.start(playbackTime);
      playbackTime += buffer.duration;
    });
    ws.addEventListener('close', ({ reason }) => end(reason || 'disconnected'));
    ws.addEventListener('error', () => end('transport_error'));
    node.port.onmessage = ({ data }) => {
      if (stopped || muted || !ready || ws.readyState !== 1) return;
      if (!data?.length) return;
      const combined = new Float32Array(captured.length + data.length);
      combined.set(captured);
      combined.set(data, captured.length);
      captured = combined;
      const ratio = context.sampleRate / HANDOFF_SAMPLE_RATE;
      // Carry fractional sample positions across render quanta (128 frames), so
      // 44.1/48 kHz input cannot accumulate per-chunk rounding drift.
      while (position < captured.length - 1) {
        const left = Math.floor(position);
        const fraction = position - left;
        frame[frameSize++] = toPcm16(captured[left] * (1 - fraction) + captured[left + 1] * fraction);
        position += ratio;
        if (frameSize === HANDOFF_FRAME_SAMPLES) {
          const bytes = new ArrayBuffer(HANDOFF_FRAME_SAMPLES * 2);
          const view = new DataView(bytes);
          for (let i = 0; i < frame.length; i += 1) view.setInt16(i * 2, frame[i], true);
          ws.send(bytes);
          frameSize = 0;
        }
      }
      const consumed = Math.min(Math.floor(position), captured.length - 1);
      captured = captured.slice(consumed);
      position -= consumed;
    };
    return { stop, setMuted(value) { if (!stopped) { muted = Boolean(value); resetCapture(); } } };
  } catch (error) {
    try { await stop(); } catch { /* Preserve the original initialization error. */ }
    throw error;
  }
}

function toPcm16(value) {
  const clamped = Math.max(-1, Math.min(1, value));
  return Math.round(clamped * (clamped < 0 ? 32768 : 32767));
}

// Float32 [-1, 1] → 16 kHz PCM16（线性插值或抽取），G3：返回 Int16Array（样本数，不是字节数）。
// 超出 [-1, 1] 的值削顶，不回绕。1 秒 48 kHz 正弦 → 长度 16000±1，峰值误差 ≤1%（AC-15）。
export function downsampleToPcm16(float32, inRate, outRate = HANDOFF_SAMPLE_RATE) {
  if (!Number.isFinite(inRate) || inRate <= 0 || !Number.isFinite(outRate) || outRate <= 0) {
    throw new RangeError('Sample rates must be positive finite numbers');
  }
  const result = new Int16Array(Math.floor(float32.length * outRate / inRate));
  for (let i = 0; i < result.length; i += 1) {
    const at = i * inRate / outRate;
    const left = Math.floor(at);
    const fraction = at - left;
    result[i] = toPcm16(float32[left] * (1 - fraction) + float32[Math.min(left + 1, float32.length - 1)] * fraction);
  }
  return result;
}

// PCM16 LE（Int16Array）→ Float32，供播放对方音频。
export function pcm16ToFloat32(pcm16) {
  return Float32Array.from(pcm16, (value) => value / (value < 0 ? 32768 : 32767));
}
