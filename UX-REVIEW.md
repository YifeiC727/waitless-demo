# Waitless UX review

## Principles

Show the task, current state and next action before explaining mechanisms. Keep lightweight interactions in chat; navigate only when the interaction mode changes (the simulated phone call).

## Applied review

| Surface | Change |
|---|---|
| Home | Plain invitation and consistent task suggestions; remove integration explanation. |
| Pre-call | Provider, verb-led goal, saved name/phone and electricity address. Explicit Start call and one short approval boundary. |
| Details | Edit, save and cancel in the same chat card with stable labels and validation. |
| Calling | Current stage plus one supporting line instead of a growing timeline. |
| Approval | Inline options; no default or silent consent. Submitted card becomes read-only. |
| Handoff | One contextual prompt and one click to join; privacy context stated once. |
| Phone | Name, timer, three controls, Return to Waitless and DEMO; one screen. |
| Follow-up | Chat updates automatically; electricity callback accelerated for demo without a manual button. |
| Result | One outcome heading, status and reference. Completed progress/approval/handoff cards retire into expandable history so they do not compete with the outcome. Refund requested is not refund received. |
| History | Native expandable details within the result card, not another page. |
| Voice | Existing draft review and cancel/finish controls; no navigation. |
| Trace | Debug-only technical entry, no consumer MCP badge. |

No real calls, account changes, microphone capture or refunds occur in default demo mode. Final authorization and user identity handoff remain explicit.
# Unified activity card

Profile edits remain an inline labeled form in chat, not a separate page or a sequence of questions. Missing information can be requested conversationally.
One task card evolves from dialing through approval, verification and outcome. Completed states become timestamped events, not separate cards. Call activity is expandable during progress and after completion; its open state survives updates.
Approval uses a fixed Confirm label, disabled until the user explicitly selects a clearly described option. No timeout or automatic consent.
