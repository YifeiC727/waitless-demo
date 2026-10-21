// MCP 客户端（C 线；tech §6.1、inv-1 §2.2、§6.1；AC-03、AC-04；K10）。
// 只用相对路径访问后台（默认 endpoint "/mcp"），不写死主机名或端口（AC-102）。
// r2（第 3 步 C 线缺口定稿）：G2 listTools 返回数组；G9 追踪条目形状。

export const MCP_PROTOCOL_VERSION = '2025-11-25';
export const MCP_ACCEPT = 'application/json, text/event-stream';

// JSON-RPC error 或 isError 结果。code：JSON-RPC 错误码（数字），或 isError 结果 content[0].text 里的业务 code（字符串，§6.1）。
export class McpError extends Error {
  constructor(code, message, data = undefined) {
    super(message);
    this.name = 'McpError';
    this.code = code;
    this.data = data;
  }
}

// 返回 { initialize(), listTools(), callTool(name, args) }。
//   fetchImpl：注入的 fetch（测试用 web/test/helpers.mjs 的 createFakeFetch）
//   onTrace(kind, label, payload)：每条发出的消息与每个响应都写一次追踪（debug 面板，AC-11、NFR-8）。
//     kind  'req'（发出的 JSON-RPC 消息，含 notifications/initialized）| 'res'（收到的响应，含 error 与 isError 结果）
//     label JSON-RPC 方法名；tools/call 写成 `tools/call <工具名>`
//     payload 发出的消息对象，或解析后的响应体；HTTP 失败时为 {status} 或 {error: message}
//     app 把每次调用存成 {kind, label, payload} 交给 renderTracePanel 的 entries（G9）。
//   endpoint：相对路径
//
//   initialize()      发 initialize（protocolVersion 2025-11-25、capabilities {}、clientInfo{name, version}），
//                     成功后发 notifications/initialized（无 id 的通知），返回 initialize 的 result。每页只握手一次。
//   listTools()       发 tools/list，返回 result.tools 数组（G2）；app 取每项的 {name, description} 作为 /assistant/turn 的 tools。
//   callTool(n, a)    发 tools/call {name: n, arguments: a}（参数原样，不追加任何字段），返回 result.structuredContent。
//
// 错误（AC-04）：响应有 error → McpError(error.code, error.message)；tools/call 结果 isError=true → 解析
// content[0].text 的 {code, message} 抛 McpError（text 不是 JSON 时也抛 McpError）；两种都先写 onTrace 再抛。
// HTTP 非 2xx 或网络失败直接抛错（Error 或 McpError 均可），不返回任何模拟结果。
export function createMcpClient({ fetchImpl, onTrace = () => {}, endpoint = '/mcp' } = {}) {
  const fetcher = fetchImpl ?? globalThis.fetch;
  let id = 0;
  let handshake;
  let sessionId;
  async function request(method, params, notification = false) {
    const body = { jsonrpc: '2.0', method };
    if (!notification) body.id = ++id;
    if (params !== undefined) body.params = params;
    const label = method === 'tools/call' ? `${method} ${params.name}` : method;
    const headers = {
      'MCP-Protocol-Version': MCP_PROTOCOL_VERSION,
      Accept: MCP_ACCEPT,
      'Content-Type': 'application/json',
    };
    if (sessionId) headers['Mcp-Session-Id'] = sessionId;
    onTrace('req', label, body);
    let response;
    try {
      response = await fetcher(endpoint, { method: 'POST', headers, body: JSON.stringify(body) });
    } catch (error) {
      onTrace('res', label, { error: error.message });
      throw error;
    }
    if (!response.ok) {
      onTrace('res', label, { status: response.status });
      throw new Error(`MCP HTTP ${response.status}`);
    }
    const receivedSession = response.headers?.get('mcp-session-id');
    if (receivedSession) sessionId = receivedSession;
    if (notification) {
      onTrace('res', label, { status: response.status });
      return;
    }
    let data;
    try { data = await response.json(); }
    catch (error) {
      onTrace('res', label, { error: error.message });
      throw error;
    }
    onTrace('res', label, data);
    if (data.error) throw new McpError(data.error.code, data.error.message, data.error.data);
    if (!Object.hasOwn(data, 'result')) throw new McpError('invalid_response', 'MCP response has no result');
    return data.result;
  }
  return {
    async initialize() {
      if (!handshake) handshake = (async () => {
        const result = await request('initialize', {
          protocolVersion: MCP_PROTOCOL_VERSION, capabilities: {},
          clientInfo: { name: 'waitless-web', version: '0.1.0' },
        });
        if (result.protocolVersion !== MCP_PROTOCOL_VERSION) {
          throw new McpError('protocol_version', 'Unsupported MCP protocol version');
        }
        await request('notifications/initialized', undefined, true);
        return result;
      })();
      return handshake;
    },
    async listTools() { return (await request('tools/list')).tools; },
    async callTool(_name, _args = {}) {
      const result = await request('tools/call', { name: _name, arguments: _args });
      if (result.isError) {
        const raw = result.content?.[0]?.text;
        let error;
        try { error = JSON.parse(raw); } catch { /* Unstructured tool errors still fail. */ }
        throw new McpError(error?.code ?? 'tool_error', error?.message ?? raw ?? 'MCP tool failed', result);
      }
      return result.structuredContent;
    },
  };
}
