---
name: find-official-number
description: Verify a supported company phone number against agreeing official sources
  before preparing a call.
metadata:
  waitless-kind: harness
---
# Find an official number
Read [trusted sources](references/trusted-sources.yaml). Accept a number only when at least min_agreeing_sources official sources agree. Show all consulted sources, including disagreements. An unverified number leaves the task in setup_required; do not silently dial it.
