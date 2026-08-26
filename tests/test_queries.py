"""Tests for the parts of the query surface that need no server.

The database-touching half is exercised against the live store; what is pinned
here is the derivation the renderers never had — zone *durations*, which the
training journal has been recording by hand off the watch.
"""

from __future__ import annotations

from apple_health.derive.zones import zone_durations


def _series(pairs):
    """(offset_seconds, bpm) → the (epoch, bpm) shape the providers return."""
    return [(1_000_000.0 + off, bpm) for off, bpm in pairs]


def test_each_sample_is_credited_the_gap_to_the_next():
    # Two samples 5 s apart in Z2, one in Z3; the last is credited the median.
    d = zone_durations(_series([(0, 140), (5, 145), (10, 165)]))
    assert d["Z2 135-159"] == 10.0
    assert d["Z3 160-169"] == 5.0


def test_an_irregular_series_is_measured_not_assumed():
    # Gaps of 5, 5, 20, 5 — the 20 s one is well inside the ×10 cap, so it is
    # credited in full rather than normalised away. The final sample takes the
    # median (5 s), there being no next sample to measure against.
    d = zone_durations(_series([(0, 140), (5, 140), (10, 140), (30, 140), (35, 140)]))
    assert d["Z2 135-159"] == 5.0 + 5.0 + 20.0 + 5.0 + 5.0


def test_a_long_pause_is_not_counted_as_time_in_zone():
    # A stop at the side of the pool must not read as forty minutes of Z1.
    d = zone_durations(_series([(0, 120), (5, 120), (10, 120), (2410, 120)]))
    assert d["Z1 <135"] == 5.0 + 5.0 + 5.0 + 5.0, "the 40-minute gap should be capped"


def test_an_empty_series_is_none_not_zeroes():
    # Distinct from a session that recorded nothing but was measured.
    assert zone_durations([]) is None


def test_a_single_sample_has_no_measurable_duration():
    d = zone_durations(_series([(0, 150)]))
    assert d is not None and sum(d.values()) == 0.0
