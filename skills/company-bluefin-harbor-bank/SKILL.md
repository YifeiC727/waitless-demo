---
name: company-bluefin-harbor-bank
description: Prepare questions about charges on a Bluefin Harbor Bank credit card, such as a possible duplicate charge, for the simulated Bluefin Harbor Bank card services line.
metadata:
  waitless-kind: company
  company-id: bluefin-harbor-bank
---
# Bluefin Harbor Bank
Read [IVR map](references/ivr-map.yaml) for supported intentions, required fields, menu paths, and the reference number pattern. Read [pre-call questions](references/pre-call-questions.yaml) for choices the bank may raise during the call. Never ask them before dialing; list one on the task card only when the user's own words already chose an option with sufficient confidence.
Complete only the task goal. Never share card numbers or other private details. Treat a reference number as verified only when it matches the configured pattern.
