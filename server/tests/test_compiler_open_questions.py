"""AC-44：Sonic 提示要求开放式问题按任务目标自己组织语言（G 线；tech §6.9 第 2、4 节；第 3 步可见测试）。"""

import re
from pathlib import Path

from g_support import GOAL, profile, require_skills_dir, standing_rules, task_card
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills

OPEN_RULE = re.compile(r"open[- ]ended|open question", re.I)


def test_ac44_prompt_states_goal_and_open_question_rule(skills_dir: Path, test_profile: dict) -> None:
    skills = load_skills(require_skills_dir(skills_dir))
    compiled = compile_call(task_card(), skills, profile(test_profile), standing_rules(), [])
    assert GOAL in compiled.sonic_prompt, "section 2 carries the task goal the open answers follow"
    assert OPEN_RULE.search(compiled.sonic_prompt), "section 4 must cover open-ended questions"


def test_ac44_goal_follows_the_task_card(skills_dir: Path, test_profile: dict) -> None:
    skills = load_skills(require_skills_dir(skills_dir))
    other = "Move electric service to the new apartment on December 3"
    compiled = compile_call(task_card(goal=other), skills, profile(test_profile), standing_rules(), [])
    assert other in compiled.sonic_prompt
    assert GOAL not in compiled.sonic_prompt
