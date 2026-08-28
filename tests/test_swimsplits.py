"""Tests for per-length swim splits.

The arithmetic here decides whether a benchmark reads as achieved, so the cases
that matter are the ones where a plausible answer would be wrong: a set of
lengths reported as a continuous swim, a window that overshoots the distance, a
length with no duration.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apple_health.commands.swimsplits import (
    Length, best_window, group_by_session,
)

JST = timezone(timedelta(hours=9))
T0 = datetime(2026, 6, 23, 12, 0, tzinfo=JST)


def _lengths(spec: list[tuple[float, float]], metres: float = 25.0) -> list[Length]:
    """Build lengths from (swim_seconds, rest_after_seconds) pairs."""
    out, clock = [], T0
    for swim, rest in spec:
        out.append(Length(clock, clock + timedelta(seconds=swim), metres))
        clock = clock + timedelta(seconds=swim + rest)
    return out


def test_a_continuous_200_is_eight_lengths_wall_to_wall():
    lengths = _lengths([(30, 0)] * 8)
    best = best_window(lengths)
    assert best is not None
    assert best.elapsed == 240 and best.rest == 0 and best.continuous


def test_rest_inside_the_window_is_counted_and_reported():
    """A set is not a benchmark.

    Eight lengths with twenty seconds of rest between each is a 8×25 set, not a
    200. Reporting only the swim time would call it a 4:00 — the flattering
    error this whole project exists to avoid.
    """
    lengths = _lengths([(30, 20)] * 8)
    best = best_window(lengths)
    assert best.elapsed == 380              # 8×30 swum + 7×20 rest
    assert best.rest == 140
    assert not best.continuous


def test_the_fastest_window_wins_not_the_first():
    lengths = _lengths([(40, 0)] * 8 + [(25, 0)] * 8)
    best = best_window(lengths)
    assert best.elapsed == 200 and best.index == 8


def test_a_window_that_cannot_land_exactly_is_not_reported():
    """Open water has no walls.

    Variable-length segments rarely sum to exactly 200 m, and reporting the
    nearest overshoot as "your 200" would invent a time that was never swum.
    """
    lengths = _lengths([(30, 0)] * 8, metres=33.0)
    assert best_window(lengths) is None


def test_too_few_lengths_reports_nothing_rather_than_a_short_200():
    assert best_window(_lengths([(30, 0)] * 4)) is None


def test_sessions_split_on_a_long_gap():
    """Two swims in a day are two sessions, not one with a very long rest."""
    morning = _lengths([(30, 20)] * 4)
    evening_start = T0 + timedelta(hours=6)
    evening = [Length(evening_start, evening_start + timedelta(seconds=30), 25.0)]
    sessions = group_by_session(morning + evening)
    assert [len(s) for s in sessions] == [4, 1]


def test_a_pause_mid_session_does_not_split_it():
    """A two-minute rest is a set boundary, not a different swim."""
    lengths = _lengths([(30, 5), (30, 120), (30, 5), (30, 0)])
    assert len(group_by_session(lengths)) == 1
