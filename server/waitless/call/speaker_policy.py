"""Pure v5.28 speaker tiering; transfer evidence can only tighten policy."""
from collections.abc import Mapping
from dataclasses import dataclass, replace
import math
from typing import NamedTuple


@dataclass(frozen=True)
class SpeakerVerdict:
    speaker_class: str
    action: str
    handoff_kind: str | None
    gate_speaker: str
    phase: str | None
    reason_code: str
    arm_quiet: bool
    start_stuck: bool


class TransferResult(NamedTuple):
    source_seq: int
    choice: str
    confidence: float


@dataclass(frozen=True)
class SpeakerPolicyState:
    guard: bool = False
    guard_since_seq: int | None = None
    guard_evidence: dict | None = None
    uncertain_run: int = 0
    unavailable_run: int = 0
    last_transfer: tuple[int, str, float] | None = None


def is_queued(state, *, min_confidence):
    transfer = state.last_transfer
    return (state.guard and transfer is not None and transfer[1] == 'pending'
            and transfer[2] >= min_confidence)


def _probability(value):
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


def valid_answers(answers, *, config):
    """Also enforce the contract for clients returning constructed model objects.

    config carries speaker/keypad/transfer references, engine.speaker, seq and
    the source company text (evidence). Missing configuration is an error.
    The keypad options must include only the currently authorized field options.
    """
    if not isinstance(answers, Mapping) or set(answers) != {'keypad', 'speaker', 'transfer'}:
        return False
    return valid_answer_part(answers, names={'keypad', 'speaker', 'transfer'}, config=config)


def valid_answer_part(answers, *, names, config):
    if not isinstance(answers, Mapping) or set(answers) != set(names):
        return False
    for name, answer in answers.items():
        options = config[name]['options']
        probs = getattr(answer, 'probabilities', None)
        if (getattr(answer, 'type', None) != 'choice'
                or not isinstance(getattr(answer, 'choice', None), str)
                or answer.choice not in options
                or not _probability(getattr(answer, 'confidence', None))
                or not isinstance(probs, dict) or not probs
                or not set(probs) <= set(options)
                or not all(_probability(value) for value in probs.values())):
            return False
        if name == 'speaker' and (set(probs) != set(options)
                or abs(math.fsum(probs.values()) - 1) > config['speaker']['probability_sum_tolerance'] + 1e-9):
            return False
    return True


def apply_guard(answers, *, state, config):
    """For an already validated package, retain the first transfer evidence.

    This operation does not advance/reset counters for stale results, except
    the required uncertain reset when entering guard for the first time.
    """
    transfer = answers['transfer']
    qualifies = (transfer.choice in ('pending', 'connected')
        and transfer.confidence >= config['transfer']['min_confidence'])
    if transfer.choice == 'connected':
        qualifies = (transfer.confidence >= config['engine'].connected_handoff_min_confidence
            or qualifies)
    if not state.guard and qualifies:
        return replace(state, guard=True, guard_since_seq=config['seq'], uncertain_run=0,
            guard_evidence={'choice': transfer.choice, 'confidence': transfer.confidence,
                            'text': config['evidence']})
    return state


def classify(answers, *, state, config):
    """Validate the whole package, set guard, then take the first matching tier."""
    engine = config['engine']
    if not valid_answers(answers, config=config):
        new_state = replace(state, unavailable_run=state.unavailable_run + 1)
        capped = new_state.unavailable_run >= engine.unavailable_max_attempts
        return SpeakerVerdict('unavailable', 'handoff' if capped else 'wait',
            'needs_user' if capped else None, 'unknown', 'hold' if state.guard else None,
            'speaker_unavailable_repeated' if capped else 'jev_speaker_unavailable',
            state.guard and not capped and not is_queued(state,
                min_confidence=config['transfer']['min_confidence']),
            not state.guard and not capped), new_state

    new_state = apply_guard(answers, state=replace(state, unavailable_run=0), config=config)
    who, transfer = answers['speaker'], answers['transfer']
    if who.choice == 'live_human' and who.confidence >= config['speaker']['min_confidence']:
        return SpeakerVerdict('live_human', 'handoff', 'live_human', 'live_human',
            'live_rep', 'jev_speaker', False, False), new_state
    if (new_state.guard and transfer.choice == 'connected'
            and transfer.confidence >= engine.connected_handoff_min_confidence):
        return SpeakerVerdict('transfer_connected', 'handoff', 'transfer_connected', 'unknown',
            'live_rep', 'jev_speaker_transfer_connected', False, False), new_state
    if new_state.guard:
        hold = who.choice == 'hold'
        return SpeakerVerdict('guard_wait', 'hold' if hold else 'wait', None,
            'hold' if hold else 'unknown', 'hold', 'jev_speaker_guard_wait',
            not is_queued(new_state, min_confidence=config['transfer']['min_confidence']), False), new_state
    if (who.choice != 'live_human'
            and who.probabilities['live_human'] < config['speaker']['automated_max_human_p']):
        hold = who.choice == 'hold'
        phase = {'menu_or_recording': 'ivr', 'virtual_agent': 'ai_agent', 'hold': 'hold'}[who.choice]
        return SpeakerVerdict('automated', 'hold' if hold else 'allow', None,
            'hold' if hold else 'virtual_agent', phase, 'jev_speaker', False, False), replace(new_state, uncertain_run=0)
    new_state = replace(new_state, uncertain_run=new_state.uncertain_run + 1)
    capped = new_state.uncertain_run >= engine.uncertain_max_turns
    return SpeakerVerdict('uncertain', 'handoff' if capped else 'wait',
        'needs_user' if capped else None, 'unknown', None,
        'speaker_uncertain_repeated' if capped else 'jev_speaker_uncertain',
        not capped, not capped), new_state
