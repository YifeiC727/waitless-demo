---
name: company-lakeshore-energy
description: Prepare start, stop, or move electric service tasks for the simulated electric utility line that this demo calls for every electric service request, whichever real utility serves the address.
metadata:
  waitless-kind: company
  company-id: lakeshore-energy
  live-lookup-service: electric utility
---
# Lakeshore Energy
Read [IVR map](references/ivr-map.yaml) for supported intentions, required fields, and the confirmation pattern. A nonempty menu path is reference metadata, never permission to press keys in advance. For this company the paths are intentionally empty because no stable public ComEd key sequence is documented: listen to the complete relevant menu and let the live keypad decision choose only from options actually heard. Read [pre-call questions](references/pre-call-questions.yaml) for choices the company may raise during the call. Never ask them before dialing; list one on the task card only when the user's own words already chose an option with sufficient confidence. When the company raises one, ask the account holder if this call has not interrupted them yet; otherwise use the declared default and say it is the default, or end for follow-up when there is no default.
Complete only the task goal. Identity and private verification require truthful AI disclosure and transfer to the account holder. Treat a confirmation as verified only when it matches the configured company pattern.
