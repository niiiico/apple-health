"""One-shot migration of the SQLite dataset into the Postgres store (ADR-006).

Reads, never writes, the SQLite database: it is the source of record until this
has been verified, and ADR-001's cold archive is the recovery path behind it.

Two things are recovered here that the SQLite schema could not hold:

- **Heart-rate series.** They live as `hr-<uuid>.csv` sidecars in the inbox, so
  `session_detail` needed an ``--inbox`` path to render zones at all. Folding
  them into `hr_samples` is what lets the store answer for a session on its own.
- **Coverage.** `applied_deltas` recorded which files were applied but not the
  instant each one had observed HealthKit through — the omission behind a
  training conclusion that stood wrong for a month. That instant is each delta's
  ``generated_at``, read back from the inbox here.

The full-export build gets no `ingest_runs` row: nothing in the SQLite database
records when that export was taken, and inventing a value would be exactly the
kind of plausible-but-wrong coverage this is meant to end. It does not affect
`coverage()`, which reports the maximum — always a delta.

Usage::

    ah-migrate --sqlite data/health.db --inbox <dir> [--dsn ...] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ..config import repo_root
from ..store import Store

DEFAULT_INBOX = Path("/Volumes/nicolas-data/HealthData/healthsync-inbox")


def _parse_ts(value: str) -> datetime:
    """Parse an Apple Health timestamp: 'YYYY-MM-DD HH:MM:SS +0900'."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")


def _tz_name(value: str) -> str:
    """Recover a fixed-offset zone name from a stored timestamp.

    The SQLite rows carry an offset, not an IANA identifier — the identifier
    only arrives with HealthKit metadata on the delta path. An offset is enough
    to render the wall-clock day, which is what `tz_name` is for.
    """
    return f"UTC{value[-5:-2]}:{value[-2:]}"


