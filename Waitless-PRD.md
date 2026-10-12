# Waitless — Alexa+ MCP Experience PRD

## 1. Product definition

Waitless is not a standalone calling app. It is a reusable customer-service calling capability that an AI assistant can invoke through MCP. Alexa+ is the primary conversational surface; the Waitless Companion is a lightweight mobile web surface used only when visual review, consent, identity handoff, or case evidence is required.

**Positioning:** “Ask Alexa. Waitless handles the call.”

**Hackathon framing:** Alexa+ simulated experience backed by a self-hosted MCP server using Streamable HTTP. The default demo uses a fictional utility company and simulated calls. A live-demo variant may place a real PSTN call only to a team-controlled Amazon Connect/Twilio test contact center with every participant's consent.

## 2. Goals

- Let a user delegate a customer-service task without opening another app.
- Demonstrate real MCP protocol traffic: `initialize`, `tools/list`, and `tools/call`.
- Collect only the minimum information needed for the current task.
- Stop for explicit consent before money, contracts, account changes, or other consequential choices.
- Remove the AI from the sensitive media path during identity verification.
- Preserve a case-level history across calls, callbacks, promises, and outcomes.

## 3. Non-goals

- No calls to an uninvolved real company, account access, payment, or external account change.
- No storage of passwords, PINs, OTPs, SSNs, dates of birth, or payment-card data.
- No claim of official Alexa/Amazon affiliation or production Alexa+ connectivity.
- No general-purpose standalone phone dialer.

## 4. Surfaces

### Alexa+ conversational surface

Primary entry point. The user describes a task naturally. Alexa+ invokes Waitless, renders structured tool results as compact cards, and reports progress and completion.

### Waitless Companion

Opened only from an Alexa+ tool card or notification. It handles:

- progressive setup and phone verification;
- review of task scope and authorization boundaries;
- consequential approvals;
- secure identity handoff;
- evidence, confirmation numbers, and case history.

The Companion is intentionally shallow. The hackathon version has no persistent bottom navigation. Cases return to a single case overview; Profile & Security is accessed from the header.

### Voice input

The conversational composer supports dictation through a microphone affordance with recording feedback, elapsed time, cancel, and finish. A transcript is inserted into the draft for review before sending. The prototype defaults to a clearly labeled simulated voice example, with no microphone capture. `window.WaitlessVoice.endpoint` or a custom transcription adapter enables real MediaRecorder capture and backend transcription. Voice capture is canceled on navigation, reset, page exit, or backgrounding, and never runs during identity handoff. See `VOICE-INPUT.md` for the integration contract.

### MCP trace

A developer-facing drawer shows protocol events, tool names, arguments, results, timestamps, and transport mode. It is hidden by default so technical detail does not contaminate the consumer UX.

## 5. First-use experience

1. User asks Alexa+ to start electricity service.
2. Alexa+ calls `prepare_task` with the natural-language goal.
3. Waitless returns `setup_required` and only the missing fields.
4. User opens a one-time Companion setup sheet.
5. User verifies a demo phone number, confirms the service address, and accepts the delegation boundary.
6. Waitless returns a structured plan.
7. User starts the simulated call.

The pre-call review is a confirmation screen, not a policy dashboard. It prioritizes the provider, requested outcome, effective date, handoff phone, and task-specific address. Authorization rules are compressed into one calm sentence: Waitless will interrupt only for a consequential decision or identity check. Detailed permission language remains in setup and is available on demand rather than repeated at the decision point.

Profile creation is progressive, not a mandatory form before value is shown. Reusable preferences are saved only after the user sees why they are needed.

## 6. Core flow

```text
Alexa request
  → prepare_task
  → progressive setup (when required)
  → review plan and boundaries
  → start_assisted_call
  → IVR / queue / agent progress
  → answer_pending_choice (money involved)
  → representative is live
  → secure hot handoff (AI disconnected; user invited into the existing call)
  → resume_assisted_call
  → callback waiting
  → resolution with evidence
  → case history
```

## 7. Authorization policy

| Action type | Default behavior |
|---|---|
| Navigate IVR, wait on hold, explain goal | Automatic |
| Share task-scoped profile fields | Automatic after task confirmation |
| Choose paperless billing | Automatic when saved as a preference |
| Accept a charge, deposit, autopay, contract, cancellation | Ask the user |
| Provide OTP, PIN, SSN, DOB, card information | Never sent to the model; secure handoff |

