---
name: handoff-to-user
description: Bring the account holder into the same call when a live representative
  needs private verification.
metadata:
  waitless-kind: harness
---
# Handoff to the user
Read [handoff templates](references/handoff.yaml). Only live_rep or identity_check permits transfer. Otherwise request a representative. Announce the transfer before handing over the same line. Disconnect AI audio, recording, and transcription while the user speaks. On return, resume with the configured template; never ask the AI to supply SSN, date of birth, or card information.
