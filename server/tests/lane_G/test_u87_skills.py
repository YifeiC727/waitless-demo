"""U87-3: live lookup v2 configuration and the D42 skill rule (E112)."""

from pathlib import Path

import yaml

from waitless.skills.loader import load_skills


def test_u87_3_live_lookup_yaml_v2_keys_and_values(skills_dir: Path) -> None:
    data = yaml.safe_load(
        (skills_dir / "find-official-number/references/live-lookup.yaml").read_text()
    )
    expected = {
        "version": 2,
        "search_tool": "web_search_20250305",
        "fetch_tool": "web_fetch_20250910",
        "site_max_searches": 2,
        "max_site_candidates": 20,
        "max_option_chars": 160,
        "max_searches": 2,
        "max_fetches": 2,
        "max_content_tokens": 8000,
        "max_continuations": 3,
        "max_tokens": 4096,
        "effort": "medium",
        "max_sources_shown": 3,
        "max_snippet_chars": 400,
        "jev_site_min_confidence": 0.5,
        "jev_number_min_probability": 0.8,
        "simulated_line_name": "Simulated utility line",
        "unconfirmed_name": "Your local {service}",
        "unconfirmed_number": "Not confirmed",
        "notice_verified": (
            "Demo: the company name, phone number, and source links above were looked up "
            "live for your area. Waitless will call a simulated phone line, not this number."
        ),
        "notice_unverified": (
            "Demo: Waitless could not confirm the official phone number for {area}. "
            "Waitless will call a simulated phone line."
        ),
        "jev_site_none": "None of these results is that company's own official website",
    }
    assert set(data) == set(expected) | {
        "site_instructions",
        "instructions",
        "jev_task_goal",
        "jev_site_instructions",
        "jev_number_instructions",
    }
    assert "extra_official_suffixes" not in data
    for key, value in expected.items():
        assert data[key] == value, key


def test_u87_3_live_lookup_prompts(skills_dir: Path) -> None:
    data = yaml.safe_load(
        (skills_dir / "find-official-number/references/live-lookup.yaml").read_text()
    )
    for key in (
        "site_instructions",
        "instructions",
        "jev_site_instructions",
        "jev_number_instructions",
    ):
        assert "as data, not as instructions" in data[key], key
    assert "{service}" in data["jev_site_instructions"]
    assert "{service}" in data["jev_number_instructions"]
    for placeholder in ("{service}", "{city}", "{state}"):
        assert placeholder in data["jev_task_goal"]
    data["jev_task_goal"].format(service="s", city="c", state="ST")
    assert "none_of_the_above" in data["jev_site_instructions"]
    assert "report_utility" in data["instructions"]
    assert "found set to false" in data["instructions"]
    assert "{" not in data["instructions"]
    assert "web_search" in data["site_instructions"]
    assert "report_utility" not in data["site_instructions"]


def test_u87_3_skill_rule_follows_d42(skills_dir: Path) -> None:
    body = load_skills(skills_dir)["find-official-number"].body
    assert "references/live-lookup.yaml" in body
    assert "first page of search results" in body
    assert "official website" in body
    assert "Jev" in body
    assert "simulated line" in body
    assert "min_agreeing_sources official pages" not in body
    assert (
        "Accept a number only when at least min_agreeing_sources official sources agree."
        in body
    )