def _observed_through(inbox: Path, filename: str) -> datetime | None:
    """The instant a delta had observed HealthKit through.

    Prefers the delta's own ``generated_at`` over the timestamp in its filename:
    the two differ by seconds (the name is stamped when the file is written,
    the field when the query ran), and the field is the honest one.
    """
    path = inbox / filename
    if path.exists():
        try:
            generated = json.loads(path.read_text()).get("generated_at")
            if generated:
                return datetime.fromisoformat(generated.replace("Z", "+00:00"))
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    # Fall back to the name: delta-20260812T142133Z-0016.json
    stamp = filename.split("-")[1] if "-" in filename else ""
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def migrate(sqlite_path: Path, inbox: Path, store: Store) -> dict[str, int]:
    """Copy the SQLite dataset and the inbox HR series into Postgres.

    Args:
        sqlite_path: Existing SQLite database. Opened read-only.
        inbox: Directory holding `hr-<uuid>.csv` sidecars and delta JSON.
        store: Connected Postgres store, already migrated.

    Returns:
        Row counts per table, for the caller to report and check.
    """
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    counts: dict[str, int] = {}
    uuid_to_id: dict[str, int] = {}

    with store.cursor() as cur:
        rows = src.execute(
            "SELECT uuid, activity, start, end, duration_min, distance_km,"
            " energy_kcal, avg_hr, max_hr, source, indoor FROM workouts ORDER BY start"
        ).fetchall()
        for r in rows:
            cur.execute(
                """INSERT INTO workouts (uuid, activity, started_at, ended_at, tz_name,
                       duration_min, distance_km, energy_kcal, avg_hr, max_hr, source, indoor)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (r["uuid"], r["activity"], _parse_ts(r["start"]),
                 _parse_ts(r["end"]) if r["end"] else None, _tz_name(r["start"]),
                 r["duration_min"], r["distance_km"], r["energy_kcal"],
                 r["avg_hr"], r["max_hr"], r["source"],
                 None if r["indoor"] is None else bool(r["indoor"])),
            )
            new_id = cur.fetchone()["id"]
            if r["uuid"]:
                uuid_to_id[r["uuid"]] = new_id
        counts["workouts"] = len(rows)

        # RunningCadence is a derivation (speed ÷ stride), which is why those
        # rows have an avg and no sum. ADR-006 (d) says derivations are computed
        # on read, and the NOT NULL on `sum` is the schema enforcing it, so they
        # are skipped rather than coerced. The formula lives in derive/cadence.py.
        rows = src.execute(
            "SELECT day, type, unit, count, sum, min, max FROM daily_metrics"
            " WHERE type <> 'RunningCadence'"
        ).fetchall()
        counts["cadence_days_skipped"] = src.execute(
            "SELECT count(*) FROM daily_metrics WHERE type = 'RunningCadence'"
        ).fetchone()[0]
        cur.executemany(
            """INSERT INTO daily_metrics (day, type, unit, count, sum, min, max)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            [(r["day"], r["type"], r["unit"], r["count"], r["sum"], r["min"], r["max"])
             for r in rows],
        )
        counts["daily_metrics"] = len(rows)

        rows = src.execute("SELECT type, start, value, unit, source FROM records").fetchall()
        cur.executemany(
            "INSERT INTO records (type, recorded_at, value, unit, source) VALUES (%s,%s,%s,%s,%s)",
            [(r["type"], _parse_ts(r["start"]), r["value"], r["unit"], r["source"])
             for r in rows],
        )
        counts["records"] = len(rows)

        rows = src.execute(
            "SELECT filename, start, end, n_points, distance_km, duration_min,"
            " elev_gain_m, avg_speed_kmh, min_lat, min_lon, max_lat, max_lon FROM routes"
        ).fetchall()
        cur.executemany(
            """INSERT INTO routes (filename, started_at, ended_at, n_points, distance_km,
                   duration_min, elev_gain_m, avg_speed_kmh, min_lat, min_lon, max_lat, max_lon)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [(r["filename"],
              datetime.fromisoformat(r["start"]) if r["start"] else None,
              datetime.fromisoformat(r["end"]) if r["end"] else None,
              r["n_points"], r["distance_km"], r["duration_min"], r["elev_gain_m"],
              r["avg_speed_kmh"], r["min_lat"], r["min_lon"], r["max_lat"], r["max_lon"])
             for r in rows],
        )
        counts["routes"] = len(rows)

        # HR series: the sidecars that made --inbox a parameter of every renderer.
        samples = 0
        series_found = 0
        for uuid, workout_id in uuid_to_id.items():
            csv_path = inbox / f"hr-{uuid}.csv"
            if not csv_path.exists():
                continue
            series_found += 1
            batch = []
            with open(csv_path) as fh:
                for row in csv.DictReader(fh):
                    try:
                        batch.append((
                            workout_id,
                            datetime.fromisoformat(row["time"].replace("Z", "+00:00")),
                            int(round(float(row["bpm"]))),
                        ))
                    except (KeyError, ValueError):
                        continue
            if batch:
                # ON CONFLICT: the primary key makes a re-run idempotent rather
                # than doubling every sample.
                cur.executemany(
                    "INSERT INTO hr_samples (workout_id, t, bpm) VALUES (%s,%s,%s)"
                    " ON CONFLICT (workout_id, t) DO NOTHING",
                    batch,
                )
                samples += len(batch)
        counts["hr_samples"] = samples
        counts["hr_series"] = series_found

        # Coverage, recovered from the deltas themselves.
        applied = src.execute(
            "SELECT filename, applied_at, n_workouts, n_records, n_daily"
            " FROM applied_deltas ORDER BY anchor_seq"
        ).fetchall()
        runs = 0
        for r in applied:
            observed = _observed_through(inbox, r["filename"])
            if observed is None:
                continue
            cur.execute(
                """INSERT INTO ingest_runs (source, ref, observed_through, applied_at,
                       workouts_added, records_added, metric_days)
                   VALUES ('healthsync', %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (ref) DO NOTHING""",
                (r["filename"], observed, r["applied_at"],
                 r["n_workouts"], r["n_records"], r["n_daily"]),
            )
            runs += 1
        counts["ingest_runs"] = runs

    src.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    """Migrate the SQLite dataset into Postgres."""
    ap = argparse.ArgumentParser(description="Copy the SQLite dataset into the Postgres store.")
    ap.add_argument("--sqlite", type=Path, default=repo_root() / "data/health.db")
    ap.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    ap.add_argument("--dsn", default=None, help="Defaults to APPLE_HEALTH_DSN.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Roll back instead of committing; still reports counts.")
    args = ap.parse_args(argv)

    if not args.sqlite.exists():
        print(f"no such SQLite database: {args.sqlite}")
        return 1

    store = Store(args.dsn)

    # One-shot by design. Re-running would fail on the workouts.uuid constraint
    # partway through, which is safe but reads as a crash; say so instead.
    with store.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM workouts")
        existing = cur.fetchone()["n"]
    if existing:
        print(f"target already holds {existing:,} workouts — this migration is "
              f"one-shot. Drop and recreate the database to re-run.")
        return 1

    counts = migrate(args.sqlite, args.inbox, store)
    for name, n in counts.items():
        print(f"  {name:<14} {n:>8,}")

    cov = store.coverage()
    print(f"  coverage       {cov.observed_through}")
    if args.dry_run:
        print("dry run — rolled back")
    else:
        store.commit()
        print("committed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
