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

Current UX: edit profile, approve options, view results and expand history within chat. Only joining the simulated phone call opens the Companion view. Electricity's demo callback advances automatically. See UX-REVIEW.md for the all-surface presentation audit.

The home screen offers two parallel cases in one shared application: **Lakeshore Energy electricity service** and **Costco membership cancellation**. Both use the same details form, pre-call review, approval component, chat, voice input, progress, private handoff, and history. Case configuration supplies the fields and content. Each sends its own `scenario`; task IDs are `WL-2048` and `WL-2049`. The legacy electricity URL redirects into the shared application.

1. Ask to cancel a Costco membership and inquire about a refund.
2. Confirm the call request; optionally edit saved sample details in chat.
3. Start the call; dialing, menu navigation, hold, and representative conversation progress automatically.
4. Choose and approve immediate cancellation or only disabling auto-renewal.
5. Answer the simulated private handoff. Mute/speaker toggle visual state, keypad opens, and the call timer advances. No audio is captured or transmitted.
6. Return control to Waitless. Demo confirmation arrives automatically; view the case history and refund status.

Consumer headers have no MCP badge. Open `?debug=1` (or `&debug=1` with a case parameter) to reveal Developer trace and inspect request/response traffic. This lightweight demo transport implements the JSON-RPC methods shown in the prototype, not a complete production MCP server or an official Alexa integration. Join demo call in chat now enters the private call directly, without an extra handoff landing page.

## Files

- `Waitless-Alexa-MCP-Demo.html` — standalone interface and demo behavior
- `electricity-demo.html` — compatibility link to the electricity case in the shared app
- `waitless-mcp-demo-server.mjs` — local web server and tool responses
- `Waitless-PRD.md` — product definition and architecture
- `DESIGN.md` — interface decisions and design guidance
- `VOICE-INPUT.md` — voice recording and transcription integration contract
- `FIELD-CONTRACT.md` — shared field labels, data keys, and workflow contract

Costco is a real organization used as the scenario. All calls, member data, identity checks, account changes, refund requests, and confirmations here are simulated. This project is not affiliated with Amazon or Costco. There are no real calls or account changes.