## 8. Secure handoff interaction model

The user must never wait in a second customer-service queue. Waitless remains on the original call through IVR and hold. Only after a live representative requests account-holder verification does Waitless invite the user into that same call.

```text
Phase A — delegated call
Representative ↔ conference bridge ↔ Waitless AI

Phase B — hot handoff
Representative is asked to hold
Conference bridge calls the user's verified phone

Phase C — private verification
Representative ↔ conference bridge ↔ user
AI participant removed; controlled recording and transcription off

Phase D — return
User marks verification complete
Waitless AI rejoins; user may leave
```

The Companion shows three separate states:

1. **Representative ready:** the person is already on the line; the user will not enter a queue.
2. **Secure bridge ringing:** the system is calling the verified phone ending in `0147`.
3. **Direct call:** a phone-style interface with timer, mute, speaker and keypad; no transcript or chat composer is displayed.

The demo button reads **Simulate answering** unless a real test conference is configured. Production uses an ordinary system phone call first; a native-app WebRTC endpoint is a later option.

The refined handoff screen prioritizes the connected representative, provider, task, and masked phone number. It uses one privacy note rather than a numbered infrastructure explanation. The simulated call view uses a live elapsed timer, visual mute/speaker toggles, a demo keypad, and a primary “Return to Waitless” action. These web controls simulate interaction only; production system-phone controls belong to the operating system.

## 9. MCP tools demonstrated

- `prepare_task(goal, provider_hint)`
- `save_task_context(task_id, phone, service_address, consent)`
- `start_assisted_call(task_id)`
- `get_call_status(task_id)`
- `answer_pending_choice(task_id, choice_id, answer)`
- `begin_identity_handoff(task_id)`
- `resume_assisted_call(task_id, verification_completed)`
- `get_case_result(task_id)`

The handoff tools return explicit media state, not just a conversational promise:

```json
{
  "representative_connected": true,
  "user_bridge": "ringing",
  "ai_media": "disconnected",
  "recording": false,
  "transcription": false
}
```

## 10. Demo data

- Fictional provider: Lakeshore Energy
- Goal: Start electric service on November 1
- Consequential choice: refundable deposit versus autopay enrollment
- Verification: the representative is already connected; the user's verified phone is invited into the existing call; no sensitive value is entered in the UI
- Outcome: service scheduled, deposit waived, confirmation `LE-48291`

## 11. Success criteria

User-triggered tool requests show a processing spinner for about 1.3 seconds and disable duplicate submissions. The simulated call automatically advances through dialing (1.8s), phone menu (2.2s), hold (2.8s), and representative conversation (2.4s), then presents the approval request. Restart cancels pending progression. These shortened timings are for the demo.

- A first-time viewer understands the product within 15 seconds.
- The end-to-end demo can be completed in under 2 minutes.
- Every consequential action has an explicit user decision.
- The identity handoff visibly states that AI listening, recording, and transcription are off.
- The user understands that the representative is already online and there is no second wait.
- The MCP trace proves that the UI is driven by tool calls rather than a prerecorded animation.
- The final screen distinguishes a verified result from a promise that still needs follow-up.

## 12. Visual direction

Use an Alexa-inspired, assistant-forward layout: warm near-white canvas, dark ink typography, soft lavender/blue ambient gradients, spacious chat rhythm, rounded tool cards, and a persistent composer. Do not copy Amazon logos, proprietary artwork, or exact branded assets. Waitless cards use restrained green accents to make the capability legible while remaining visually native to the host assistant.

The reference viewport is **393 × 852 CSS pixels** (modern 6.1-inch iPhone class). On an actual phone the experience fills the viewport without a decorative device frame. Desktop preview centers that exact mobile canvas rather than stretching the interface. Primary body copy targets 12–13 px inside the compact simulator and scales with the browser's device pixel ratio; controls retain at least a 44 px effective touch target on mobile.

Pre-call cards should answer five questions in scan order: **who is being called, what will be requested, when it should take effect, which task-scoped details will be shared, and what happens after Start**. Avoid taxonomy such as “Can do / Ask first / Only you provide” in the primary card; it is system language with low information value for the immediate decision.
