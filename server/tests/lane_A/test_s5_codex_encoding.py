import sys

import pytest

from waitless.bedrock import CodexCliConverseClient, CodexCliError, _run_codex
from waitless.models import ConverseCall


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_codex_decode_failure_is_private_and_does_not_retry(stream):
    calls = []

    async def runner(command, prompt, timeout):
        calls.append(command)
        return await _run_codex(command, prompt, timeout)

    command = [sys.executable, "-c", f"import sys; sys.{stream}.buffer.write(b'\\xffprivate-output-marker')"]
    client = CodexCliConverseClient(command=command, timeout_s=5, runner=runner)
    call = ConverseCall(task="phrase_reply", model_id="fixture", system=[{"text": "private-prompt-marker"}],
                        messages=[{"role": "user", "content": [{"text": "private-user-marker"}]}])
    try:
        with pytest.raises(CodexCliError) as caught:
            await client.converse(call)
        assert "private" not in str(caught.value)
        assert caught.value.__suppress_context__
    finally:
        assert len(calls) == 1
