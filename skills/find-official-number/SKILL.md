---
name: find-official-number
description: Verify a supported company phone number against agreeing official sources
  before preparing a call.
metadata:
  waitless-kind: harness
---
# Find an official number
Read [trusted sources](references/trusted-sources.yaml). Accept a number only when at least min_agreeing_sources official sources agree. Show all consulted sources, including disagreements. An unverified number leaves the task in setup_required; do not silently dial it.

When live lookup is on, also read [live lookup](references/live-lookup.yaml). Look up the real company that provides the company skill's live-lookup-service for the user's city and state, sending only the city and state. Show its name, number, and sources on the task card only when at least min_agreeing_sources official pages show the same number, at least one of them on the company's own domain, and the company name appears on one of them; otherwise show it as not confirmed. Always show the demo notice. The call itself always goes to the simulated line, never to the number found.
