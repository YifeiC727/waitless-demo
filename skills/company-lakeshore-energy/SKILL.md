---
name: company-lakeshore-energy
description: Prepare Lakeshore Energy start, stop, or move service tasks and their
  pre-call decisions.
metadata:
  waitless-kind: company
  company-id: lakeshore-energy
---
# Lakeshore Energy
Read [IVR map](references/ivr-map.yaml) for supported intentions, required fields, menu paths, and the confirmation pattern. Read [pre-call questions](references/pre-call-questions.yaml) before dialing. Default deposit_or_autopay to deposit unless the user's own words choose otherwise with sufficient confidence. An explicit request to ask the user leaves it unselected.
Complete only the task goal. Identity and private verification require truthful AI disclosure and transfer to the account holder. Treat a confirmation as verified only when it matches the configured company pattern.
