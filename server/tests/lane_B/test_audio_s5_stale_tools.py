"""An old Sonic session cannot run tools against the next scenario step."""
import asyncio
import re

from tests.lane_B.test_company_bot_run import launch, setup


async def test_old_agent_tool_cannot_mutate_representative_step(setup):
    engine = setup[0]
    socket, task, service, _, _ = await launch(setup)
    try:
        assert (await service.tool("record_detail", field="address", value="service address"))["ok"]
        assert (await service.tool("record_detail", field="start_date", value="next week"))["ok"]
        engine._bot_on_digits(re.sub(r"\D", "", engine._company.customers[0].phone_number) + "#")
        assert (await service.tool("transfer_to_representative", event="deposit_review"))["ok"]
        assert engine.current_step == "rep"
        # A second tool in the same batch can arrive before the old pipeline
        # has observed the completion event and finished cancelling.
        result = await service.tool("end_call")
        assert not result["ok"]
        assert engine.current_step == "rep" and not engine.ended
    finally:
        await socket.incoming.put(None)
        await asyncio.wait_for(task, 3)
