// 前端测试用的假对象（第 3 步写，冻结；tech §12）。只用 Node ≥20 自带的能力，不需要 npm 依赖和 DOM。
//
//   createFakeFetch()            假 fetch：记录每个请求，按路由应答；可挂 createFakeMcpServer()
//   createFakeMcpServer()        假 MCP Streamable HTTP 端点（JSON-RPC：initialize、notifications/initialized、tools/list、tools/call）
//   FakeWebSocket                假 WebSocket：记录发送，测试端可模拟服务器 open/send/close
//   createFakeMediaDevices()     假 navigator.mediaDevices：记录 getUserMedia 调用，音轨可 stop
//   FakeAudioContext、FakeAudioWorkletNode  转接音频用到的 Web Audio 最小替身
//   trackTimers()                统计 setTimeout/setInterval 调用（AC-08、AC-09「不设定时器」）
//   float32Sine()                生成 Float32 正弦（AC-15 重采样）

export const MCP_PROTOCOL_VERSION = '2025-11-25';

export function jsonResponse(body, status = 200, headers = {}) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });
}

function headersToObject(headers) {
  const out = {};
  if (!headers) return out;
  if (typeof headers.forEach === 'function' && !Array.isArray(headers)) {
    headers.forEach((v, k) => { out[String(k).toLowerCase()] = String(v); });
    return out;
  }
  const entries = Array.isArray(headers) ? headers : Object.entries(headers);
  for (const [k, v] of entries) out[String(k).toLowerCase()] = String(v);
  return out;
}

function parseBody(body) {
  if (body == null) return null;
  if (typeof body !== 'string') return body;
  try { return JSON.parse(body); } catch { return body; }
}

// ---------------------------------------------------------------------------
// fetch
// ---------------------------------------------------------------------------

// routes: { 'POST /assistant/turn': handler | response | [response, ...] }
// handler(req) 返回 Response 或可 JSON 化的对象；数组按顺序逐次取用。
export function createFakeFetch({ routes = {}, mcp = null } = {}) {
  const table = new Map(Object.entries(routes));
  const calls = [];
  async function fetchImpl(input, init = {}) {
    const url = typeof input === 'string' ? input : input.url;
    const method = String(init.method || 'GET').toUpperCase();
    const path = url.replace(/^[a-z]+:\/\/[^/]+/i, '').split('?')[0];
    const req = { url, path, method, headers: headersToObject(init.headers), body: parseBody(init.body) };
    calls.push(req);
    if (mcp && path === mcp.path) return mcp.handle(req);
    const key = `${method} ${path}`;
    if (!table.has(key)) return jsonResponse({ error: `no fake route for ${key}` }, 404);
    let entry = table.get(key);
    if (Array.isArray(entry)) {
      if (entry.length === 0) throw new Error(`fake route ${key} exhausted`);
      entry = entry.shift();
    }
    const out = typeof entry === 'function' ? await entry(req) : entry;
    if (out instanceof Error) throw out;
    return out instanceof Response ? out : jsonResponse(out);
  }
  fetchImpl.calls = calls;
  fetchImpl.route = (key, entry) => { table.set(key, entry); return fetchImpl; };
  fetchImpl.callsTo = (path) => calls.filter((c) => c.path === path);
  return fetchImpl;
}

// ---------------------------------------------------------------------------
// MCP 端点
// ---------------------------------------------------------------------------

