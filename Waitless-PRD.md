# Waitless — Alexa+ MCP Experience PRD

## 1. Product definition

Waitless is not a standalone calling app. It is a reusable customer-service calling capability that an AI assistant can invoke through MCP. Alexa+ is the primary conversational surface; the Waitless Companion is a lightweight mobile web surface used only when visual review, consent, identity handoff, or case evidence is required.

**Positioning:** “Ask Alexa. Waitless handles the call.”

**Hackathon framing:** Alexa+ simulated experience backed by a self-hosted MCP server using Streamable HTTP. The original fictional-utility electricity case and the added Costco membership case are available in parallel. Costco is a real organization; all conversations, account changes, refunds, and confirmations in this prototype are invented. A live-demo variant may place a real PSTN call only to a team-controlled Amazon Connect/Twilio test contact center with every participant's consent.

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
- No claim of official Alexa/Amazon/Costco affiliation or production Alexa+ connectivity.
- No general-purpose standalone phone dialer.

## 4. Surfaces

### Alexa+ conversational surface

Primary entry point. The user describes a task naturally. Alexa+ invokes Waitless, renders structured tool results as compact cards, and reports progress and completion.

### Waitless Companion

Opened only from an Alexa+ tool card or notification. It handles:

- progressive setup and handoff-phone review;
- review of task scope and authorization boundaries;
- consequential approvals;
- secure identity handoff;
- evidence, confirmation numbers, and case history.

The Companion is intentionally shallow. The hackathon version has no persistent bottom navigation. Cases return to a single case overview.

### Voice input

The conversational composer supports dictation through a microphone affordance with recording feedback, elapsed time, cancel, and finish. A transcript is inserted into the draft for review before sending. The prototype defaults to a clearly labeled simulated voice example, with no microphone capture. `window.WaitlessVoice.endpoint` or a custom transcription adapter enables real MediaRecorder capture and backend transcription. Voice capture is canceled on navigation, reset, page exit, or backgrounding, and never runs during identity handoff. See `VOICE-INPUT.md` for the integration contract.

### MCP trace

A developer-facing drawer shows protocol events, tool names, arguments, results, timestamps, and transport mode. It is hidden by default so technical detail does not contaminate the consumer UX.

## 5. First-use experience

Both scenarios use one shared workflow rather than separate screens. `FIELD_SCHEMA` defines stable labels and keys: `name` → Name, `phone_number` → Phone number, `address` → Address, and `membership_last_four` → Membership last four digits. Electricity selects name/phone/address; Costco selects name/phone/membership hint. Setup, pre-call review, and final approval are shared renderers whose content comes from `CASES`. Name and phone are retained during same-session case switching. Only the active case's fields are sent to the tool. See `FIELD-CONTRACT.md`.

1. User selects or describes the electricity or Costco task in Alexa+.
2. Alexa+ shows loading and calls `prepare_task` with the goal and active fields from a saved demo profile. Demo assumes registration and account hints already exist; no actual registration or persistent profile is implemented.
3. Waitless checks required fields and returns `ready` for the complete sample profile. Only missing information requires a follow-up.
4. A single chat card summarizes provider, request, Name and masked Phone number, plus the service Address for electricity. No mandatory Check your details page.
5. Optional Edit details opens the shared editor; Save details updates the same card.
6. User explicitly clicks Start call. No countdown, timeout consent, or automatic dialing after silence.
7. Progress, identity handoff and separate explicit final approval continue unchanged.

The pre-call card prioritizes provider, request, name and masked phone. Account hints stay in the saved profile and optional editor instead of repeating every field. Electricity also shows the service address. A short sentence distinguishes permission to contact from final approval.

Demo assumes a registered sample profile. Real profile creation is outside this prototype; missing data should be requested progressively rather than requiring a full form.

## 6. Core flow

```text
Alexa request
  → prepare_task
  → progressive setup (when required)
  → review plan and boundaries
  → start_assisted_call
  → IVR / queue / agent progress
  → answer_pending_choice (cancel membership or only stop auto-renewal)
  → representative is live
  → secure hot handoff (AI disconnected; user invited into the existing call)
  → resume_assisted_call
  → confirmation waiting (advances automatically in this scenario)
  → resolution with evidence
  → case history
```

