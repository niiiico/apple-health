"""Tests for the parts of the Postgres store that need no server.

The coverage comparison is the one worth pinning: it is the mechanism that
stops a truncated view reading as a complete one, and its first implementation
was off by a full day in both directions by mixing a UTC instant with a
wall-clock date.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

from apple_health.derive.zones import zone_of
from apple_health.store import ZoneModel, assess_coverage

JST = timezone(timedelta(hours=9))
PDT = timezone(timedelta(hours=-7))


def test_no_ingest_reports_an_empty_record():
    cov = assess_coverage(None, date(2026, 8, 26))
    assert cov.observed_through is None
    assert "empty" in cov.warning


def test_no_requested_window_carries_the_boundary_without_a_warning():
    observed = datetime(2026, 8, 26, 5, 0, tzinfo=UTC)
    cov = assess_coverage(observed)
    assert cov.observed_through == observed
    assert cov.warning is None


def test_a_day_is_covered_only_once_the_ingest_reaches_its_end():
    # 23:59:59.999999 JST on the 26th covers the 26th; a second earlier does not.
    assert assess_coverage(
        datetime(2026, 8, 26, 23, 59, 59, 999999, tzinfo=JST), date(2026, 8, 26), JST
    ).warning is None
    assert assess_coverage(
        datetime(2026, 8, 26, 23, 59, 58, tzinfo=JST), date(2026, 8, 26), JST
    ).warning is not None


def test_morning_sync_does_not_read_as_a_day_stale():
    """The first direction of the old bug.

    08:00 JST on the 26th is 2026-08-25T23:00Z, so comparing `observed.date()`
    to the request reported a just-synced record as covering only the 25th.
    """
    observed = datetime(2026, 8, 26, 8, 0, tzinfo=JST)
    assert observed.astimezone(UTC).date() == date(2026, 8, 25)  # the trap
    cov = assess_coverage(observed, date(2026, 8, 25), JST)
    assert cov.warning is None, "the 25th is fully observed and must not warn"


def test_evening_sync_does_not_claim_a_day_it_only_half_saw():
    """The other direction, and the dangerous one.

    18:00 PDT on the 25th is 2026-08-26T01:00Z. Comparing UTC dates made
    `08-25 <= 08-26` true and reported full coverage for a day observed only
    through the evening — a silent 'absent' where the truth is 'unknown'.
    """
    observed = datetime(2026, 8, 25, 18, 0, tzinfo=PDT)
    assert observed.astimezone(UTC).date() == date(2026, 8, 26)  # the trap
    cov = assess_coverage(observed, date(2026, 8, 25), PDT)
    assert cov.warning is not None
    assert "UNKNOWN, not absent" in cov.warning


def test_zone_model_classifies_across_every_band():
    zm = ZoneModel(date(2026, 1, 15), "watch-auto", 134, 159, 169, 177)
    assert [zm.zone_of(b) for b in (100, 134, 135, 165, 174, 178, 200)] == [1, 1, 2, 3, 4, 5, 5]


def test_an_implausible_rate_is_not_classified_as_recovery():
    """Z5 is open-ended; a corrupt sample must not land in Z1 via the fallback."""
    assert zone_of(1200).startswith("Z5")
