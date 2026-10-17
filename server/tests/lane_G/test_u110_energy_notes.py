"""U110: public utility facts must not become invented call choices."""


def test_energy_notes_do_not_invent_deposit_autopay_tradeoff(skills_dir):
    notes = (
        skills_dir / "company-lakeshore-energy/references/agent-notes.md"
    ).read_text(encoding="utf-8").lower()

    assert "do not assume an option number" in notes
    assert "do not present autopay as a way to replace or waive a deposit" in notes
    assert "there is no default" in notes
    assert "default is the deposit" not in notes
