"""Apply pending HealthSync deltas to the Postgres store (ADR-006, step 2).

`ah-ingest` merges deltas into SQLite; this does the same into Postgres, so the
store stops being the snapshot `ah-migrate` left and becomes current. Until it
runs, pointing any reader at Postgres silently drops everything synced since the
migration — which is why the reader default stayed on the inbox.

The merge semantics mirror `sources.healthsync` exactly, because divergence
between the two would be invisible until a rendered file disagreed with itself:

- **Workouts** dedupe on uuid *and* on the natural key, so an overlapping
  bootstrap cannot double a session that arrived by full export.
- **Daily metrics** add for a normal delta and *replace* for a schema-2
  backfill, which is authoritative for whole days it re-queried.
- **Coverage** comes from the delta's ``generated_at``.

Idempotency is per file, never per row: `ingest_runs.ref` is unique, and a
delta already recorded there is skipped. The additive daily merge is not
content-idempotent — re-applying one would double a day — so the ledger is the
only thing standing between a re-run and corruption, exactly as `applied_deltas`
is on the SQLite side.

Usage::

    ah-pgsync [--inbox PATH] [--dsn ...] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

from ..sources import gpx, healthsync
from ..store import Store
from .migrate import DEFAULT_INBOX, _observed_through, _parse_ts, _tz_name


def applied_refs(store: Store) -> set[str]:
    """Delta filenames Postgres has already recorded an ingest run for."""
    with store.cursor() as cur:
        cur.execute("SELECT ref FROM ingest_runs WHERE source = 'healthsync'")
        return {r["ref"] for r in cur.fetchall()}


def _merge_workouts(cur, section: dict) -> tuple[int, int]:
    """Insert new workouts (dedup by uuid or natural key), apply deletes."""
    inserted = 0
    for w in section.get("added", []):
        cur.execute(
            """INSERT INTO workouts (uuid, activity, started_at, ended_at, tz_name,
                   duration_min, distance_km, energy_kcal, avg_hr, max_hr, source, indoor)
               SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                WHERE NOT EXISTS (SELECT 1 FROM workouts WHERE uuid = %s)
                  AND NOT EXISTS (SELECT 1 FROM workouts
                                   WHERE started_at = %s AND activity = %s
                                     AND COALESCE(source,'') = COALESCE(%s,''))""",
            (w["uuid"], w["activity"], _parse_ts(w["start"]),
             _parse_ts(w["end"]) if w.get("end") else None, _tz_name(w["start"]),
             w.get("duration_min"), w.get("distance_km"), w.get("energy_kcal"),
             w.get("avg_hr"), w.get("max_hr"), w.get("source"),
             None if w.get("indoor") is None else bool(w["indoor"]),
             w["uuid"], _parse_ts(w["start"]), w["activity"], w.get("source")),
        )
        if cur.rowcount:
            inserted += 1
            continue
        # Skipped as a duplicate. If it matched on the natural key, the stored
        # row came from the full export and carries no uuid — adopt this one, or
        # the series lookup below finds nothing and the samples are lost for
        # good, since the delta is about to be marked applied.
        cur.execute(
            """UPDATE workouts SET uuid = %s
                WHERE uuid IS NULL AND started_at = %s AND activity = %s
                  AND COALESCE(source,'') = COALESCE(%s,'')""",
            (w["uuid"], _parse_ts(w["start"]), w["activity"], w.get("source")),
        )

    deleted = 0
    for uuid in section.get("deleted", []):
        # ON DELETE CASCADE takes the workout's hr_samples and laps with it.
        cur.execute("DELETE FROM workouts WHERE uuid = %s", (uuid,))
        deleted += cur.rowcount
    return inserted, deleted


def _merge_records(cur, section: dict) -> int:
    """Insert sparse records, skipping ones already stored.

    The dedup is not optional: a schema-2 backfill re-queries a closed range
    ignoring anchors and re-emits records for days already ingested. Without it
    Postgres gains a second copy of every resting-HR and VO2max row in the
    range, and any later average over that period is quietly wrong. Returns the
    number actually written, not the number offered.
    """
    written = 0
    for r in section.get("added", []):
        cur.execute(
            """INSERT INTO records (type, recorded_at, value, unit, source)
               SELECT %s,%s,%s,%s,%s
                WHERE NOT EXISTS (SELECT 1 FROM records
                                   WHERE type = %s AND recorded_at = %s
                                     AND COALESCE(source,'') = COALESCE(%s,''))""",
            (r["type"], _parse_ts(r["start"]), r.get("value"), r.get("unit"), r.get("source"),
             r["type"], _parse_ts(r["start"]), r.get("source")),
        )
        written += cur.rowcount
    return written


def _merge_daily(cur, section: dict, *, replace: bool) -> int:
    """Fold per-(day, type) buckets in.

    `replace` is the schema-2 backfill path: the producer re-queried whole days
    from scratch, so its buckets are authoritative and overwrite. The additive
    path is the normal one and is *not* content-idempotent.
    """
    rows = section.get("added", [])
    conflict = (
        """count = excluded.count, sum = excluded.sum,
           min = excluded.min, max = excluded.max, unit = excluded.unit"""
        if replace else
        """count = daily_metrics.count + COALESCE(excluded.count, 0),
           sum   = daily_metrics.sum + COALESCE(excluded.sum, 0),
           min   = LEAST(daily_metrics.min, COALESCE(excluded.min, daily_metrics.min)),
           max   = GREATEST(daily_metrics.max, COALESCE(excluded.max, daily_metrics.max)),
           unit  = excluded.unit"""
    )
    for d in rows:
        cur.execute(
            f"""INSERT INTO daily_metrics (day, type, unit, count, sum, min, max)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (day, type) DO UPDATE SET {conflict}""",
            (d["day"], d["type"], d.get("unit"), d["count"], d["sum"],
             d.get("min"), d.get("max")),
        )
    return len(rows)


def _merge_routes(cur, section: dict, inbox: Path) -> int:
    """Summarise and upsert any route GPX the delta's workouts reference."""
    written = 0
    for w in section.get("added", []):
        name = w.get("route_file")
        if not name:
            continue
        path = inbox / name
        if not path.exists():
            print(f"    ! route file missing, skipped: {name}")
            continue
        s = gpx.summarise_gpx(path)
        cur.execute(
            """INSERT INTO routes (filename, started_at, ended_at, n_points, distance_km,
                   duration_min, elev_gain_m, avg_speed_kmh, min_lat, min_lon, max_lat, max_lon)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (filename) DO UPDATE SET
                   started_at = excluded.started_at, ended_at = excluded.ended_at,
                   n_points = excluded.n_points, distance_km = excluded.distance_km,
                   duration_min = excluded.duration_min, elev_gain_m = excluded.elev_gain_m,
                   avg_speed_kmh = excluded.avg_speed_kmh,
                   min_lat = excluded.min_lat, min_lon = excluded.min_lon,
                   max_lat = excluded.max_lat, max_lon = excluded.max_lon""",
            (s.filename,
             datetime.fromisoformat(s.start) if s.start else None,
             datetime.fromisoformat(s.end) if s.end else None,
             s.n_points, s.distance_km, s.duration_min, s.elev_gain_m,
             s.avg_speed_kmh, s.min_lat, s.min_lon, s.max_lat, s.max_lon),
        )
        written += 1
    return written


