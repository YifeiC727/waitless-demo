"""Agent speech instructions use special approval and opaque routing events."""
import asyncio
import json

import yaml

from tests.lane_B.test_company_bot_run import launch, setup
from tests.lane_B.test_audio_r3_transfer import prepare_transfer


def test_baseline_agent_persona_uses_special_approval(company_dir):
    path = company_dir / "lakeshore-energy" / "scenarios" / "baseline.yaml"
    agent = yaml.safe_load(path.read_text())["steps"]["agent"]
    assert "deposit" not in agent["persona"].lower()
    assert "special approval" in agent["persona"].lower()
    assert agent["on_done"] == {"deposit_review": "rep"}


async def test_agent_sonic_prompt_has_neutral_transfer_instructions(setup):
    socket, task, service, _, _ = await launch(setup)
    try:
        prompt = service.options["persona"].lower()
        assert "deposit" not in prompt and "deposit_review" not in prompt
        assert "special approval" in prompt and "representative" in prompt
        assert "transfer_to_representative" in prompt
        assert setup[0]._scenario.steps["agent"].on_done == {"deposit_review": "rep"}
    finally:
        await socket.incoming.put(None)
        await asyncio.wait_for(task, 3)


async def test_neutral_event_in_prompt_routes_to_configured_representative(setup):
    socket, task, service, _, _ = await launch(setup)
    try:
        routes = json.loads(service.options["persona"].splitlines()[1])["on_done"]
        assert routes == {"transition_1": "rep"}
        await prepare_transfer(setup[0], service)
        assert (await service.tool("transfer_to_representative", event=next(iter(routes))))["ok"]
        await asyncio.wait_for(task, 3)
        assert setup[0].current_step == "rep" and socket.closed and service.cancelled
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
