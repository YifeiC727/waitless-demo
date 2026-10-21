"""v5.28 T5: synchronous PCM accounting and conservative source marks."""
import math
import struct

import pytest


def pcm(ms, rate=16000, channels=1, value=0):
    return struct.pack('<h', value) * (rate * ms // 1000 * channels)


@pytest.fixture
def meter(fake_clock):
    from waitless.call.quiet import QuietMeter
    return QuietMeter(frame_ms=20, threshold_dbfs=-45, quiet_max_gap_ms=200,
                      uncertain_quiet_ms=5000, clock=fake_clock)


def feed(meter, clock, ms=20, *, step=20, rate=16000, channels=1, value=0):
    clock.advance_sync(step)
    meter.observe(pcm(ms, rate, channels, value), rate, channels, clock.monotonic_ms())


@pytest.mark.parametrize('rate,channels', [(8000, 1), (16000, 1), (8000, 2), (16000, 2)])
def test_cross_frame_multiple_windows_and_incomplete_residue(meter, rate, channels):
    meter.observe(pcm(19, rate, channels), rate, channels, 0)
    assert meter.audio_total_ms == 0 and meter.residue
    meter.observe(pcm(42, rate, channels), rate, channels, 20)
    assert meter.audio_total_ms == 60
    assert len(meter.residue) == rate // 1000 * channels * 2


def test_threshold_equality_is_voiced_and_tail_is_retained(fake_clock):
    from waitless.call.quiet import QuietMeter
    meter = QuietMeter(frame_ms=20, threshold_dbfs=20 * math.log10(1024 / 32768),
                      quiet_max_gap_ms=200, uncertain_quiet_ms=5000, clock=fake_clock)
    meter.observe(pcm(40, value=1023) + pcm(20, value=-1024) + pcm(7), 16000, 1, 90)
    assert meter.voiced_serial == 1
    assert meter.audio_total_ms == meter.reset_audio_ms == 60
    assert meter.reset_wall_ms == meter.last_arrival_ms == 90
    assert meter.residue == pcm(7)


@pytest.mark.parametrize('rate,channels', [(8000, 1), (16000, 2)])
def test_format_change_discards_residue_and_resets(meter, rate, channels):
    meter.observe(pcm(39), 16000, 1, 0)
    meter.observe(pcm(1, rate, channels), rate, channels, 20)
    assert meter.audio_total_ms == meter.reset_audio_ms == 20
    assert meter.reset_wall_ms == 20
    assert meter.residue == pcm(1, rate, channels)


@pytest.mark.parametrize('gap,discard', [(200, False), (201, True)])
def test_gap_boundary(meter, gap, discard):
    meter.observe(pcm(19), 16000, 1, 0)
    meter.observe(pcm(1), 16000, 1, gap)
    assert meter.audio_total_ms == (0 if discard else 20)
    assert bool(meter.residue) == discard
    if discard:
        assert meter.reset_wall_ms == gap and meter.reset_audio_ms == 0


def test_4980_then_5000_and_freshness(meter, fake_clock):
    mark = meter.on_company_final(1)
    for _ in range(249):
        feed(meter, fake_clock)
    assert not meter.qualifies(mark, fake_clock.monotonic_ms())
    feed(meter, fake_clock)
    assert meter.qualifies(mark, fake_clock.monotonic_ms())
    assert meter.qualifies(mark, fake_clock.monotonic_ms() + 200)
    assert not meter.qualifies(mark, fake_clock.monotonic_ms() + 201)


def test_backlog_cannot_replace_wall_time(meter, fake_clock):
    mark = meter.on_company_final(1)
    feed(meter, fake_clock, 5000)
    assert meter.audio_total_ms == 5000
    assert not meter.qualifies(mark, fake_clock.monotonic_ms())
    fake_clock.advance_sync(4980)
    assert not meter.qualifies(mark, fake_clock.monotonic_ms())


def test_disconnect_resume_restarts_quiet(meter, fake_clock):
    mark = meter.on_company_final(1)
    for _ in range(250):
        feed(meter, fake_clock)
    feed(meter, fake_clock, step=300)
    assert not meter.qualifies(mark, fake_clock.monotonic_ms())
    for _ in range(249):
        feed(meter, fake_clock)
    assert not meter.qualifies(mark, fake_clock.monotonic_ms())
    feed(meter, fake_clock)
    assert meter.qualifies(mark, fake_clock.monotonic_ms())


def test_source_marks_exclude_prior_quiet_but_include_verdict_delay(meter, fake_clock):
    for _ in range(250):
        feed(meter, fake_clock)
    first = meter.on_company_final(1)
    assert not meter.qualifies(first, fake_clock.monotonic_ms())
    for _ in range(250):
        feed(meter, fake_clock)
    assert meter.qualifies(first, fake_clock.monotonic_ms())
    second = meter.on_company_final(2)
    assert meter.marks[1] is first and meter.marks[2] is second
    assert not meter.qualifies(second, fake_clock.monotonic_ms())


def test_source_residue_19_then_1_conservatively_loses_whole_window(meter, fake_clock):
    meter.observe(pcm(19), 16000, 1, 0)
    mark = meter.on_company_final(7)
    assert mark.mark_audio_ms == 20 and mark.mark_wall_ms == 0
    feed(meter, fake_clock, 1)
    for _ in range(249):
        feed(meter, fake_clock)
    assert fake_clock.monotonic_ms() == 5000
    assert meter.audio_total_ms - mark.mark_audio_ms == 4980
    assert not meter.qualifies(mark, fake_clock.monotonic_ms())
    feed(meter, fake_clock)
    assert meter.qualifies(mark, fake_clock.monotonic_ms())


def test_sparse_audio_after_final_has_insufficient_audio_credit(meter, fake_clock):
    meter.observe(pcm(5000), 16000, 1, 0)
    mark = meter.on_company_final(1)
    for _ in range(25):
        feed(meter, fake_clock, step=200)
    assert fake_clock.monotonic_ms() == 5000
    assert not meter.qualifies(mark, fake_clock.monotonic_ms())


def test_late_voiced_window_and_voiced_residue_invalidate_recheck(meter, fake_clock):
    mark = meter.on_company_final(1)
    for _ in range(250):
        feed(meter, fake_clock)
    assert meter.qualifies(mark, fake_clock.monotonic_ms())
    meter.observe(pcm(20) + pcm(1, value=12000), 16000, 1, fake_clock.monotonic_ms())
    assert not meter.qualifies(mark, fake_clock.monotonic_ms())
    meter.observe(pcm(19, value=12000), 16000, 1, fake_clock.monotonic_ms())
    assert meter.voiced_serial == 1 and meter.reset_audio_ms == meter.audio_total_ms
    meter.observe(pcm(20) + pcm(20, value=12000), 16000, 1, fake_clock.monotonic_ms())
    assert meter.voiced_serial == 2 and meter.reset_audio_ms == meter.audio_total_ms


def test_each_window_notifies_synchronously(meter):
    seen = []
    meter.window_observers.append(lambda: seen.append((meter.audio_total_ms, meter.voiced_serial)))
    meter.observe(pcm(20) + pcm(20, value=12000) + pcm(20), 16000, 1, 60)
    assert seen == [(20, 0), (40, 1), (60, 1)]


def test_queued_qualification_is_invalid_after_format_change(meter, fake_clock):
    mark = meter.on_company_final(1)
    for _ in range(250):
        feed(meter, fake_clock)
    assert meter.qualifies(mark, fake_clock.monotonic_ms())
    feed(meter, fake_clock, rate=8000)
    assert not meter.qualifies(mark, fake_clock.monotonic_ms())
