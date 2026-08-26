"""Tests for the pure parts of the SQLite→Postgres migration.

The database-touching half is exercised by running `ah-migrate` against a
throwaway Postgres; what is worth pinning here is the recovery of facts the
SQLite schema did not hold — the zone a timestamp was recorded in, and the
instant each delta had observed HealthKit through.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from apple_health.commands.migrate import _observed_through, _parse_ts, _tz_name
from apple_health.derive.cadence import cadence_spm


def test_parses_apple_health_timestamps_with_their_offset():
    ts = _parse_ts("2026-07-30 19:08:35 +0200")
    assert ts.utcoffset().total_seconds() == 2 * 3600
    assert ts.astimezone(UTC).hour == 17


def test_recovers_a_zone_name_from_the_stored_offset():
    # Not an IANA identifier — the offset is all SQLite kept — but enough to
    # render the wall-clock day, which is what tz_name exists for.
    assert _tz_name("2026-07-30 19:08:35 +0200") == "UTC+02:00"
    assert _tz_name("2026-08-12 19:12:20 +0900") == "UTC+09:00"


def test_prefers_the_delta_s_generated_at_over_its_filename(tmp_path):
    """The two differ by seconds and the field is the honest one.

    The name is stamped when the file is written; `generated_at` is when the
    anchored query actually ran, which is the instant coverage means.
    """
    name = "delta-20260812T142133Z-0016.json"
    (tmp_path / name).write_text(json.dumps({"generated_at": "2026-08-12T14:21:10Z"}))
    assert _observed_through(tmp_path, name) == datetime(2026, 8, 12, 14, 21, 10, tzinfo=UTC)


def test_falls_back_to_the_filename_when_the_delta_is_gone(tmp_path):
    # Deltas are pruned from the inbox; the name still carries a usable instant.
    assert _observed_through(tmp_path, "delta-20260812T142133Z-0016.json") == datetime(
        2026, 8, 12, 14, 21, 33, tzinfo=UTC
    )


def test_unparseable_names_yield_no_coverage_claim(tmp_path):
    # Better to record no ingest_run than to invent a coverage instant.
    assert _observed_through(tmp_path, "not-a-delta.json") is None


def test_cadence_matches_the_formula_the_stored_rows_used():
    # 10 km/h at 1.03 m stride ≈ 161.8 spm, in the validated 162–165 band.
    assert cadence_spm(10.0, 1.03) == 161.8
    assert cadence_spm(10.0, 0) is None
