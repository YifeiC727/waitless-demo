"""Selected option details travel as data, preserving the agent's reported phase."""
import pytest
from lane_D.agent_support import *


@pytest.mark.parametrize('phase,detail', [('live_rep', 'One-time deposit'), ('ai_agent', '')])
async def test_answer_formats_option_detail_and_restores_phase(agent_call, phase, detail):
    h = agent_call
    await h.boot()
    await h.tool('update_status', {'phase': phase, 'status_line': 'Speaking to service'})
    await h.tool('ask_user', {'question': 'How should we pay?', 'options': [
        {'label': 'Keep the deposit', 'detail': detail}, {'label': 'Autopay'}]})
    choice = h.call.status().pending_choice
    answer = await h.call.answer(choice.choice_id, choice.options[0].id)
    assert answer.status == 'choice_recorded'
    payload = h.event_payloads()[-1]
    assert payload['label'] == 'Keep the deposit' and payload['detail'] == detail
    assert h.call.status().phase == phase
    h.assert_trace_contract()
