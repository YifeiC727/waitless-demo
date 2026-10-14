import pytest

from waitless.bedrock import CodexCliConverseClient, CodexCliError
from waitless.models import ConverseCall


@pytest.mark.parametrize(
    "stdout,schema",
    [
        ("output-marker: not JSON", {"type": "object"}),
        ('{"value": "output-marker"}', {"type": "object", "properties": {"value": {"type": "integer"}}}),
        ('{"value": "output-marker"}', {"type": "invalid-schema-type"}),
    ],
    ids=["invalid-json", "invalid-input", "invalid-schema"],
)
async def test_forced_output_errors_are_private_and_do_not_retry(stdout, schema):
    calls = []

    async def runner(argv, prompt, timeout_s):
        calls.append((argv, prompt, timeout_s))
        return 0, stdout, "stderr-marker"

    call = ConverseCall(
        task="extract_request",
        model_id="fixture-model",
        system=[{"text": "prompt-marker"}],
        messages=[{"role": "user", "content": [{"text": "user-marker"}]}],
        tool_config={
            "tools": [{"toolSpec": {"name": "extract", "description": "Extract", "inputSchema": {"json": schema}}}],
            "toolChoice": {"tool": {"name": "extract"}},
        },
    )
    client = CodexCliConverseClient(command=["fixture-command"], timeout_s=1, runner=runner)
    try:
        with pytest.raises(CodexCliError) as caught:
            await client.converse(call)
        for marker in ("output-marker", "prompt-marker", "user-marker", "stderr-marker", "invalid-schema-type"):
            assert marker not in str(caught.value)
    finally:
        assert len(calls) == 1
