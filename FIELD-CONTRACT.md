# Shared workflow and field contract

Both cases run through the same rendering functions and state transitions in `Waitless-Alexa-MCP-Demo.html`. `CASES` supplies scenario content; `FIELD_SCHEMA` is the only source of field labels and input metadata. The legacy `electricity-demo.html` URL redirects into the shared application rather than maintaining a second copy.

## Shared workflow

1. Profile check — `begin` checks a preloaded, simulated registered profile during loading. Complete details lead directly to the pre-call card; no mandatory details screen.
2. Before the call — `callReview` summarizes the request, name and masked phone (plus electricity address). Start call requires an explicit click, never silence or a timer. Edit details optionally opens `openSetup` with consistent field labels; saving updates the same card. Missing details are requested only when needed.
3. Your approval — `approvalContent` renders the same inline chat radio-option component and explicit approval action, with scenario-specific terms. No Review options navigation. After approval the card becomes a read-only summary.

Dialing progress, private handoff, transcription entry, result cards, and case history also use shared functions. Scenario-specific follow-up remains supported: electricity waits for a demo callback; Costco collects demo confirmation automatically.

## Field names

| Key | Display label | Electricity | Costco |
|---|---|---|---|
| `name` | Name | Required | Required |
| `phone_number` | Phone number | Required | Required |
| `address` | Address | Required | Not requested |
| `membership_last_four` | Membership last four digits | Not requested | Required |

Name and phone are not renamed to “Primary member name” or “Phone for secure handoff” in a different scenario. Address likewise keeps its shared label wherever it is requested. Case-specific meaning belongs in helper text, not an alternate field name.

Reviewed details are retained in the current browser session when restarting and choosing the other case. No profile is stored across page reloads. Case-only fields are only rendered and sent when the active schema requires them. Edit/save updates the same pre-call card. Inputs are escaped when rendered.

`save_task_context` receives `task_id`, `scenario`, the active schema's field keys, `scope: request_information_only`, and `demo: true`. Final approval is a separate tool call. All values and outcomes remain demo data.
