"""Explicit YAML null values must not bypass optional step-field validation."""

import shutil
from pathlib import Path

import pytest
import yaml

from waitless.company.config import ScenarioError, load_scenario


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hold_cues", None),
        ("simulated_user", None),
        ("simulated_user", {"choose": None}),
        ("simulated_user", {"handoff_exit": None}),
    ],
)
def test_explicit_null_step_extras_are_rejected(company_dir: Path, tmp_path: Path, field: str, value) -> None:
    root = tmp_path / "company"
    shutil.copytree(company_dir, root)
    path = root / "lakeshore-energy" / "scenarios" / "menu-reworded.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["steps"]["rep"][field] = value
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ScenarioError):
        load_scenario(root, "lakeshore-energy", "menu-reworded")
