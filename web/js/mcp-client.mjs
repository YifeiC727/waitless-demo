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
  // todo(C): 每个请求都带 MCP-Protocol-Version: 2025-11-25 与 Accept: application/json, text/event-stream，
  //          以及 content-type: application/json（AC-03）。JSON-RPC id 自增。
  // todo(C): 没有本地模拟回退，也没有「只限本机地址」的判断（inv-1 §6.1）。
  return {
    async initialize() { throw new Error('not implemented'); },
    async listTools() { throw new Error('not implemented'); },
    async callTool(_name, _args = {}) { throw new Error('not implemented'); },
  };
}
