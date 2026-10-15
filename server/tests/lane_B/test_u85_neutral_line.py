"""U85-5: simulated customer service speech never names a company."""

from waitless.company.config import load_company, load_scenario


def test_u85_5_baseline_greeting_is_neutral(company_dir):
    steps = load_scenario(company_dir, "lakeshore-energy", "baseline").steps
    assert steps["greeting"].text == (
        "Thank you for calling. You have reached a simulated utility customer service line."
    )


def test_u85_5_nothing_spoken_names_a_company(company_dir):
    company = load_company(company_dir, "lakeshore-energy")
    scenario = load_scenario(company_dir, company.company_id, "baseline")
    spoken = [("dead_end_text", company.dead_end_text)]
    for step_id, step in scenario.steps.items():
        spoken.extend([
            (f"{step_id}.text", step.text),
            (f"{step_id}.persona", step.persona),
        ])
        spoken.extend(
            (f"{step_id}.text_script[{index}].say", line.say)
            for index, line in enumerate(step.text_script or [])
        )
        spoken.extend(
            (f"{step_id}.actions[{index}].line", action.line)
            for index, action in enumerate(step.actions or [])
        )
    for field, text in spoken:
        if text is not None:
            assert "lakeshore" not in text.casefold(), field
            assert company.name.casefold() not in text.casefold(), field


def test_u85_5_personas_describe_the_simulated_line(company_dir):
    steps = load_scenario(company_dir, "lakeshore-energy", "baseline").steps
    for step_id in ("agent", "rep"):
        persona = steps[step_id].persona
        assert persona is not None, step_id
        assert "simulated utility customer service line" in persona, step_id
        assert "Do not say a company name." in persona, step_id
