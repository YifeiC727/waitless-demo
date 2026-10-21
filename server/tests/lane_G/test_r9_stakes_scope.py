"""I2B-3: non-questions and partial company turns do not inherit earlier risk."""

from g_support import point_questions


def test_stakes_zero_without_a_company_question_about_money_contract_or_change(skills_dir):
    text = point_questions(skills_dir, "call_round")["stakes"]["instructions"]
    assert "If the last company utterance does not itself ask about money, a contract, or a change to the account" in text
    assert "collect details or verify the account" in text
    assert "mid-sentence, score 0" in text
    assert "only Waitless mentioned earlier do not count as the company asking" in text
