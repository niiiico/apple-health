"""Incrementally merge delta files into the SQLite dataset.

This is the consumer side of the incremental-sync path (see
``docs/adr-002-incremental-sync.md``). The on-device HealthSync app drops delta
JSON files (plus route GPX) into a synced folder; ``ah-ingest`` reads the files
it has not yet applied and merges each, idempotently, into ``health.db``.

Wire format: ``docs/delta-contract.md``. Per-file merge happens in one
transaction that also records the filename in ``applied_deltas`` — so a delta is
applied exactly once, which is the *only* thing that makes the additive
``daily_metrics`` merge safe to re-run.

Usage::

    uv run ah-ingest --inbox /path/to/iCloud/HealthSync --db data/health.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import __version__, db, parse_gpx

SCHEMA_VERSION = 1


@dataclass
class IngestSummary:
    """Tallies across one ``ah-ingest`` run."""

    files: int = 0
    skipped: int = 0
    workouts: int = 0
    deleted_workouts: int = 0
    records: int = 0
    daily: int = 0
    routes: int = 0
    cadence_days: int = 0

    def add_file(self, counts: dict[str, int]) -> None:
        self.files += 1
        self.workouts += counts["workouts"]
        self.deleted_workouts += counts["deleted_workouts"]
        self.records += counts["records"]
        self.daily += counts["daily"]
        self.routes += counts["routes"]


def applied_filenames(conn: sqlite3.Connection) -> set[str]:
    """Names of delta files already merged into this DB."""
    return {row[0] for row in conn.execute("SELECT filename FROM applied_deltas")}


def _is_full_build(conn: sqlite3.Connection) -> bool:
    """True if this DB was produced by a full ``ah-build`` and not yet ingested.

    Unioning a full-export DB with a bootstrap delta would double-count dense
    ``daily_metrics`` (aggregates cannot be content-deduped), so ``ah-ingest``
    refuses unless forced. Once a delta has been applied the DB is 'incremental'
    and the guard lifts.
    """
    row = dict(conn.execute("SELECT key, value FROM meta").fetchall() or [])
    has_deltas = conn.execute("SELECT 1 FROM applied_deltas LIMIT 1").fetchone() is not None
    return row.get("mode") == "full" and not has_deltas


# --- per-section merges -------------------------------------------------------

def _merge_workouts(conn: sqlite3.Connection, section: dict) -> tuple[int, int]:
    """Insert new workouts (dedup by uuid or natural key), apply deletes."""
    inserted = 0
    for w in section.get("added", []):
        # Skip if already present by uuid, or by natural key (covers full-export
        # rows whose uuid is NULL, so an overlapping bootstrap does not duplicate).
        cur = conn.execute(
            "INSERT INTO workouts "
            "(uuid, activity, start, end, duration_min, distance_km, energy_kcal, "
            " avg_hr, max_hr, source, indoor) "
            "SELECT ?,?,?,?,?,?,?,?,?,?,? "
            "WHERE NOT EXISTS (SELECT 1 FROM workouts WHERE uuid = ?) "
            "  AND NOT EXISTS (SELECT 1 FROM workouts "
            "                  WHERE start = ? AND activity = ? "
            "                    AND COALESCE(source,'') = COALESCE(?,''))",
            (
                w["uuid"], w["activity"], w["start"], w.get("end"),
                w.get("duration_min"), w.get("distance_km"), w.get("energy_kcal"),
                w.get("avg_hr"), w.get("max_hr"), w.get("source"), w.get("indoor"),
                w["uuid"],
                w["start"], w["activity"], w.get("source"),
            ),
        )
        inserted += cur.rowcount

    deleted = 0
    for uuid in section.get("deleted", []):
        cur = conn.execute("DELETE FROM workouts WHERE uuid = ?", (uuid,))
        deleted += cur.rowcount
    return inserted, deleted


def _merge_records(conn: sqlite3.Connection, section: dict) -> int:
    """Insert sparse records, skipping exact (type, start, source) duplicates.

    ``records.deleted`` is intentionally ignored: the table stores no UUID and a
    delete carries no (type, start) — see the delta contract.
    """
    inserted = 0
    for r in section.get("added", []):
        cur = conn.execute(
            "INSERT INTO records (type, start, value, unit, source) "
            "SELECT ?,?,?,?,? "
            "WHERE NOT EXISTS (SELECT 1 FROM records "
            "                  WHERE type = ? AND start = ? "
            "                    AND COALESCE(source,'') = COALESCE(?,''))",
            (
                r["type"], r["start"], r.get("value"), r.get("unit"), r.get("source"),
                r["type"], r["start"], r.get("source"),
            ),
        )
        inserted += cur.rowcount
    return inserted


def _merge_daily(conn: sqlite3.Connection, section: dict) -> int:
    """Additively merge partial per-(day,type) buckets into ``daily_metrics``.

    ``count``/``sum`` add, ``min``/``max`` fold, ``avg`` is recomputed as
    ``sum/count``. NOT content-idempotent — safety comes from the per-file
    ``applied_deltas`` guard, never from re-running this.
    """
    rows = section.get("added", [])
    for d in rows:
        count, ssum = d["count"], d["sum"]
        dmin, dmax = d.get("min"), d.get("max")
        avg = ssum / count if count else None
        conn.execute(
            """
            INSERT INTO daily_metrics (day, type, unit, count, sum, min, max, avg)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(day, type) DO UPDATE SET
                count = daily_metrics.count + excluded.count,
                sum   = COALESCE(daily_metrics.sum, 0) + COALESCE(excluded.sum, 0),
                min   = min(COALESCE(daily_metrics.min, excluded.min),
                            COALESCE(excluded.min, daily_metrics.min)),
                max   = max(COALESCE(daily_metrics.max, excluded.max),
                            COALESCE(excluded.max, daily_metrics.max)),
                unit  = excluded.unit,
                avg   = (COALESCE(daily_metrics.sum, 0) + COALESCE(excluded.sum, 0))
                        / NULLIF(daily_metrics.count + excluded.count, 0)
            """,
            (d["day"], d["type"], d.get("unit"), count, ssum, dmin, dmax, avg),
        )
    return len(rows)


def _merge_routes(conn: sqlite3.Connection, section: dict, inbox: Path) -> int:
    """Summarise and upsert any route GPX referenced by the delta's workouts."""
    written = 0
    for w in section.get("added", []):
        name = w.get("route_file")
        if not name:
            continue
        path = inbox / name
        if not path.exists():
            print(f"    ! route file missing, skipped: {name}")
            continue
        summary = parse_gpx.summarise_gpx(path)
        parse_gpx.write(conn, [summary])  # INSERT OR REPLACE on filename
        written += 1
    return written