// tools: [{name, description, inputSchema, outputSchema}]
// results: { toolName: entry | [entry, ...] | (args) => entry }
//   entry = 结构化结果对象（返回 isError:false 且 structuredContent=entry）
//         | { isError: true, code, message }（返回 isError:true，content[0].text 为 {"code","message"}）
//         | { rpcError: { code, message } }（返回 JSON-RPC error）
export function createFakeMcpServer({ tools = [], results = {}, path = '/mcp', protocolVersion = MCP_PROTOCOL_VERSION } = {}) {
  const requests = [];
  const pending = Object.fromEntries(Object.entries(results).map(([k, v]) => [k, Array.isArray(v) ? [...v] : v]));
  function rpcResult(id, result) { return jsonResponse({ jsonrpc: '2.0', id, result }); }
  function rpcError(id, error) { return jsonResponse({ jsonrpc: '2.0', id, error }); }
  async function handle(req) {
    const msg = req.body || {};
    requests.push({ method: msg.method, params: msg.params, id: msg.id, headers: req.headers });
    if (msg.id === undefined || msg.id === null) return new Response(null, { status: 202 });
    switch (msg.method) {
      case 'initialize':
        return rpcResult(msg.id, {
          protocolVersion,
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name: 'fake-waitless', version: '0.0.0' },
        });
      case 'tools/list':
        return rpcResult(msg.id, { tools });
      case 'tools/call': {
        const name = msg.params?.name;
        let entry = pending[name];
        if (Array.isArray(entry)) {
          if (entry.length === 0) return rpcError(msg.id, { code: -32603, message: `fake tool ${name} exhausted` });
          entry = entry.shift();
        }
        if (typeof entry === 'function') entry = await entry(msg.params?.arguments || {});
        if (entry === undefined) return rpcError(msg.id, { code: -32602, message: `unknown tool ${name}` });
        if (entry.rpcError) return rpcError(msg.id, entry.rpcError);
        if (entry.isError) {
          return rpcResult(msg.id, {
            isError: true,
            content: [{ type: 'text', text: JSON.stringify({ code: entry.code, message: entry.message || '' }) }],
          });
        }
        return rpcResult(msg.id, {
          isError: false,
          content: [{ type: 'text', text: JSON.stringify(entry) }],
          structuredContent: entry,
        });
      }
      default:
        return rpcError(msg.id, { code: -32601, message: `method not found: ${msg.method}` });
    }
  }
  return {
    path,
    handle,
    requests,
    methods: () => requests.map((r) => r.method),
    toolCalls: () => requests.filter((r) => r.method === 'tools/call').map((r) => r.params?.name),
  };
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------

export class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances = [];
  static reset() { FakeWebSocket.instances = []; }
  static last() { return FakeWebSocket.instances[FakeWebSocket.instances.length - 1]; }

  constructor(url, protocols) {
    this.url = String(url);
    this.protocols = protocols;
    this.readyState = FakeWebSocket.CONNECTING;
    this.binaryType = 'blob';
    this.sent = [];
    this.closeCalls = [];
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    this._listeners = { open: [], message: [], close: [], error: [] };
    FakeWebSocket.instances.push(this);
  }

  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this._listeners[type] = (this._listeners[type] || []).filter((f) => f !== fn); }
  _fire(type, event) {
    const handler = this['on' + type];
    if (typeof handler === 'function') handler.call(this, event);
    for (const fn of this._listeners[type] || []) fn.call(this, event);
  }

  send(data) {
    if (this.readyState !== FakeWebSocket.OPEN) throw new Error('FakeWebSocket.send while not OPEN');
    this.sent.push(data);
  }

  close(code = 1000, reason = '') {
    this.closeCalls.push({ code, reason });
    if (this.readyState === FakeWebSocket.CLOSED) return;
    this.readyState = FakeWebSocket.CLOSED;
    this._fire('close', { code, reason, wasClean: true });
  }

  get sentText() { return this.sent.filter((d) => typeof d === 'string'); }
  get sentBinary() { return this.sent.filter((d) => typeof d !== 'string'); }

  // 测试端（模拟服务器）
  serverOpen() { this.readyState = FakeWebSocket.OPEN; this._fire('open', {}); }
  serverSend(data) {
    const payload = typeof data === 'string' || data instanceof ArrayBuffer || ArrayBuffer.isView(data)
      ? data
      : JSON.stringify(data);
    this._fire('message', { data: ArrayBuffer.isView(payload) ? payload.buffer : payload });
  }
  serverClose(code = 1000, reason = '') {
    this.readyState = FakeWebSocket.CLOSED;
    this._fire('close', { code, reason, wasClean: code === 1000 });
  }
  serverError() { this._fire('error', {}); }
}

// ---------------------------------------------------------------------------
// mediaDevices
// ---------------------------------------------------------------------------

export class FakeMediaStreamTrack {
  constructor(kind = 'audio') {
    this.kind = kind;
    this.readyState = 'live';
    this.enabled = true;
    this.stopCalls = 0;
  }
  stop() { this.stopCalls += 1; this.readyState = 'ended'; }
  get stopped() { return this.readyState === 'ended'; }
}

export class FakeMediaStream {
  constructor(tracks = [new FakeMediaStreamTrack('audio')]) { this._tracks = tracks; }
  getTracks() { return [...this._tracks]; }
  getAudioTracks() { return this._tracks.filter((t) => t.kind === 'audio'); }
}

