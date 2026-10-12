# Waitless

A mobile web hackathon prototype: ask an Alexa-inspired assistant to handle a customer-service call, approve a consequential choice, join an identity-verification handoff, and review the outcome.

## Run

Requires Node.js 20 or later. No dependencies or API keys are required.

```sh
npm start
```

Open http://127.0.0.1:4173. Use `PORT=4180 npm start` to select another port.

You can also open `Waitless-Alexa-MCP-Demo.html` directly. The standalone version uses simulated local tool responses; the hosted version sends JSON-RPC requests to the included `/mcp` endpoint.

## GitHub Pages

GitHub Pages publishes the `docs` directory on the `main` branch. `docs/index.html` is the standalone frontend; refresh it from `Waitless-Alexa-MCP-Demo.html` before committing frontend updates. The Node server does not run on Pages; tool responses there are simulated locally in the browser. The MCP drawer labels this as standalone demo transport.

Live demo: https://yifeic727.github.io/waitless-demo/ . The repository is public with the owner’s approval.

## Demo flow

The home screen offers two parallel cases. The original **Lakeshore Energy electricity-service case** is preserved at `electricity-demo.html` (service start, deposit/autopay, identity handoff, callback, confirmation). **Costco membership cancellation** is the additional case below. Both include chat, voice input, call progress, approval, handoff, and history. Each sends its own `scenario` in tool arguments; their task IDs are `WL-2048` and `WL-2049` respectively.

1. Ask to cancel a Costco membership and inquire about a refund.
2. Review the task-scoped demo details.
3. Start the call; dialing, menu navigation, hold, and representative conversation progress automatically.
4. Choose and approve immediate cancellation or only disabling auto-renewal.
5. Answer the simulated private handoff. Mute/speaker toggle visual state, keypad opens, and the call timer advances. No audio is captured or transmitted.
6. Return control to Waitless. Demo confirmation arrives automatically; view the case history and refund status.

The MCP drawer shows request/response traffic. This lightweight demo transport implements the JSON-RPC methods shown in the prototype, not a complete production MCP server or an official Alexa integration.

## Files

- `Waitless-Alexa-MCP-Demo.html` — standalone interface and demo behavior
- `electricity-demo.html` — original electricity case with navigation to the added Costco case
- `waitless-mcp-demo-server.mjs` — local web server and tool responses
- `Waitless-PRD.md` — product definition and architecture
- `DESIGN.md` — interface decisions and design guidance
- `VOICE-INPUT.md` — voice recording and transcription integration contract

Costco is a real organization used as the scenario. All calls, member data, identity checks, account changes, refund requests, and confirmations here are simulated. This project is not affiliated with Amazon or Costco. There are no real calls or account changes.