def _merge_hr_series(cur, section: dict, inbox: Path) -> int:
    """Load `hr-<uuid>.csv` sidecars for the delta's workouts into hr_samples."""
    samples = 0
    for w in section.get("added", []):
        uuid = w.get("uuid")
        if not uuid:
            continue
        path = inbox / f"hr-{uuid}.csv"
        if not path.exists():
            # The contract neither guarantees a sidecar arrives with its delta
            # nor retries one that did not. Say so; sweep_orphan_series() is
            # what actually recovers it.
            print(f"    ! HR sidecar missing: hr-{uuid}.csv (sweep will retry)")
            continue
        cur.execute("SELECT id FROM workouts WHERE uuid = %s", (uuid,))
        row = cur.fetchone()
        if not row:
            continue
        batch = []
        with open(path) as fh:
            for line in csv.DictReader(fh):
                try:
                    batch.append((row["id"],
                                  datetime.fromisoformat(line["time"].replace("Z", "+00:00")),
                                  int(round(float(line["bpm"])))))
                except (KeyError, ValueError):
                    continue
        if batch:
            cur.executemany(
                "INSERT INTO hr_samples (workout_id, t, bpm) VALUES (%s,%s,%s)"
                " ON CONFLICT (workout_id, t) DO NOTHING",
                batch,
            )
            samples += cur.rowcount
    return samples


