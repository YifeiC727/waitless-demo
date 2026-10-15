"""U85-4: live lookup configuration and skill declarations (E102–E104)."""

from pathlib import Path

import yaml

from waitless.skills.loader import load_skills


def test_u85_4_live_lookup_yaml_has_no_names_or_urls(skills_dir: Path) -> None:
    text = (skills_dir / "find-official-number/references/live-lookup.yaml").read_text()
    assert "://" not in text
    assert "lakeshore" not in text.casefold()


def test_u85_4_company_skill_declares_lookup_service(skills_dir: Path) -> None:
    skill = load_skills(skills_dir)["company-lakeshore-energy"]
    assert skill.metadata == {
        "waitless-kind": "company",
        "company-id": "lakeshore-energy",
        "live-lookup-service": "electric utility",
    }
    assert skill.description == (
        "Prepare start, stop, or move electric service tasks for the simulated electric "
        "utility line that this demo calls for every electric service request, whichever "
        "real utility serves the address."
    )
    assert "lakeshore" not in skill.description.casefold()


def test_u85_4_find_official_number_skill_mentions_live_lookup(skills_dir: Path) -> None:
    body = load_skills(skills_dir)["find-official-number"].body
    assert "references/live-lookup.yaml" in body
    assert "simulated line" in body
