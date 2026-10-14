"""B-r3: the transfer announcement must not trigger a consequential answer."""

import re
from pathlib import Path

import yaml


def test_agent_transfer_announces_special_approval_and_expects_wait(company_dir: Path) -> None:
    scenario = yaml.safe_load(
        (company_dir / "lakeshore-energy" / "scenarios" / "baseline.yaml").read_text(
            encoding="utf-8"
        )
    )
    lines = scenario["steps"]["agent"]["text_script"]
    assert len(lines) == 4
    last_line = lines[-1]
    assert last_line["say"] == (
        "Your new service needs a special approval. "
        "Let me transfer you to a representative."
    )
    assert re.search(r"\b(?:deposit|fee|autopay|contract|cancel)\b|\$", last_line["say"], re.I) is None
    assert last_line["expect"]["intent"] == "wait"
