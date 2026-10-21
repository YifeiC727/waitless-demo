---
name: find-official-number
description: Verify a supported company phone number against agreeing official sources
  before preparing a call.
metadata:
  waitless-kind: harness
---
# Find an official number
Read [trusted sources](references/trusted-sources.yaml). Accept a number only when at least min_agreeing_sources official sources agree. Show all consulted sources, including disagreements. An unverified number leaves the task in setup_required; do not silently dial it.

When live lookup is on, also read [live lookup](references/live-lookup.yaml). Look up the real company that provides the company skill's live-lookup-service for the user's city and state, sending only the city and state. First search the web, and let Jev pick, from the first page of search results, the official website of that company. Then look for the number on that website only. Show the company name, the number, and the pages of that website that show it on the task card only when the number appears in text from that website and Jev judges that it is that company's customer service number for starting, stopping, or moving service; do not cross-check other websites. Otherwise show it as not confirmed. Always show the demo notice. The call itself always goes to the simulated line, never to the number found.
