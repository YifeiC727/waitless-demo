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

The included `.github/workflows/pages.yml` publishes only the standalone frontend on every push to `main`. In the repository's Settings → Pages, choose GitHub Actions as the source. The Node server does not run on Pages; tool responses there are simulated locally in the browser. The MCP drawer labels this as standalone demo transport.

The expected project URL after publication is `https://yifeic727.github.io/waitless-demo/`. It is not live until the repository and Pages deployment are created successfully. Private-repository Pages availability depends on the account plan; the repository is kept private unless public source publication is explicitly approved.

## Demo flow

1. Start electricity at a new apartment.
2. Review the task-scoped demo details.
3. Start the call; dialing, menu navigation, hold, and representative conversation progress automatically.
4. Approve a deposit or autopay option.
5. Answer the simulated private handoff. Mute/speaker toggle visual state, keypad opens, and the call timer advances. No audio is captured or transmitted.
6. Return control to Waitless, simulate a callback, and view the case history.

The MCP drawer shows request/response traffic. This lightweight demo transport implements the JSON-RPC methods shown in the prototype, not a complete production MCP server or an official Alexa integration.

## Files

- `Waitless-Alexa-MCP-Demo.html` — standalone interface and demo behavior
- `waitless-mcp-demo-server.mjs` — local web server and tool responses
- `Waitless-PRD.md` — product definition and architecture
- `DESIGN.md` — interface decisions and design guidance

All providers, calls, identity checks, charges, and confirmations are fictional. This project is not affiliated with Amazon. There are no real calls or account changes.
