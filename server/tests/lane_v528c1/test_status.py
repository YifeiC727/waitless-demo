"""Three handoff notifications from real LiveCall, including durable Trace/Store."""
from datetime import datetime, timedelta

import pytest

from waitless.models import CallStatusOut, TraceEvent
from waitless.store import Store
from waitless.trace import Trace
from .conftest import trigger


KINDS = [
    ('live_human', 'notification_text', '有人接起了，请马上加入'),
    ('transfer_connected', 'connected_text', '转接后线路上在跟你说话，请马上加入'),
    ('needs_user', 'needs_user_text', '线路上的情况 Waitless 判断不了，请你接手'),
]


@pytest.mark.parametrize('kind,field,message', KINDS)
@pytest.mark.parametrize('configured', [False, True])
async def test_status_selects_kind_notification_and_preserves_deadline(call, kind, field, message, configured):
    # Always choosing live_human, or hardcoding a message, breaks this API response.
    if configured:
        message = 'Configured <notice> & "join"'
        setattr(call.deps.settings.engine.handoff, field, message)
    policy = call.deps.settings.engine.handoff
    expected_expiry = datetime.fromisoformat(call.deps.clock.now_iso()) + timedelta(seconds=policy.join_timeout_s)
    try:
        await call.system_handoff(trigger(), kind=kind)
        status = call.status()
        assert status.phase == 'handoff_ready'
        assert status.handoff.state == 'ready'
        assert status.handoff.reason_kind == kind and status.handoff.reason == message
        assert status.handoff.generation == 1
        assert status.handoff.remaining_s == policy.join_timeout_s
        assert status.handoff.expires_at == expected_expiry
        assert status.handoff.media.model_dump() == {'ai_media': 'disconnected', 'recording': False, 'transcription': False}
        payload = status.model_dump_json()
        restored = CallStatusOut.model_validate_json(payload)
        assert restored == status
        await call.deps.clock.advance(1001)
        later = call.status().handoff
        assert later.remaining_s == policy.join_timeout_s - 1
        assert later.expires_at == expected_expiry and later.generation == 1
        assert later.reason_kind == kind and later.reason == message
        # Active media and ended state must retain provenance, without a ready deadline.
        call._toolbox.action_phase = 'in_handoff'
        active = call.status().handoff
        assert active.state == 'active' and active.expires_at is None and active.remaining_s is None
        assert active.reason_kind == kind and active.reason == message
    finally:
        await call._finish(call.call_id)
    ended = call.status().handoff
    assert ended.state == 'ended' and ended.reason_kind == kind and ended.reason == message
    assert ended.expires_at is None and ended.remaining_s is None


async def test_tool_initiated_handoff_has_no_system_reason(call):
    # The extra system notification must not appear on an agent-tool handoff.
    call._toolbox.action_phase = 'handoff_ready'
    try:
        status = call.status()
        assert status.handoff.state == 'ready'
        assert status.handoff.reason is None and status.handoff.reason_kind is None
    finally:
        await call._finish(call.call_id)


