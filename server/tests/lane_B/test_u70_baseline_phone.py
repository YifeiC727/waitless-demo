"""U70-4: baseline requests a phone number on the keypad."""

from waitless.company.config import load_scenario


PHONE_TEXT = "Please enter your phone number, followed by the pound key."


def test_u70_4_baseline_agent_asks_for_phone_on_keypad(company_dir):
    agent = load_scenario(company_dir, "lakeshore-energy", "baseline").steps["agent"]
    assert agent.verifies == "phone_number"
    assert agent.text_script[2].say == PHONE_TEXT
    assert agent.text_script[2].expect.intent == "press_field:phone_number"
    persona = agent.persona.lower()
    assert "phone number" in persona
    assert "special approval" in persona
    assert "account number" not in persona
    assert len(agent.text_script) == 4
    assert all(line.expect.intent != "press_field:account_number" for line in agent.text_script)
