"""Render a compact training brief for the Claude Vault (Box knowledge base).

Produces a small markdown file (~400 tokens) with the current training week to
date plus recent significant workouts, meant to be uploaded to the Vault as
``sport-week-current.md`` where the Claude iOS app can read it. Overwritten on
each refresh — it is a volatile snapshot, not an archive (the DB stays the
source of truth).

Usage::

    uv run python tools/vault_sport_week.py --db data/health.db > /tmp/brief.md
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date, timedelta
from pathlib import Path

# A workout is "significant" if it is multisport (race/brick) or meets a
# per-activity distance floor (km).
SIGNIFICANT_FLOOR_KM = {"Running": 10.0, "Swimming": 2.0, "Cycling": 30.0}
ALWAYS_SIGNIFICANT = {"SwimBikeRun"}
SIGNIFICANT_LOOKBACK_WEEKS = 6


def _pace(duration_min: float | None, distance_km: float | None) -> str:
    """min/km as m:ss, or '' when not computable."""
    if not duration_min or not distance_km:
        return ""
    total = duration_min / distance_km
    return f"{int(total)}:{round((total % 1) * 60):02d}/km"


def _fmt_workout(row: sqlite3.Row) -> str:
    day, activity = row["start"][:10], row["activity"]
    parts = []
    if row["distance_km"]:
        parts.append(f"{row['distance_km']:.1f} km")
    if row["duration_min"]:
        parts.append(f"{row['duration_min']:.0f} min")
    if activity == "Running":
        p = _pace(row["duration_min"], row["distance_km"])
        if p:
            parts.append(p)
    if row["avg_hr"]:
        parts.append(f"avg HR {row['avg_hr']:.0f}")
    return f"- {day} **{activity}** — {', '.join(parts)}" if parts else f"- {day} **{activity}**"


def _sparse_avg(conn: sqlite3.Connection, rtype: str, start: date, end: date) -> float | None:
    """Mean of a sparse record type over [start, end)."""
    row = conn.execute(
        "SELECT avg(value) FROM records WHERE type = ? AND start >= ? AND start < ?",
        (rtype, start.isoformat(), end.isoformat()),
    ).fetchone()
    return row[0]


def render(conn: sqlite3.Connection, today: date) -> str:
    """The full markdown brief for the week containing ``today``."""
    conn.row_factory = sqlite3.Row
    monday = today - timedelta(days=today.weekday())
    tomorrow = today + timedelta(days=1)

    week = conn.execute(
        "SELECT * FROM workouts WHERE start >= ? AND start < ? ORDER BY start",
        (monday.isoformat(), tomorrow.isoformat()),
    ).fetchall()

    # Per-sport totals this week, and the prior-4-week weekly average for context.
    def totals(rows) -> dict[str, tuple[int, float]]:
        out: dict[str, tuple[int, float]] = {}
        for r in rows:
            n, km = out.get(r["activity"], (0, 0.0))
            out[r["activity"]] = (n + 1, km + (r["distance_km"] or 0.0))
        return out

    prior = conn.execute(
        "SELECT * FROM workouts WHERE start >= ? AND start < ?",
        ((monday - timedelta(weeks=4)).isoformat(), monday.isoformat()),
    ).fetchall()
    week_t, prior_t = totals(week), totals(prior)

    lines = [
        "---",
        "tags: [sport, training, weekly]",
        "volatility: high",
        f"last_updated: {today.isoformat()}",
        "---",
        f"# Training week {monday.isocalendar()[:2][0]}-W{monday.isocalendar()[1]:02d}"
        f" (from {monday.isoformat()}, through {today.isoformat()})",
        "",
        "## Totals (week to date vs prior 4-wk weekly avg)",
    ]
    for act in sorted(set(week_t) | set(prior_t)):
        n, km = week_t.get(act, (0, 0.0))
        pn, pkm = prior_t.get(act, (0, 0.0))
        lines.append(
            f"- {act}: {n} session(s), {km:.1f} km (avg {pn / 4:.1f}/wk, {pkm / 4:.1f} km/wk)"
        )

    lines += ["", "## Sessions this week"]
    lines += [_fmt_workout(r) for r in week] or ["- none yet"]

    rhr_w = _sparse_avg(conn, "RestingHeartRate", monday, tomorrow)
    rhr_p = _sparse_avg(conn, "RestingHeartRate", monday - timedelta(weeks=4), monday)
    hrv_w = _sparse_avg(conn, "HeartRateVariabilitySDNN", monday, tomorrow)
    hrv_p = _sparse_avg(conn, "HeartRateVariabilitySDNN", monday - timedelta(weeks=4), monday)
    lines += ["", "## Wellness"]
    if rhr_w and rhr_p:
        lines.append(f"- Resting HR: {rhr_w:.0f} bpm this week (4-wk avg {rhr_p:.0f})")
    if hrv_w and hrv_p:
        lines.append(f"- HRV (SDNN): {hrv_w:.0f} ms this week (4-wk avg {hrv_p:.0f})")

    floors = " OR ".join(f"{a} ≥ {k:g} km" for a, k in SIGNIFICANT_FLOOR_KM.items())
    since = today - timedelta(weeks=SIGNIFICANT_LOOKBACK_WEEKS)
    sig = conn.execute(
        "SELECT * FROM workouts WHERE start >= ? AND start < ? ORDER BY start DESC",
        (since.isoformat(), monday.isoformat()),
    ).fetchall()
    sig = [
        r for r in sig
        if r["activity"] in ALWAYS_SIGNIFICANT
        or (r["distance_km"] or 0.0) >= SIGNIFICANT_FLOOR_KM.get(r["activity"], float("inf"))
    ]
    lines += ["", f"## Significant workouts, prior {SIGNIFICANT_LOOKBACK_WEEKS} weeks"
              f" (multisport OR {floors})"]
    lines += [_fmt_workout(r) for r in sig] or ["- none"]
    lines += ["", "_Snapshot generated from health.db; full data stays local._", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render the weekly Vault training brief.")
    ap.add_argument("--db", type=Path, default=Path("data/health.db"), help="SQLite path")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(args.db)
    print(render(conn, date.today()), end="")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
