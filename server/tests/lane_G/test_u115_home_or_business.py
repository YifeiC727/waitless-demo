"""U115: energy notes settle residential menus from the task data."""

from g_support import profile, standing_rules, task_card
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills


HOME_OR_BUSINESS = (
    "When the phone system asks whether the new service is for a home or a "
    "business, use the task data: for a house or an apartment, say "
    '"My home" yourself unless the task specifies business service; for business '
    'service, say "My business". If the task does not settle the choice, follow '
    "Getting help. Save interruptions for the representative's identity check "
    "and the account holder's deposit decision."
)


def test_energy_prompt_settles_home_or_business(skills_dir, test_profile):
    card = task_card(precall=False, goal="Start electric service at my new apartment")
    compiled = compile_call(
        card, load_skills(skills_dir), profile(test_profile), standing_rules(), []
    )

    assert HOME_OR_BUSINESS in compiled.sonic_prompt.splitlines()


def test_bank_prompt_omits_energy_home_or_business_guidance(skills_dir, test_profile):
    card = task_card(
        company_id="bluefin-harbor-bank", precall=False,
        goal="Dispute a duplicate charge from Quillmark Grocers",
    )
    compiled = compile_call(
        card, load_skills(skills_dir), profile(test_profile), standing_rules(), []
    )

    assert "# Bluefin Harbor Bank" in compiled.sonic_prompt
    assert HOME_OR_BUSINESS not in compiled.sonic_prompt
    assert 'say "My home" yourself' not in compiled.sonic_prompt
