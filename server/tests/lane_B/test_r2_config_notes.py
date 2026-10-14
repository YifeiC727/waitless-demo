"""B-r2: completed scenario validation has no obsolete TODO marker."""

import inspect

from waitless.company.config import load_scenario


def test_load_scenario_has_no_obsolete_todo():
    assert "todo" not in inspect.getsource(load_scenario).lower()
