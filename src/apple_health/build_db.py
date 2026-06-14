"""CLI: build the SQLite dataset from an Apple Health export.

Usage::

    uv run ah-build --export /path/export.xml --routes /path/workout-routes --db data/health.db

Either source may be omitted to parse only the other. The DB is rebuilt from
scratch each run (it is a disposable projection of the immutable export).
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import __version__, db, parse_export, parse_gpx


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the Apple Health SQLite dataset.")
    ap.add_argument("--export", type=Path, help="path to export.xml")
    ap.add_argument("--routes", type=Path, help="path to workout-routes directory")
    ap.add_argument("--db", type=Path, default=Path("data/health.db"), help="output SQLite path")
    args = ap.parse_args(argv)

    if not args.export and not args.routes:
        ap.error("provide at least one of --export or --routes")

    conn = db.connect(args.db)
    db.init_schema(conn)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('builder_version', ?)", (__version__,))

    if args.export:
        print(f"Parsing export: {args.export}")
        t0 = time.time()
        res = parse_export.parse_export(args.export)
        parse_export.write(conn, res)
        print(
            f"  workouts={len(res.workouts):,} "
            f"daily_metrics={len(res.daily):,} "
            f"sparse_records={len(res.records):,} "
            f"(from {res.n_records_seen:,} records) in {time.time() - t0:.0f}s"
        )

    if args.routes:
        print(f"Parsing routes: {args.routes}")
        t0 = time.time()
        summaries = parse_gpx.parse_routes(args.routes)
        parse_gpx.write(conn, summaries)
        total_km = sum(s.distance_km for s in summaries)
        print(f"  routes={len(summaries):,} total={total_km:,.0f} km in {time.time() - t0:.0f}s")

    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('built_at', datetime('now'))")
    conn.commit()
    conn.close()
    print(f"Done → {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
