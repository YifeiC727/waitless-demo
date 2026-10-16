"""AC-44：phone-agent 提示携带任务目标与自然回答规则（G 线；tech E131；第 3 步可见测试）。"""

import json
from pathlib import Path

from g_support import GOAL, profile, require_skills_dir, standing_rules, task_card
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills

def test_ac44_prompt_states_goal_and_open_question_rule(skills_dir: Path, test_profile: dict) -> None:
    skills = load_skills(require_skills_dir(skills_dir))
    compiled = compile_call(task_card(), skills, profile(test_profile), standing_rules(), [])
    data = json.loads(compiled.sonic_prompt.split("\n\n# Task data\n", 1)[1])
    assert data["goal"] == GOAL
    assert "answer what was just asked" in compiled.sonic_prompt
    assert "Work only toward the goal in the task data" in compiled.sonic_prompt


def test_ac44_goal_follows_the_task_card(skills_dir: Path, test_profile: dict) -> None:
    skills = load_skills(require_skills_dir(skills_dir))
    other = "Move electric service to the new apartment on December 3"
    compiled = compile_call(task_card(goal=other), skills, profile(test_profile), standing_rules(), [])
    data = json.loads(compiled.sonic_prompt.split("\n\n# Task data\n", 1)[1])
    assert data["goal"] == other
    assert GOAL not in compiled.sonic_prompt