# --- orchestration ------------------------------------------------------------

def apply_delta(conn: sqlite3.Connection, path: Path) -> dict[str, int]:
    """Apply one delta file atomically and record it in ``applied_deltas``.

    Returns per-section counts. Raises ``ValueError`` on an unreadable file or
    an unsupported ``schema`` version (leaving the DB untouched).
    """
    delta = json.loads(path.read_text())
    schema = delta.get("schema")
    if schema != SCHEMA_VERSION:
        raise ValueError(f"{path.name}: unsupported delta schema {schema!r}")

    with conn:  # one transaction: all-or-nothing, commits on success
        w_ins, w_del = _merge_workouts(conn, delta.get("workouts", {}))
        n_rec = _merge_records(conn, delta.get("records", {}))
        n_daily = _merge_daily(conn, delta.get("daily_metrics", {}))
        n_routes = _merge_routes(conn, delta.get("workouts", {}), path.parent)
        conn.execute(
            "INSERT INTO applied_deltas "
            "(filename, anchor_seq, n_workouts, n_records, n_daily, n_routes) "
            "VALUES (?,?,?,?,?,?)",
            (path.name, delta.get("anchor_seq"), w_ins, n_rec, n_daily, n_routes),
        )
    return {
        "workouts": w_ins, "deleted_workouts": w_del,
        "records": n_rec, "daily": n_daily, "routes": n_routes,
    }


def pending_deltas(inbox: Path, done: set[str]) -> list[Path]:
    """Delta files in ``inbox`` not yet applied, in chronological (name) order."""
    return sorted(p for p in inbox.glob("delta-*.json") if p.name not in done)


def ingest_dir(conn: sqlite3.Connection, inbox: Path, *, force: bool = False) -> IngestSummary:
    """Apply every pending delta in ``inbox``, then re-derive cadence."""
    db.init_schema(conn)
    db.ensure_incremental_schema(conn)

    if _is_full_build(conn) and not force:
        raise SystemExit(
            "Refusing to ingest into a full-build DB (mode=full): merging delta "
            "history would double-count daily_metrics. Point --db at a fresh file "
            "to bootstrap from deltas, or pass --force if you know the deltas "
            "carry only data newer than the build."
        )

    done = applied_filenames(conn)
    summary = IngestSummary()
    for path in pending_deltas(inbox, done):
        print(f"Applying {path.name}")
        counts = apply_delta(conn, path)
        summary.add_file(counts)
        print(
            f"  +{counts['workouts']} workouts (-{counts['deleted_workouts']}) "
            f"+{counts['records']} records {counts['daily']} metric-days "
            f"{counts['routes']} routes"
        )

    if summary.files:
        summary.cadence_days = db.derive_cadence(conn)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('mode', 'incremental')")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_ingest_at', datetime('now'))")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('ingester_version', ?)", (__version__,))
        conn.commit()
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Merge Apple Health delta files into the SQLite dataset.")
    ap.add_argument("--inbox", type=Path, required=True, help="folder of delta-*.json + route GPX")
    ap.add_argument("--db", type=Path, default=Path("data/health.db"), help="SQLite path")
    ap.add_argument("--force", action="store_true", help="ingest even into a full-build DB")
    args = ap.parse_args(argv)

    if not args.inbox.is_dir():
        ap.error(f"inbox not found: {args.inbox}")

    conn = db.connect(args.db)
    s = ingest_dir(conn, args.inbox, force=args.force)
    conn.close()

    if not s.files:
        print("No new delta files.")
    else:
        print(
            f"Done → {args.db}: {s.files} files, "
            f"+{s.workouts} workouts (-{s.deleted_workouts}), "
            f"+{s.records} records, {s.daily} metric-days, {s.routes} routes, "
            f"cadence re-derived for {s.cadence_days} days"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
