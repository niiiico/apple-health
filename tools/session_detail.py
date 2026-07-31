"""Render per-day training session detail files into ``data/sessions/``.

Companion to ``race_detail.py`` for ordinary (delta-era) sessions. Race archives
mine per-sample HR from the raw ``export.xml``; workouts synced incrementally
have no raw export, so this tool uses what the HealthSync delta path provides:

- ``workouts`` table (always): activity, duration, distance, avg/max HR, kcal.
- ``route-<uuid>.gpx`` in the sync inbox (outdoor workouts): km splits + D+.
- ``hr-<uuid>.csv`` in the inbox (app ≥ 2026-07-11): HR zone distribution and
  drift, same zone model as ``race_detail``. Older deltas lack the series;
  the app's "Backfill HR series" button writes the missing CSVs, after which
  a still-absent file means only avg/max HR is available.

One markdown file per day (``data/sessions/YYYY-MM-DD.md``), overwritten on
re-run; the DB and inbox stay the sources of truth.

Usage::

    uv run python tools/session_detail.py [--since 2026-06-29] [--until 2026-07-10]

Defaults to the current week (Monday → today).
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

from race_detail import ZONES, summarize, thirds

REPO = Path(__file__).resolve().parent.parent
DEFAULT_INBOX = Path("/Volumes/nicolas-data/HealthData/healthsync-inbox")
_GPX_NS = "{http://www.topografix.com/GPX/1/1}"
_EARTH_R_KM = 6371.0088


def _hav_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlam = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_R_KM * math.asin(math.sqrt(a))


def _mmss(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def km_splits(gpx: Path) -> list[tuple[str, float, float]]:
    """Per-km splits from a route GPX: (label, seconds, elevation gain m).

    The final partial kilometre is included with its real fraction as label
    (pace normalised); segments shorter than 200 m are dropped.
    """
    pts: list[tuple[datetime, float, float, float | None]] = []
    for _ev, el in ET.iterparse(str(gpx), events=("end",)):
        if el.tag != f"{_GPX_NS}trkpt":
            continue
        t_el, e_el = el.find(f"{_GPX_NS}time"), el.find(f"{_GPX_NS}ele")
        if t_el is None or not t_el.text:
            continue
        pts.append((
            datetime.fromisoformat(t_el.text.replace("Z", "+00:00")),
            float(el.get("lat")), float(el.get("lon")),
            float(e_el.text) if e_el is not None and e_el.text else None,
        ))
        el.clear()

    splits: list[tuple[str, float, float]] = []
    dist = gain = 0.0
    seg_start_t, seg_start_d = None, 0.0
    for (t0, la0, lo0, e0), (t1, la1, lo1, e1) in zip(pts, pts[1:]):
        if seg_start_t is None:
            seg_start_t = t0
        dist += _hav_km(la0, lo0, la1, lo1)
        if e0 is not None and e1 is not None and e1 > e0:
            gain += e1 - e0
        if dist - seg_start_d >= 1.0:
            splits.append((f"{len(splits) + 1}", (t1 - seg_start_t).total_seconds(), gain))
            seg_start_t, seg_start_d, gain = t1, dist, 0.0
    if pts and seg_start_t is not None:
        frac = dist - seg_start_d
        if frac >= 0.2:
            secs = (pts[-1][0] - seg_start_t).total_seconds()
            splits.append((f"{len(splits)}–{dist:.1f}", secs / frac, gain))  # normalised pace
    return splits


def hr_sections(csv: Path) -> list[str]:
    """Zone table + drift lines from an ``hr-<uuid>.csv`` series."""
    vals: list[tuple[float, float]] = []
    for line in csv.read_text().splitlines()[1:]:
        ts, bpm = line.split(",")
        vals.append((datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp(), float(bpm)))
    s = summarize(vals)
    if not s:
        return []
    lines = ["### FC — zones", "",
             "| n | avg | min | max | " + " | ".join(z[0].split()[0] for z in ZONES) + " |",
             "|---|---|---|---|" + "---|" * len(ZONES)]
    zc = " | ".join(f"{s['zones'][z[0]]:.0f}%" for z in ZONES)
    lines.append(f"| {s['n']} | {s['avg']:.0f} | {s['min']:.0f} | {s['max']:.0f} | {zc} |")
    th = thirds(vals)
    if len(th) == 3:
        lines += ["", "Dérive (tiers) : " +
                  "  ".join(f"{lab} avg {a:.0f}/max {mx:.0f}" for lab, a, mx in th)]
    return lines + [""]


def workout_section(w: sqlite3.Row, inbox: Path) -> list[str]:
    parts = []
    if w["distance_km"]:
        parts.append(f"{w['distance_km']:.2f} km")
    if w["duration_min"]:
        parts.append(f"{w['duration_min']:.0f} min")
    if w["activity"] == "Running" and w["distance_km"] and w["duration_min"]:
        parts.append(_mmss(w["duration_min"] * 60 / w["distance_km"]) + "/km")
    if w["avg_hr"]:
        parts.append(f"FC {w['avg_hr']:.0f}/{w['max_hr']:.0f}")
    if w["energy_kcal"]:
        parts.append(f"{w['energy_kcal']:.0f} kcal")
    lines = [f"## {w['activity']} {w['start'][11:16]} — " + ", ".join(parts), ""]

    hr = inbox / f"hr-{w['uuid']}.csv" if w["uuid"] else None
    if hr and hr.exists():
        lines += hr_sections(hr)
    else:
        lines += ["_Séries FC absentes de l'inbox (backfill « HR series » à lancer dans l'app ?) — avg/max seulement._", ""]

    gpx = inbox / f"route-{w['uuid']}.gpx" if w["uuid"] else None
    if gpx and gpx.exists():
        sp = km_splits(gpx)
        if sp:
            lines += ["### Splits (GPX)", "", "| km | temps | D+ |", "|---|---|---|"]
            lines += [f"| {lab} | {_mmss(secs)} | {g:.0f} m |" for lab, secs, g in sp]
            lines.append("")
    return lines


def render_range(conn: sqlite3.Connection, inbox: Path, outdir: Path,
                 since: date, until: date) -> list[Path]:
    """Write one file per day having workouts in [since, until]; returns paths."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM workouts WHERE start >= ? AND start < ? ORDER BY start",
        (since.isoformat(), (until + timedelta(days=1)).isoformat()),
    ).fetchall()
    outdir.mkdir(parents=True, exist_ok=True)
    written = []
    days: dict[str, list[sqlite3.Row]] = {}
    for w in rows:
        days.setdefault(w["start"][:10], []).append(w)
    for day, ws in sorted(days.items()):
        lines = [f"# Séances {day}", ""]
        for w in ws:
            lines += workout_section(w, inbox)
        zone_hdr = " · ".join(z[0] for z in ZONES)
        lines += [f"> Zones (bpm) : {zone_hdr}. Sources : health.db + inbox HealthSync.", ""]
        path = outdir / f"{day}.md"
        path.write_text("\n".join(lines))
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    monday = date.today() - timedelta(days=date.today().weekday())
    ap = argparse.ArgumentParser(description="Render per-day session detail markdown.")
    ap.add_argument("--db", type=Path, default=REPO / "data/health.db")
    ap.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    ap.add_argument("--outdir", type=Path, default=REPO / "data/sessions")
    ap.add_argument("--since", type=date.fromisoformat, default=monday)
    ap.add_argument("--until", type=date.fromisoformat, default=date.today())
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    for p in render_range(conn, args.inbox, args.outdir, args.since, args.until):
        print(f"wrote {p}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
