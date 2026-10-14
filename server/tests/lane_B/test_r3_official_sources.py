"""B-r3: both official source fixtures publish one consistent number."""

import re
from pathlib import Path

import pytest
import yaml


PHONE_NUMBER = re.compile(
    r"(?<!\d)(?:\+?1[\s.-]*)?(?:\(\d{3}\)|\d{3})[\s.-]*\d{3}[\s.-]*\d{4}(?!\d)"
)


@pytest.mark.parametrize("page_name", ["contact.html", "start-service.html"])
def test_official_source_has_one_company_number(company_dir: Path, page_name: str) -> None:
    company = yaml.safe_load(
        (company_dir / "lakeshore-energy" / "company.yaml").read_text(encoding="utf-8")
    )
    page = company_dir / company["company_id"] / company["website"]["dir"] / page_name
    assert page.is_file(), f"missing official source fixture: {page_name}"
    html = page.read_text(encoding="utf-8")
    assert "fictional" in html.lower()
    assert company["line_number"] in html
    numbers = PHONE_NUMBER.findall(html)
    assert len(numbers) == 1, f"expected one telephone number in {page_name}: {numbers}"
    assert {re.sub(r"\D", "", number) for number in numbers} == {
        re.sub(r"\D", "", company["line_number"])
    }
