"""K1: every accepted cue must retain its wait semantics at word boundaries."""

from pathlib import Path

import pytest

from waitless.company.config import load_company, load_scenario
from waitless.company.text import TextCompany


def at_offer(company_dir: Path, tmp_path: Path, cues: list[str]) -> TextCompany:
    import yaml

    scenario = load_scenario(company_dir, "lakeshore-energy", "menu-reworded")
    scenario.steps["rep"].hold_cues = cues
    directory = tmp_path / "lakeshore-energy" / "scenarios"
    directory.mkdir(parents=True)
    (directory / "menu-reworded.yaml").write_text(yaml.safe_dump(scenario.model_dump()), encoding="utf-8")
    scenario = load_scenario(tmp_path, "lakeshore-energy", "menu-reworded")
    company = load_company(company_dir, "lakeshore-energy")
    text = TextCompany(scenario, company)
    text.start()
    for digits in scenario.expected.menu_digits:
        text.on_digits(digits)
    text.on_speech("Service address")
    text.on_speech("Start date")
    text.on_digits(company.customers[0].account_number + "#")
    lines = text.on_speech("I am an AI assistant.")
    assert lines[-1].expect.intent == "ask_user"
    return text


@pytest.mark.parametrize(
    ("cue", "speech"),
    [
        (" hold ", "HOLD, please."),
        (" one moment ", "ONE MOMENT, please."),
        ("hold!", "Please HOLD!"),
        ("(one moment)", "Please wait (ONE MOMENT)."),
    ],
)
def test_accepted_cue_edges_do_not_advance_the_offer(company_dir: Path, tmp_path: Path, cue: str, speech: str) -> None:
    text = at_offer(company_dir, tmp_path, [cue])
    assert text.on_speech(speech) == []
    assert not text.ended
    lines = text.on_speech("The account holder chose: on November 3 at no charge.")
    assert lines[-1].expect.intent == "confirm_and_end"
    assert lines[-1].confirmation_number


@pytest.mark.parametrize("speech", ["The household chose November 3.", "Withhold the extra fee."])
def test_cue_does_not_match_inside_a_word(company_dir: Path, tmp_path: Path, speech: str) -> None:
    text = at_offer(company_dir, tmp_path, [" hold "])
    lines = text.on_speech(speech)
    assert lines[-1].expect.intent == "confirm_and_end"