async def test_fallback_notification_and_trace_survive_real_store_roundtrip(call, tmp_path):
    # A FakeTrace cannot establish durable fallback provenance or teardown snapshots.
    from call_engine._support import jev_exchange
    path = tmp_path / 'handoff.sqlite3'
    store = Store(path)
    call.deps.trace = Trace(store, call.deps.clock)
    exchange = jev_exchange('agent_tool')
    source = dict(jev_request=exchange.request.model_dump(mode='json'),
                  jev_response=exchange.response.model_dump(mode='json'))
    original_id = 'stored-source'
    call.deps.trace.event(TraceEvent(task_id=call.task_id, call_id=call.call_id,
        event_type='decision', decision_id=original_id, decision_point='agent_tool',
        action={'turn_seq': 1}, policy_decision={'intent': 'speaker', 'action': 'wait',
            'reason_code': 'speaker_uncertain'}, latency_ms=0, **source))
    try:
        await call.system_handoff(dict(seq=1, source_decision_id=original_id,
            reason_code='speaker_quiet_after_uncertain', quiet_audio_ms=5000,
            quiet_wall_ms=5000, **source), kind='needs_user', precheck=lambda: True)
        status = CallStatusOut.model_validate_json(call.status().model_dump_json())
        assert status.handoff.reason_kind == 'needs_user'
        assert status.handoff.reason == '线路上的情况 Waitless 判断不了，请你接手'
    finally:
        await call._finish(call.call_id)
        store.close()
    reopened = Store(path)
    try:
        trace = Trace(reopened, call.deps.clock)
        events = trace.events(call_id=call.call_id)
        decisions = [e for e in events if e.event_type == 'decision']
        assert len(decisions) == 2
        fallback = decisions[1]
        assert fallback.decision_id != original_id
        assert fallback.policy_decision == {'intent': 'speaker_fallback', 'action': 'handoff',
            'handoff_kind': 'needs_user', 'reason_code': 'speaker_quiet_after_uncertain',
            'policy_rule': 'phone-agent/speaker-fallback', 'source_decision_id': original_id,
            'quiet_audio_ms': 5000, 'quiet_wall_ms': 5000}
        assert fallback.jev_request == source['jev_request'] and fallback.jev_response == source['jev_response']
        notifications = [e for e in events if e.event_type in ('handoff_announced', 'handoff_ready')]
        assert [e.event_type for e in notifications] == ['handoff_announced', 'handoff_ready']
        assert all(e.decision_id == fallback.decision_id and e.seq > fallback.seq for e in notifications)
        ended, = [e for e in events if e.event_type == 'call_ended']
        summary, = [e for e in events if e.event_type == 'action' and e.action.get('kind') == 'teardown_summary']
        assert ended.action['teardown_errors'] == ended.action['teardown_unreclaimed'] == []
        assert summary.action == {'kind': 'teardown_summary', 'teardown_errors': [], 'teardown_unreclaimed': []}
        assert summary.seq > ended.seq > notifications[-1].seq
        assert trace.check_call(call.call_id) == []
    finally:
        reopened.close()


@pytest.mark.parametrize('kind,field,_message', KINDS)
async def test_real_status_json_renders_escaped_reason_and_deadline_in_web(call, kind, field, _message):
    # Exercise the shipped renderer with actual status JSON, keeping markup out of text.
    import json
    from pathlib import Path
    import subprocess
    setattr(call.deps.settings.engine.handoff, field, 'Configured <notice> & "join"')
    renderer = Path(__file__).resolve().parents[3] / 'web/js/cards.mjs'
    script = '''
import { readFileSync } from 'node:fs';
const { renderHandoffReady } = await import(process.argv[1]);
const status = JSON.parse(readFileSync(0, 'utf8'));
process.stdout.write(renderHandoffReady(status.handoff, { mode: 'live-sim' }));
'''
    try:
        await call.system_handoff(trigger(), kind=kind)
        payload = json.loads(call.status().model_dump_json())
        rendered = subprocess.run(['node', '--input-type=module', '-e', script, renderer.as_uri()],
            input=json.dumps(payload), capture_output=True, text=True, check=True).stdout
        assert 'Configured &lt;notice&gt; &amp; &quot;join&quot;' in rendered
        assert 'Configured <notice>' not in rendered
        assert f'Join within {call.deps.settings.engine.handoff.join_timeout_s} seconds' in rendered
        assert payload['handoff']['expires_at'] in rendered
    finally:
        await call._finish(call.call_id)


async def test_legacy_system_handoff_without_kind_keeps_live_human_notice(call):
    # Existing accepted-handoff callers predate kind; their handshake/status must still work.
    call._toolbox.action_phase = 'handoff_ready'
    call._system_handoff = True
    assert call._handoff_kind is None
    try:
        status = call.status()
        assert status.handoff.reason_kind == 'live_human'
        assert status.handoff.reason == '有人接起了，请马上加入'
    finally:
        await call._finish(call.call_id)
