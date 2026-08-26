"""CLI: print a headline-stats markdown report from the SQLite dataset.

Usage::

    uv run ah-report --db data/health.db
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def _scalar(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _section(title: str) -> str:
    return f"\n## {title}\n"


def _table(headers: list[str], rows: list[tuple]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out)


def build_report(conn: sqlite3.Connection) -> str:
    conn.row_factory = sqlite3.Row
    out: list[str] = ["# Apple Health — headline stats"]

    built_at = _scalar(conn, "SELECT value FROM meta WHERE key='built_at'")
    span = conn.execute("SELECT min(start), max(start) FROM workouts").fetchone()
    out.append(f"\nBuilt: {built_at} · Workout span: {(span[0] or '?')[:10]} → {(span[1] or '?')[:10]}")

    # Workouts by activity
    out.append(_section("Workouts by activity"))
    rows = conn.execute("""
        SELECT activity,
               count(*) AS n,
               round(sum(distance_km), 0) AS km,
               round(sum(duration_min) / 60.0, 0) AS hours,
               round(sum(energy_kcal), 0) AS kcal
        FROM workouts GROUP BY activity ORDER BY n DESC
    """).fetchall()
    out.append(_table(["Activity", "Count", "Dist km", "Hours", "kcal"],
                      [tuple(r) for r in rows]))

    # Running volume per year
    out.append(_section("Running volume per year"))
    rows = conn.execute("""
        SELECT substr(start, 1, 4) AS yr,
               count(*) AS runs,
               round(sum(distance_km), 0) AS km,
               round(sum(duration_min) / 60.0, 1) AS hours,
               round(avg(avg_hr), 0) AS avg_hr
        FROM workouts WHERE activity = 'Running'
        GROUP BY yr ORDER BY yr
    """).fetchall()
    out.append(_table(["Year", "Runs", "km", "Hours", "Avg HR"],
                      [tuple(r) for r in rows]))

    # Longest / fastest runs (>=5 km, with pace)
    out.append(_section("Notable runs"))
    rows = conn.execute("""
        SELECT substr(start,1,10) AS date,
               round(distance_km,1) AS km,
               round(duration_min,0) AS min,
               printf('%d:%02d', CAST(duration_min/distance_km AS INT),
                      CAST((duration_min/distance_km - CAST(duration_min/distance_km AS INT))*60 AS INT)) AS pace
        FROM workouts
        WHERE activity='Running' AND distance_km >= 5 AND duration_min > 0
        ORDER BY distance_km DESC LIMIT 5
    """).fetchall()
    out.append("**Longest:**\n")
    out.append(_table(["Date", "km", "min", "min/km"], [tuple(r) for r in rows]))
    rows = conn.execute("""
        SELECT substr(start,1,10) AS date,
               round(distance_km,1) AS km,
               printf('%d:%02d', CAST(duration_min/distance_km AS INT),
                      CAST((duration_min/distance_km - CAST(duration_min/distance_km AS INT))*60 AS INT)) AS pace
        FROM workouts
        WHERE activity='Running' AND distance_km >= 5 AND duration_min > 0
        ORDER BY duration_min/distance_km ASC LIMIT 5
    """).fetchall()
    out.append("\n**Fastest pace (≥5 km):**\n")
    out.append(_table(["Date", "km", "min/km"], [tuple(r) for r in rows]))

    # Physiology trends from sparse records
    out.append(_section("Physiology trends (yearly)"))
    for label, rtype in [("Resting HR (bpm)", "RestingHeartRate"),
                         ("VO2max (ml/kg/min)", "VO2Max"),
                         ("Body mass (kg)", "BodyMass"),
                         ("HRV SDNN (ms)", "HeartRateVariabilitySDNN")]:
        rows = conn.execute("""
            SELECT substr(start,1,4) AS yr, round(avg(value),1) AS v, count(*) AS n
            FROM records WHERE type=? GROUP BY yr ORDER BY yr
        """, (rtype,)).fetchall()
        if rows:
            cells = ", ".join(f"{r['yr']}: {r['v']}" for r in rows)
            out.append(f"- **{label}** — {cells}")

    # Running form (daily_metrics, latest 3 years)
    out.append(_section("Running form (yearly avg)"))
    for label, mtype in [("Cadence (spm, derived)", "RunningCadence"),
                        ("Vertical osc (cm)", "RunningVerticalOscillation"),
                        ("Ground contact (ms)", "RunningGroundContactTime"),
                        ("Stride length (m)", "RunningStrideLength")]:
        rows = conn.execute("""
            SELECT substr(day,1,4) AS yr, round(avg(avg),2) AS v
            FROM daily_metrics WHERE type=? GROUP BY yr ORDER BY yr
        """, (mtype,)).fetchall()
        if rows:
            cells = ", ".join(f"{r['yr']}: {r['v']}" for r in rows)
            out.append(f"- **{label}** — {cells}")

    # Routes
    out.append(_section("GPS routes"))
    r = conn.execute("""
        SELECT count(*) n, round(sum(distance_km),0) km, round(sum(elev_gain_m),0) dplus,
               min(min_lat) a, min(min_lon) b, max(max_lat) c, max(max_lon) d
        FROM routes
    """).fetchone()
    if r and r["n"]:
        out.append(f"- {r['n']:,} routes · {r['km']:,.0f} km · {r['dplus']:,.0f} m D+ cumulative")
        out.append(f"- Bounding box: lat [{r['a']:.3f}, {r['c']:.3f}], lon [{r['b']:.3f}, {r['d']:.3f}]")

    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Print headline-stats report.")
    ap.add_argument("--db", type=Path, default=Path("data/health.db"))
    ap.add_argument("--out", type=Path, help="write markdown to this file instead of stdout")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    report = build_report(conn)
    conn.close()

    if args.out:
        args.out.write_text(report, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
