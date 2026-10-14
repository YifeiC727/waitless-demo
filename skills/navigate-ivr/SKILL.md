---
name: navigate-ivr
description: Parse automated telephone menus and supply choices to Jev before the
  program sends DTMF.
metadata:
  waitless-kind: harness
---
# Navigate IVR
Read [IVR rules](references/ivr-rules.yaml) for press_patterns, digit_words, operator, repeat, and safe defaults. Present parsed digits with labels to Jev. Wait until the full company turn ends. The program sends keys; Sonic does not speak digits or press keys. A none choice uses the configured operator if present, then repeat, then requests clarification.