def _merge_swim_lengths(cur, inbox: Path, section: dict) -> int:
    """Load `swim-<uuid>.csv` sidecars into `laps`, one row per length.

    HealthKit records swimming as one sample per length, each with a start and
    an end, which is finer than lap events: the swim time is end − start and
    the rest before the next length is the gap. That is what makes a benchmark
    200 readable from the record — any eight consecutive 25 m lengths — instead
    of being read off the watch by hand.

    They land in `laps` rather than a new table because the shape already fits
    (index, start, duration, distance) and the session page already renders it.
    """
    lengths = 0
    for w in section.get("added", []):
        uuid = w.get("uuid")
        if not uuid:
            continue
        path = inbox / f"swim-{uuid}.csv"
        if not path.exists():
            # Silent by design: most workouts are not swims, and warning on
            # every ride would train the reader to ignore the line that matters.
            continue
        cur.execute("SELECT id FROM workouts WHERE uuid = %s", (uuid,))
        row = cur.fetchone()
        if not row:
            continue
        batch = []
        with open(path) as fh:
            for idx, line in enumerate(csv.DictReader(fh), start=1):
                try:
                    start = datetime.fromisoformat(line["start"].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(line["end"].replace("Z", "+00:00"))
                    metres = float(line["metres"])
                except (KeyError, ValueError):
                    continue
                duration = (end - start).total_seconds()
                if duration <= 0:
                    # A length cannot take no time. Skipping rather than storing
                    # it keeps a divide-by-zero out of every pace computed later.
                    continue
                batch.append((row["id"], idx, start, duration, metres))
        if batch:
            cur.executemany(
                """INSERT INTO laps (workout_id, idx, started_at, duration_s, distance_m)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (workout_id, idx) DO UPDATE SET
                       started_at = excluded.started_at,
                       duration_s = excluded.duration_s,
                       distance_m = excluded.distance_m""",
                batch,
            )
            lengths += len(batch)
    return lengths


def apply_delta(store: Store, path: Path) -> dict[str, int]:
    """Apply one delta to Postgres and record its ingest run."""
    delta = json.loads(path.read_text())
    schema = delta.get("schema")
    if schema not in healthsync.SUPPORTED_SCHEMAS:
        raise ValueError(f"{path.name}: unsupported delta schema {schema!r}")
    backfill = healthsync._validate_backfill(path.name, delta)

    workouts = delta.get("workouts", {})
    with store.cursor() as cur:
        w_ins, w_del = _merge_workouts(cur, workouts)
        n_rec = _merge_records(cur, delta.get("records", {}))
        n_daily = _merge_daily(cur, delta.get("daily_metrics", {}), replace=bool(backfill))
        n_routes = _merge_routes(cur, workouts, path.parent)
        n_hr = _merge_hr_series(cur, workouts, path.parent)
        n_lengths = _merge_swim_lengths(cur, path.parent, workouts)
        observed = _observed_through(path.parent, path.name)
        if observed is None:
            raise ValueError(f"{path.name}: no generated_at and an unparseable name; "
                             "refusing to record an ingest run with no coverage instant")
        cur.execute(
            """INSERT INTO ingest_runs (source, ref, observed_through,
                   workouts_added, records_added, metric_days)
               VALUES ('healthsync', %s, %s, %s, %s, %s)""",
            (path.name, observed, w_ins, n_rec, n_daily),
        )
    return {"workouts": w_ins, "deleted_workouts": w_del, "records": n_rec,
            "daily": n_daily, "routes": n_routes, "hr_samples": n_hr,
            "lengths": n_lengths}


def sweep_orphan_series(store: Store, inbox: Path) -> int:
    """Load any `hr-` or `swim-` sidecar whose workout has none of its data yet.

    Two cases need this, and neither is reachable from a delta's own
    `workouts.added`: a sidecar that arrived after the delta referencing it (the
    contract does not order them and does not retry), and the app's "Backfill HR
    series" pass, which writes sidecars with *no* delta referencing them at all.
    `ah-migrate` caught those with a one-shot uuid sweep; without this they would
    simply never reach Postgres, and the inbox fallback would hide it until the
    inbox is retired.
    """
    loaded = 0
    with store.cursor() as cur:
        for path in sorted(inbox.glob("hr-*.csv")):
            uuid = path.stem[3:]
            cur.execute(
                """SELECT w.id FROM workouts w
                    WHERE w.uuid = %s
                      AND NOT EXISTS (SELECT 1 FROM hr_samples s WHERE s.workout_id = w.id)""",
                (uuid,),
            )
            row = cur.fetchone()
            if not row:
                continue
            batch = []
            with open(path) as fh:
                for line in csv.DictReader(fh):
                    try:
                        batch.append((row["id"],
                                      datetime.fromisoformat(line["time"].replace("Z", "+00:00")),
                                      int(round(float(line["bpm"])))))
                    except (KeyError, ValueError):
                        continue
            if batch:
                cur.executemany(
                    "INSERT INTO hr_samples (workout_id, t, bpm) VALUES (%s,%s,%s)"
                    " ON CONFLICT (workout_id, t) DO NOTHING",
                    batch,
                )
                loaded += cur.rowcount
                print(f"  swept {path.name}: +{cur.rowcount} samples")

        # The same two cases, for swim lengths. The app's "Backfill swim
        # lengths" pass writes these with no delta at all, so without a sweep a
        # recovered season of splits would sit in the inbox and never arrive.
        for path in sorted(inbox.glob("swim-*.csv")):
            uuid = path.stem[5:]
            cur.execute(
                """SELECT w.id FROM workouts w
                    WHERE w.uuid = %s
                      AND NOT EXISTS (SELECT 1 FROM laps l WHERE l.workout_id = w.id)""",
                (uuid,),
            )
            row = cur.fetchone()
            if not row:
                continue
            batch = []
            with open(path) as fh:
                for idx, line in enumerate(csv.DictReader(fh), start=1):
                    try:
                        start = datetime.fromisoformat(line["start"].replace("Z", "+00:00"))
                        end = datetime.fromisoformat(line["end"].replace("Z", "+00:00"))
                        metres = float(line["metres"])
                    except (KeyError, ValueError):
                        continue
                    duration = (end - start).total_seconds()
                    if duration <= 0:
                        continue
                    batch.append((row["id"], idx, start, duration, metres))
            if batch:
                cur.executemany(
                    """INSERT INTO laps (workout_id, idx, started_at, duration_s, distance_m)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (workout_id, idx) DO UPDATE SET
                           started_at = excluded.started_at,
                           duration_s = excluded.duration_s,
                           distance_m = excluded.distance_m""",
                    batch,
                )
                loaded += len(batch)
                print(f"  swept {path.name}: +{len(batch)} lengths")
    return loaded


def main(argv: list[str] | None = None) -> int:
    """Apply every delta Postgres has not yet seen."""
    ap = argparse.ArgumentParser(description="Apply pending deltas to the Postgres store.")
    ap.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    ap.add_argument("--dsn", default=None, help="Defaults to APPLE_HEALTH_DSN.")
    ap.add_argument("--dry-run", action="store_true", help="roll back instead of committing")
    args = ap.parse_args(argv)

    # A missing inbox must not read as "nothing to do": Path.glob on a
    # nonexistent directory yields nothing, so an unmounted volume would report
    # "up to date" and exit 0 — the quiet-healthy-cycle failure again.
    if not args.inbox.is_dir():
        ap.error(f"inbox not found: {args.inbox}")

    store = Store(args.dsn)
    try:
        pending = healthsync.pending_deltas(args.inbox, applied_refs(store))
        for path in pending:
            counts = apply_delta(store, path)
            # Commit per delta, as healthsync's `with conn:` does. One
            # permanently-bad delta must block only itself, not every delta
            # behind it.
            if not args.dry_run:
                store.commit()
            print(f"Applied {path.name}")
            print("  +{workouts} workouts (-{deleted_workouts}) +{records} records "
                  "{daily} metric-days {routes} routes +{hr_samples} hr samples"
                  .format(**counts)
                  + (f" +{counts['lengths']} lengths" if counts.get("lengths") else ""))

        swept = sweep_orphan_series(store, args.inbox)
        if not args.dry_run and swept:
            store.commit()

        if not pending and not swept:
            print("postgres up to date")
            return 0

        print(f"coverage now {store.coverage().observed_through}")
        if args.dry_run:
            store.rollback()
            print("dry run — rolled back")
        else:
            store.commit()
            print("committed")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
