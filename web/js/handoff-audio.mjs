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
  throw new Error('not implemented');
}

// Float32 [-1, 1] → 16 kHz PCM16（线性插值或抽取），G3：返回 Int16Array（样本数，不是字节数）。
// 超出 [-1, 1] 的值削顶，不回绕。1 秒 48 kHz 正弦 → 长度 16000±1，峰值误差 ≤1%（AC-15）。
export function downsampleToPcm16(float32, inRate, outRate = HANDOFF_SAMPLE_RATE) {
  throw new Error('not implemented');
}

// PCM16 LE（Int16Array）→ Float32，供播放对方音频。
export function pcm16ToFloat32(pcm16) {
  throw new Error('not implemented');
}