## 7. Authorization policy

Confirmation rationale: reuse a saved sample profile instead of requiring a setup sheet. The single pre-call card confirms who will be contacted and the delegated request, not final cancellation authority. Optional editing is a correction path, not a required gate. In-call approval asks which consequence the user accepts after alternatives are explicit. Never treat silence as approval. No extra “are you sure?” dialog follows approval.

Starting the call authorizes only member-services inquiry and task-scoped information sharing. The final selected membership change is authorized separately. Full identifiers or authentication credentials, if required, are provided directly to the representative during handoff. The sample account last four digits are an account hint, not identity proof.

Scenario grounding: [Costco cancellation help](https://customerservice.costco.com/app/answers/answer_view/a_id/1308/~/how-can-i-cancel-my-costco-membership%3F) describes cancellation through the primary member and a live member-services agent. [Auto-renewal help](https://customerservice.costco.com/app/answers/detail/a_id/1812) describes managing auto-renewal separately. This does not establish that Costco accepts third-party AI delegation. IVR steps, approval sequencing, confirmation timing, and refund status are demo narrative, not Costco policy promises.

| Action type | Default behavior |
|---|---|
| Navigate IVR, wait on hold, explain goal | Automatic |
| Share task-scoped profile fields | Automatic after task confirmation |
| Cancel membership or turn off auto-renewal | Explicit case-specific approval; never remembered as a blanket preference |
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

The demo button reads **Answer demo call** unless a real test conference is configured. Production uses an ordinary system phone call first; a native-app WebRTC endpoint is a later option.

The refined handoff screen prioritizes the connected representative, provider, task, and masked phone number. It uses one privacy note rather than a numbered infrastructure explanation. The simulated call view uses a live elapsed timer, visual mute/speaker toggles, a demo keypad, and a primary “Return to Waitless” action. These web controls simulate interaction only; production system-phone controls belong to the operating system.

## 9. MCP tools demonstrated

- `prepare_task(goal, provider_hint)`
- `save_task_context(task_id, scenario, name, phone_number, address?, membership_last_four?, scope)`
- `start_assisted_call(task_id)`
- `get_call_status(task_id)`
- `answer_pending_choice(task_id, choice_id, answer)`
- `begin_identity_handoff(task_id)`
- `resume_assisted_call(task_id, verification_completed)`
- `get_case_result(task_id, approved_change)`

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

### Streamlined identity handoff

The chat's Join demo call action sends `begin_identity_handoff`, shows loading, and enters the private simulated call directly. There is no extra Ready when you are / Answer demo call page. Verification remains user-controlled and Return to Waitless remains explicit. All audio is simulated. MCP header pills are removed from both host and Companion; append `?debug=1` (or `&debug=1` alongside a case parameter) to reveal the Developer trace button for hackathon technical demonstrations. Trace collection is unchanged.

Two cases are available in parallel from the home screen. Both use the same automatic profile check, optional details editor, explicit pre-call confirmation, and final-approval steps. The electricity scenario retains its service-start, deposit/autopay, callback, and outcome content; Costco contributes membership-specific content to the shared workflow.

### Original case — electric service (`WL-2048`)

- Fictional provider: Lakeshore Energy
- Goal: start electric service on November 1
- Context: handoff phone and service address
- Decision: refundable deposit versus autopay
- Flow: existing setup, pre-call confirmation, paced call, approval, private handoff, callback waiting, and history
- Result: simulated service confirmation `LE-48291`
- Voice demo transcript: electricity-service request

### Additional case — Costco membership (`WL-2049`)

- Scenario provider: Costco member services; simulated interaction, no affiliation
- Goal: Cancel membership and ask about a membership-fee refund
- Consequential choice: cancel now versus only disable auto-renewal; no option is preselected
- Verification: the representative is already connected; the user's verified phone is invited into the existing call; no sensitive value is entered in the UI
- Outcome: cancellation confirmed in the demo, refund requested but not received; alternate path disables auto-renewal and leaves membership active. Confirmation `DEMO-CM-48291`.

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
