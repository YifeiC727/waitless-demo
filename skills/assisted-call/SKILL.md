---
name: assisted-call
description: Prepare and monitor assisted calls through the Waitless MCP tools, with
  the user starting each call.
metadata:
  waitless-kind: client
---
# Assisted call
Use prepare_task before calling. Show the task card and confirm its goal in one sentence. Wait for the user's Start click; never start, answer a pending choice, join, or return on the user's behalf. While a call runs, poll get_call_status. Fetch get_case_result after it ends. Use get_case_result before creating a follow-up call so the next task covers only unfinished work.
Use concise English replies with facts from tool results. Jev selects each next step; the language model extracts facts and phrases replies.
Read [questions](jev/questions.yaml) for decision points and [policy](policy.yaml) for actions and boundaries.
