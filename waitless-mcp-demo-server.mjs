import http from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const htmlPath = join(here, "Waitless-Alexa-MCP-Demo.html");
const port = Number(process.env.PORT || 4173);

const tools = [
  "prepare_task",
  "save_task_context",
  "start_assisted_call",
  "get_call_status",
  "answer_pending_choice",
  "begin_identity_handoff",
  "resume_assisted_call",
  "get_case_result"
].map((name) => ({ name, description: `Waitless demo tool: ${name}`, inputSchema: { type: "object" } }));

function resultFor(name, args) {
  if (args?.scenario === "electric_service_demo") {
    const base = { demo: true, task_id: args?.task_id || "WL-2048" };
    return {
      prepare_task: { ...base, status: "setup_required", missing: ["name", "phone_number", "address"] },
      save_task_context: { ...base, status: "ready", profile_scope: "task_only" },
      start_assisted_call: { ...base, status: "calling", provider: "Lakeshore Energy (fictional)" },
      get_call_status: { ...base, status: "action_required", action: "choose_deposit_or_autopay" },
      answer_pending_choice: { ...base, status: "choice_recorded", answer: args?.answer || "autopay" },
      begin_identity_handoff: { ...base, status: "user_bridge_ringing", representative_connected: true, user_phone: "***-***-0147", ai_media: "disconnected", recording: false, transcription: false },
      resume_assisted_call: { ...base, status: "waiting_for_callback", verification_completed: true },
      get_case_result: { ...base, status: "resolved", confirmation: "LE-48291", start_date: "November 1", deposit: args?.approved_change === "deposit" ? "120_selected_not_paid" : "waived" }
    }[name] || { ...base, status: "ok" };
  }
  const base = { demo: true, task_id: args?.task_id || "WL-2049" };
  const results = {
    prepare_task: { ...base, status: "setup_required", missing: ["name", "phone_number", "membership_last_four"] },
    save_task_context: { ...base, status: "ready", profile_scope: "task_only" },
    start_assisted_call: { ...base, status: "calling", provider: "Costco (simulated call)" },
    get_call_status: { ...base, status: "action_required", action: "choose_membership_change" },
    answer_pending_choice: { ...base, status: "choice_recorded", answer: args?.answer || null },
    begin_identity_handoff: { ...base, status: "user_bridge_ringing", representative_connected: true, user_phone: "***-***-0147", ai_media: "disconnected", recording: false, transcription: false },
    resume_assisted_call: { ...base, status: "waiting_for_confirmation", verification_completed: true },
    get_case_result: { ...base, status: "resolved", confirmation: "DEMO-CM-48291", outcome: args?.approved_change === "stop_renewal" ? "auto_renewal_disabled" : "membership_canceled", refund_status: args?.approved_change === "stop_renewal" ? "not_requested" : "requested_not_received" }
  };
  return results[name] || { ...base, status: "ok" };
}

const server = http.createServer(async (req, res) => {
  const pathname = req.url.split("?")[0];
  if (req.method === "GET" && ["/", "/index.html", "/Waitless-Alexa-MCP-Demo.html", "/electricity-demo.html"].includes(pathname)) {
    const html = await readFile(pathname === "/electricity-demo.html" ? join(here, "electricity-demo.html") : htmlPath);
    res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
    return res.end(html);
  }

  if (req.method === "POST" && req.url === "/mcp") {
    let body = "";
    for await (const chunk of req) body += chunk;
    let message;
    try { message = JSON.parse(body); }
    catch { res.writeHead(400); return res.end("Invalid JSON"); }

    let payload;
    if (message.method === "initialize") {
      payload = { protocolVersion: "2025-11-25", capabilities: { tools: {} }, serverInfo: { name: "waitless-demo", version: "0.1.0" } };
    } else if (message.method === "tools/list") {
      payload = { tools };
    } else if (message.method === "tools/call") {
      const { name, arguments: args } = message.params || {};
      payload = { content: [{ type: "text", text: JSON.stringify(resultFor(name, args || {})) }], structuredContent: resultFor(name, args || {}) };
    } else {
      res.writeHead(404, { "content-type": "application/json" });
      return res.end(JSON.stringify({ jsonrpc: "2.0", id: message.id, error: { code: -32601, message: "Method not found" } }));
    }

    res.writeHead(200, {
      "content-type": "application/json",
      "mcp-protocol-version": "2025-11-25",
      "access-control-allow-origin": "*"
    });
    return res.end(JSON.stringify({ jsonrpc: "2.0", id: message.id, result: payload }));
  }

  res.writeHead(404);
  res.end("Not found");
});

server.listen(port, "127.0.0.1", () => {
  console.log(`Waitless demo: http://127.0.0.1:${port}`);
});
