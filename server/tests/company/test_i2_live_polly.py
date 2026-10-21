"""I-2 联调时跑（需要真实 AWS 凭证；默认测试按 needs_keys 标记排除）。

命令（仓库根目录）：`scripts/load-keys-macos.sh bash -c 'cd server && uv run pytest -m needs_keys tests/company/test_i2_live_polly.py'`
通过条件：1 passed。被显式选中却缺凭证时失败，不跳过（C14）。
"""

import os
from pathlib import Path

import pytest

from waitless.company.config import load_company
from waitless.company.ivr_audio import synthesize_prompt

from ._scenario_data import COMPANY_ID
from ._signals import WIRE_RATE, rms_dbfs


@pytest.mark.needs_keys
def test_ac95_live_polly_synthesizes_ivr_prompt(company_dir: Path) -> None:
    missing = [n for n in ("AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY") if not os.environ.get(n)]
    if missing:
        pytest.fail("needs_keys test selected without credentials: " + ", ".join(missing))
    import boto3

    company = load_company(company_dir, COMPANY_ID)
    client = boto3.client("polly", region_name=os.environ["AWS_REGION"])
    pcm = synthesize_prompt(
        "Thank you for calling Lakeshore Energy.", voice=company.voices.ivr_polly, polly_client=client, sample_rate=WIRE_RATE
    )
    assert len(pcm) % 2 == 0
    assert len(pcm) // 2 >= WIRE_RATE // 2, "expected at least half a second of speech"
    assert rms_dbfs(pcm) > -45.0
