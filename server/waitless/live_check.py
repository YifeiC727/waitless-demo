"""E183c consumers. Seconds are converted only at the JSONL boundary."""
from dataclasses import dataclass, field
import json
import math
import re

from waitless.call.line_protocol import LINE_FRAME_MS


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def integer(value):
    return type(value) is int and value >= 0


def _ms(value):
    return value * 1000 if number(value) and math.isfinite(value * 1000) else None


def read_company_stats(path, call_id, offset=0):
    try:
        if path is None or not integer(offset):
            return {}
        with path.open('rb') as source:
            source.seek(0, 2)
            if offset > source.tell():
                return {}
            source.seek(offset)
            rows = [json.loads(line) for line in source.read().decode('utf-8').splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            return {}
        selected = [row for row in rows if row.get('call_id') == call_id]
        if len(selected) != 1:
            return {}
        stats = dict(selected[0])
        for source, target in [('rep_first_audio_at', 'rep_first_audio_ms'),
                ('rep_last_audio_at', 'rep_last_audio_ms'),
                ('rep_first_line_end_at', 'rep_first_sentence_end_ms')]:
            stats[target] = _ms(stats.get(source))
        stats['ended_ms'] = _ms(stats.get('ended', {}).get('at'))
        stamps = stats.get('nonsilent_inbound_timestamps_during_rep')
        spans = stats.get('nonsilent_inbound_spans_during_rep')
        stats['rep_nonsilent_inbound_frame_ms'] = [_ms(t) for t in stamps] if isinstance(stamps, list) else None
        stats['rep_nonsilent_inbound_span_ms'] = [
            [_ms(t) for t in span] if isinstance(span, list) and len(span) == 2 else None
            for span in spans] if isinstance(spans, list) else None
        stats['line_facts_ms'] = [{**line, 'started_ms': _ms(line.get('started_at')),
            'ended_ms': _ms(line.get('ended_at')), 'last_sent_ms': _ms(line.get('last_sent_at'))}
            for line in stats.get('lines', [])]
        stats['tone_facts_ms'] = [{**tone, 'start_ms': _ms(tone.get('start_at')),
            'end_ms': _ms(tone.get('end_at'))} for tone in stats['dtmf_tones']]
        return stats
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
        return {}


class _InvalidFacts(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise _InvalidFacts(reason)


def intersects(first, last, start, end):
    """Closed arrival span against a half-open forbidden interval."""
    return start < end and last >= start and first < end


@dataclass
class IdentityEvidence:
    ok: bool = False
    reason: str | None = None
    notice_latency_ms: float | None = None
    join_after_notice_ms: float | None = None
    join_after_line_end_ms: float | None = None
    last_voiced_before_ready_minus_clear_ms: float | None = None
    voiced_beyond_transit: int = 0
    ready_tone_digits: int = 0
    old_responses: set[str] = field(default_factory=set)


def evaluate_identity(events, stats, *, holder_ready_ms, config, holder_windows=(),
                      company_id=None, scenario_id=None, dtmf_key_ms=None):
    # Configuration errors are actionable and must not become a silent FAIL.
    for name in ('clear_max_ms', 'transit_max_ms', 'handoff_latency_max_ms'):
        if not integer(getattr(config, name, None)) or getattr(config, name) <= 0:
            raise ValueError('missing or invalid live-check configuration: ' + name)
    result = IdentityEvidence()
    try:
        require(number(holder_ready_ms), 'holder ready missing')
        require(stats.get('schema_version') == 2 and type(stats['schema_version']) is int, 'schema version')
        require(stats.get('agent_mode') == 'scripted', 'scripted company facts required')
        require(all(isinstance(stats.get(key),str) and bool(stats[key]) for key in ('call_id','call_sid')), 'call identity schema')
        require(re.fullmatch(r'CA[0-9a-fA-F]{32}',stats['call_sid']) is not None, 'call sid schema')
        require(integer(stats.get('nonsilent_inbound_frames_during_handoff')), 'holder audio count schema')
        holder=stats['holder']
        require(set(holder)=={'keypad_entries_complete','keypad_wrong_length','choice','choices_recorded','holder_voice_answers'}, 'holder schema')
        require(holder['choice'] in {'agree','decline',None} and all(integer(holder[key]) for key in holder if key!='choice'), 'holder values')
        require((holder['choices_recorded']==0 and holder['choice'] is None) or
                (holder['choices_recorded']>0 and holder['choice'] in {'agree','decline'}), 'holder choice consistency')
        require(isinstance(stats.get('company_id'), str) and bool(stats['company_id']), 'company identity')
        require(isinstance(stats.get('scenario_id'), str) and bool(stats['scenario_id']), 'scenario identity')
        require(company_id is None or stats['company_id'] == company_id, 'wrong company')
        require(scenario_id is None or stats['scenario_id'] == scenario_id, 'wrong scenario')
        first, last, line_end, ended = [stats.get(key) for key in (
            'rep_first_audio_ms', 'rep_last_audio_ms', 'rep_first_sentence_end_ms', 'ended_ms')]
        require(all(number(t) for t in (first, last, line_end, ended)), 'line timestamps missing')
        require(first <= last <= ended and first <= line_end <= ended and
                stats['rep_first_line_end_at'] <= stats['rep_last_audio_at'] + LINE_FRAME_MS / 1000,
                'line timestamp order')
        require(holder_ready_ms <= ended, 'holder ready outside call')
        r = math.floor(first)
        events = list(events)
        lines = stats['line_facts_ms']
        require(isinstance(lines, list) and bool(lines), 'playback instances missing')
        previous = -1
        for index, line in enumerate(lines):
            start, end = line['started_ms'], line['ended_ms']
            require(line['index'] == index and type(line['index']) is int, 'playback index')
            require(line['role'] in {'ivr', 'agent', 'rep'}, 'scripted playback role')
            require(isinstance(line['step'],str) and bool(line['step']), 'playback step schema')
            require(line['intent'] is None or isinstance(line['intent'],str), 'playback intent schema')
            require(line['outcome'] in {None,'confirmed','needs_follow_up','failed'}, 'playback outcome schema')
            require(number(start) and previous <= start <= ended, 'playback start order')
            require(line['ended_at'] is None or (number(end) and start <= end <= ended), 'playback end')
            previous = start
        rep = next((line for line in lines if line['role'] == 'rep'), None)
        require(rep is not None and rep['started_ms'] == first and rep['ended_ms'] == line_end,
                'first rep instance disagrees')
        ending = stats['ended']
        require(set(ending)=={'by','reason','at','closing_index','closing_outcome','closing_line_completed'}, 'ending schema')
        require(isinstance(ending['reason'],str) and bool(ending['reason']), 'ending reason schema')
        require(ending['closing_index'] is None or integer(ending['closing_index']), 'closing index schema')
        require(ending['by'] in {'company', 'peer', 'api_hangup', 'error'} and
                type(ending['closing_line_completed']) is bool, 'ending schema')
        closing = next((line for line in reversed(lines) if line['outcome'] is not None), None)
        require(ending['closing_index'] == (closing['index'] if closing else None) and
                ending['closing_outcome'] == (closing['outcome'] if closing else None), 'closing reference')
        require(ending['closing_line_completed'] == bool(closing and closing['ended_ms'] is not None
                and closing['ended_ms'] <= ended), 'closing completion disagrees')
        stamps, spans = stats['rep_nonsilent_inbound_frame_ms'], stats['rep_nonsilent_inbound_span_ms']
        require(isinstance(stamps, list) and isinstance(spans, list) and
                len(stamps) == len(spans) == stats['nonsilent_inbound_frames_during_rep'] and
                integer(stats['nonsilent_inbound_frames_during_rep']), 'voice count/spans missing')
        starts = [ev.t_ms for ev in events if ev.event_type == 'call_started' and number(ev.t_ms)]
        call_start = min(starts) if starts else min(line['started_ms'] for line in lines)
        previous_first = previous_last = -1
        for stamp, span in zip(stamps, spans):
            require(isinstance(span, list) and len(span) == 2 and all(number(t) for t in span), 'voice span schema')
            a, b = span
            require(number(stamp) and stamp == a and call_start <= a <= b <= ended and b >= r and
                    previous_first <= a and previous_last <= b, 'voice span order/range')
            previous_first, previous_last = a, b
        tones = stats['tone_facts_ms']
        require(isinstance(tones, list), 'tone facts missing')
        previous_end = -1
        for index, tone in enumerate(tones):
            require({'start_at', 'end_at', 'digits'} <= tone.keys() and integer(tone['digits']), 'tone schema')
            a, b = tone['start_ms'], tone['end_ms']
            require(number(a) and call_start <= a <= ended and a >= previous_end, 'tone start/order')
            if tone['end_at'] is None:
                require(index == len(tones) - 1, 'open tone must be final')
                b = ended
            else:
                require(number(b) and a <= b <= ended, 'tone end/range')
            previous_end = b
            require(not intersects(a, b, r, holder_ready_ms), 'tone before holder ready')
            require(stats['company_id'] != 'bluefin-harbor-bank' or a < r, 'tone during bank rep')
            if a >= holder_ready_ms:
                result.ready_tone_digits += tone['digits']
        for a, b in holder_windows:
            require(number(a) and number(b) and holder_ready_ms <= a <= b, 'holder window')
        events = list(events)
        require(bool(events), 'trace missing')
        for ev in events:
            require(number(ev.t_ms), 'trace timestamp missing')
            require(getattr(ev, 'call_id', stats['call_id']) == stats['call_id'], 'wrong trace call')
            action = ev.action or {}
            require(ev.event_type not in {'handoff_ended', 'resume_injected'} and
                    'handoff.resume' not in json.dumps(action) and
                    not (ev.event_type == 'status_updated' and action.get('phase') == 'resuming'), 'AI resumed')
        require(all(integer(ev.action['seq']) and ev.action['seq'] > 0 for ev in events if ev.event_type == 'company_turn'), 'company turn seq')
        turns = {ev.action['seq']: ev for ev in events if ev.event_type == 'company_turn'}
        require(len(turns) == sum(ev.event_type == 'company_turn' for ev in events), 'duplicate company seq')
        post = [ev for ev in turns.values() if ev.t_ms >= r]
        require(bool(post), 'post-rep company turn missing')
        g_turn = min(post, key=lambda ev: ev.t_ms)
        speaker_decisions = [ev for ev in events if ev.event_type == 'decision' and
                             (getattr(ev, 'policy_decision', None) or {}).get('intent') == 'speaker']
        require(any((ev.action or {}).get('turn_seq') == g_turn.action['seq'] for ev in speaker_decisions),
                'G speaker verdict missing')
        for ev in speaker_decisions:
            turn = turns.get((ev.action or {}).get('turn_seq'))
            require(turn is not None, 'speaker turn missing')
            if turn.t_ms >= r:
                require(ev.policy_decision.get('action') == 'handoff', 'post-rep verdict is not handoff')
        announced = [ev for ev in events if ev.event_type == 'handoff_announced']
        notices = [ev for ev in events if ev.event_type == 'handoff_ready']
        require(len(announced) == len(notices) == 1, 'notice missing/duplicate')
        notice, announce = notices[0].t_ms, announced[0].t_ms
        require(r <= announce <= notice <= holder_ready_ms, 'handoff timing order')
        require(g_turn.t_ms <= notice, 'G later than notice')
        result.notice_latency_ms = notice - line_end
        result.join_after_notice_ms = holder_ready_ms - notice
        result.join_after_line_end_ms = holder_ready_ms - line_end
        require(result.notice_latency_ms <= config.handoff_latency_max_ms, 'notice too late')
        revokes, clears, bounds, summaries, allowed = {}, {}, {}, {}, {}
        epoch, seq, state, last_fact = 0, 0, 'closed', -1
        for ev in events:
            kind, action, t = ev.event_type, ev.action or {}, ev.t_ms
            if kind in {'output_revoked', 'output_cleared', 'output_allowed', 'response_bound', 'response_audio'}:
                require(t >= last_fact, 'gate fact timestamps unordered')
                last_fact = t
            if kind == 'output_revoked':
                cid = action['clear_id']
                require(integer(cid) and cid > 0 and cid not in revokes, 'duplicate/invalid clear id')
                require(action['state'] in {'pending', 'hold', 'closed', 'handoff', 'ended'}, 'gate state')
                require(state not in {'handoff','ended'} or action['state']=='ended', 'gate left final state')
                require(all(integer(action[key]) for key in ('revoked_seq','revoked_epoch','new_epoch')), 'revoke integer fields')
                require(action['revoked_epoch'] == epoch and action['new_epoch'] == epoch + 1 and
                        action['revoked_seq'] == seq, 'revoke permission causality')
                trigger = action['trigger_seq']
                if action['state'] == 'pending':
                    require(integer(trigger) and trigger > seq and trigger in turns, 'pending trigger')
                    seq = trigger
                else:
                    require(trigger is None, 'unexpected revoke trigger')
                epoch, state = action['new_epoch'], action['state']
                revokes[cid] = ev
            elif kind == 'output_allowed':
                require(action['speaker'] in {'menu_or_recording', 'virtual_agent'}, 'allowed speaker')
                require(integer(action['seq']) and integer(action['epoch']), 'allowed integer fields')
                permission = (action['seq'], action['epoch'])
                require(permission == (seq, epoch) and state == 'pending' and permission not in allowed,
                        'allowed permission causality')
                require(t < r, 'gate opened after rep')
                allowed[permission], state = ev, 'allowed'
            elif kind == 'output_cleared':
                cid, covers = action['clear_id'], action['covers']
                require(cid in revokes and cid not in clears and type(action['ok']) is bool,
                        'clear id or success flag')
                require(isinstance(covers, list) and all(integer(c) and c in revokes and c != cid for c in covers)
                        and len(covers) == len(set(covers)), 'clear covers')
                require(all(revokes[c].t_ms <= t for c in [cid, *covers]), 'clear precedes revoke')
                clears[cid] = ev
            elif kind == 'response_bound':
                identity = action['identity']
                require(isinstance(identity, str) and bool(identity) and identity not in bounds, 'response identity')
                require(integer(action['seq']) and integer(action['epoch']), 'binding integer fields')
                require((action['seq'], action['epoch']) == (seq, epoch), 'binding permission causality')
                bounds[identity] = ev
            elif kind == 'response_audio':
                identity = action['identity']
                require(identity in bounds and identity not in summaries, 'unbound/duplicate audio summary')
                frames, a, b = action['frames'], action['first_ms'], action['last_ms']
                require(integer(frames), 'response frame count')
                if frames == 0:
                    require(a is None and b is None, 'zero frame endpoints')
                else:
                    require(number(a) and number(b) and bounds[identity].t_ms <= a <= b <= t, 'response endpoints')
                summaries[identity] = ev
        require(set(bounds) == set(summaries), 'response summary set differs')
        g_revs = [ev for ev in revokes.values() if ev.action['trigger_seq'] == g_turn.action['seq']]
        require(len(g_revs) == 1, 'G revoke missing/duplicate')
        g_rev = g_revs[0]
        require(g_turn.t_ms <= g_rev.t_ms <= notice, 'G revoke order')
        def completed(rev):
            matches = [ev for ev in clears.values() if ev.action['clear_id'] == rev.action['clear_id']
                       or rev.action['clear_id'] in ev.action['covers']]
            require(bool(matches), 'associated cleanup missing')
            ev = min(matches, key=lambda ev: ev.t_ms)
            require(ev.action['ok'] is True and rev.t_ms <= ev.t_ms, 'associated cleanup failed/order')
            return ev.t_ms
        tc = completed(g_rev)
        require(tc - g_turn.t_ms <= config.clear_max_ms, 'G cleanup too slow')
        handoff = [ev for ev in revokes.values() if ev.action['state'] == 'handoff']
        require(len(handoff) == 1, 'handoff revoke missing/duplicate')
        h = handoff[0]
        require(g_rev.t_ms <= h.t_ms <= completed(h) <= announce <= notice <= holder_ready_ms, 'handoff barrier order')
        require(not any(ev.t_ms >= h.t_ms for ev in allowed.values()), 'gate reopened after handoff')
        for identity, bound in bounds.items():
            summary = summaries[identity].action
            if not summary['frames']:
                continue
            permission = (bound.action['seq'], bound.action['epoch'])
            permit = allowed.get(permission)
            require(permit is not None and permit.t_ms <= summary['first_ms'], 'audio without permission')
            if bound.t_ms < r and permit.t_ms < r and summary['first_ms'] < r:
                result.old_responses.add(identity)
            require(summary['last_ms'] <= g_turn.t_ms, 'new audio after G')
            revocation = [ev.t_ms for ev in revokes.values() if (
                ev.action['revoked_seq'], ev.action['revoked_epoch']) == permission]
            require(not revocation or summary['last_ms'] <= min(revocation), 'audio after revocation')
        for identity, summary in summaries.items():
            require(not summary.action['frames'] or identity in result.old_responses or
                    summary.action['last_ms'] < r, 'new response after rep')
        for ev in events:
            if ev.event_type == 'waitless_utterance' and ev.t_ms >= r:
                require((ev.action or {}).get('response_identity') in result.old_responses, 'new/unidentified text')
            if ev.event_type == 'dtmf_sent':
                require(integer((ev.action or {}).get('keys')) and ev.action['keys'] > 0, 'automatic key count missing')
                require(integer(dtmf_key_ms) and dtmf_key_ms > 0, 'automatic key duration missing')
                require(ev.t_ms < r and ev.t_ms + ev.action['keys']*dtmf_key_ms + config.transit_max_ms <= r,
                        'automatic keys overlap rep')
        forbidden = tc + config.transit_max_ms if result.old_responses else r
        for a, b in spans:
            if intersects(a, b, forbidden, holder_ready_ms):
                result.voiced_beyond_transit += 1
            if b >= holder_ready_ms:
                require(any(start <= a and b <= end for start, end in holder_windows), 'voice outside holder window')
        before = [b for a, b in spans if b < holder_ready_ms]
        result.last_voiced_before_ready_minus_clear_ms = max(before) - tc if before else None
        require(result.voiced_beyond_transit == 0, 'voiced arrival in forbidden interval')
        result.ok = True
    except (_InvalidFacts, KeyError, TypeError, AttributeError, ValueError, OverflowError) as exc:
        result.reason = str(exc) if isinstance(exc, _InvalidFacts) else 'malformed facts'
    return result


def result_check(before, response, after, *, call_id, expected_outcome):
    return (before.get('outcome') == 'needs_follow_up' and before.get('awaiting_user_result') is True
            and response.get('call_id') == call_id and response.get('outcome') == expected_outcome
            and response.get('result_source') == 'user' and response.get('awaiting_user_result') is False
            and after.get('outcome') == expected_outcome and after.get('result_source') == 'user'
            and after.get('awaiting_user_result') is False)


class AnswerGate:
    """First reply follows observed speech, or an explicit no-voice timeout."""
    def __init__(self, *, ready_ms, silence_ms, no_voice_ms):
        if not number(ready_ms) or not all(integer(t) and t > 0 for t in (silence_ms, no_voice_ms)):
            raise ValueError('first-answer timing configuration required')
        self.ready_ms, self.silence_ms, self.no_voice_ms = ready_ms, silence_ms, no_voice_ms
        self.last_voice_ms = None

    def observe(self, at_ms, *, voiced):
        if voiced and at_ms >= self.ready_ms:
            self.last_voice_ms = at_ms

    def due(self, at_ms):
        return (at_ms - self.last_voice_ms >= self.silence_ms if self.last_voice_ms is not None
                else at_ms - self.ready_ms >= self.no_voice_ms)