export function createFakeMediaDevices({ deny = false } = {}) {
  const calls = [];
  const streams = [];
  return {
    calls,
    streams,
    async getUserMedia(constraints) {
      calls.push(constraints);
      if (deny) {
        const err = new Error('Permission denied');
        err.name = 'NotAllowedError';
        throw err;
      }
      const stream = new FakeMediaStream();
      streams.push(stream);
      return stream;
    },
    allTracksStopped() { return streams.every((s) => s.getTracks().every((t) => t.stopped)); },
  };
}

// ---------------------------------------------------------------------------
// Web Audio
// ---------------------------------------------------------------------------

class FakeAudioNode {
  constructor() { this.connections = []; this.disconnected = false; }
  connect(dest) { this.connections.push(dest); return dest; }
  disconnect() { this.disconnected = true; }
}

export class FakeAudioContext {
  static instances = [];
  static reset() { FakeAudioContext.instances = []; }
  constructor(options = {}) {
    this.sampleRate = options.sampleRate || 48000;
    this.state = 'running';
    this.currentTime = 0;
    this.destination = new FakeAudioNode();
    this.addedModules = [];
    this.sources = [];
    this.buffers = [];
    this.audioWorklet = { addModule: async (url) => { this.addedModules.push(String(url)); } };
    FakeAudioContext.instances.push(this);
  }
  createMediaStreamSource(stream) { const n = new FakeAudioNode(); n.mediaStream = stream; this.sources.push(n); return n; }
  createGain() { const n = new FakeAudioNode(); n.gain = { value: 1 }; return n; }
  createBuffer(channels, length, sampleRate) {
    const data = Array.from({ length: channels }, () => new Float32Array(length));
    const buf = {
      numberOfChannels: channels, length, sampleRate, duration: length / sampleRate,
      getChannelData: (i) => data[i],
      copyToChannel: (src, i) => data[i].set(src),
    };
    this.buffers.push(buf);
    return buf;
  }
  createBufferSource() {
    const n = new FakeAudioNode();
    n.buffer = null;
    n.started = [];
    n.start = (when = 0) => { n.started.push(when); };
    n.stop = () => { n.stopped = true; };
    return n;
  }
  async resume() { this.state = 'running'; }
  async suspend() { this.state = 'suspended'; }
  async close() { this.state = 'closed'; }
}

export class FakeAudioWorkletNode extends FakeAudioNode {
  static instances = [];
  static reset() { FakeAudioWorkletNode.instances = []; }
  constructor(context, name, options = {}) {
    super();
    this.context = context;
    this.name = name;
    this.options = options;
    const port = {
      onmessage: null,
      posted: [],
      postMessage: (msg) => { port.posted.push(msg); },
      // 测试端：模拟 worklet 发来一帧数据（例如 Float32Array）
      emit: (data) => { if (typeof port.onmessage === 'function') port.onmessage({ data }); },
      close: () => { port.closed = true; },
    };
    this.port = port;
    FakeAudioWorkletNode.instances.push(this);
  }
}

// ---------------------------------------------------------------------------
// 定时器与信号
// ---------------------------------------------------------------------------

export function trackTimers() {
  const original = {
    setTimeout: globalThis.setTimeout,
    setInterval: globalThis.setInterval,
    clearTimeout: globalThis.clearTimeout,
    clearInterval: globalThis.clearInterval,
  };
  const record = { timeouts: [], intervals: [], cleared: [] };
  globalThis.setTimeout = (fn, ms, ...args) => { record.timeouts.push(ms); return original.setTimeout(fn, ms, ...args); };
  globalThis.setInterval = (fn, ms, ...args) => { record.intervals.push(ms); return original.setInterval(fn, ms, ...args); };
  globalThis.clearTimeout = (id) => { record.cleared.push(id); return original.clearTimeout(id); };
  globalThis.clearInterval = (id) => { record.cleared.push(id); return original.clearInterval(id); };
  record.count = () => record.timeouts.length + record.intervals.length;
  record.restore = () => Object.assign(globalThis, original);
  return record;
}

export function float32Sine({ freq = 440, sampleRate = 48000, seconds = 1, amplitude = 0.5 } = {}) {
  const n = Math.round(sampleRate * seconds);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i += 1) out[i] = amplitude * Math.sin((2 * Math.PI * freq * i) / sampleRate);
  return out;
}

export function flushMicrotasks(times = 5) {
  let p = Promise.resolve();
  for (let i = 0; i < times; i += 1) p = p.then(() => undefined);
  return p;
}
