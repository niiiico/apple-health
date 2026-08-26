"""Archive per-segment heart-rate detail for significant workouts.

The SQLite projection (``data/health.db``) keeps only daily HR aggregates, but
the raw ``export.xml`` has every sample. For races we want to keep a durable,
re-analysable record: this tool scans HR on each race day, buckets samples into
named legs/segments (windows from the GPX route times, local JST), computes
zone distribution + drift, and writes one markdown file per race into
``data/races/`` — so the granular analysis survives even after the raw export
is rotated away.

This is the canonical "keep a record of a significant workout" tool. To archive
a new race, add an entry to ``RACES`` (date, title, splits prose, and segment
windows) and re-run. Segment windows come from the ``routes`` table
(``substr(start,12)`` is UTC; add 9 h for JST) or the workout start/end.

Usage::

    AH_EXPORT=/path/to/export.xml uv run ah-races

``AH_EXPORT`` defaults to the dated cold-archive export on the NAS. Point it at
whichever export covers the race dates you are archiving.
"""

from __future__ import annotations

import argparse
import os
import re
from datetime import time

from ..config import repo_root
from ..derive.zones import ZONES, summarize, thirds, zone_of  # noqa: F401

# Canonical immutable source (ADR-001). Override with AH_EXPORT for other exports.
EXPORT = os.environ.get(
    "AH_EXPORT",
    "/Volumes/nicolas-data/HealthData/apple_health_export_2026-06-29/export.xml",
)
OUTDIR = str(repo_root() / "data" / "races")


def _t(h, m, s=0):
    return time(h, m, s)


# Registry of archived races. Segment windows are JST (UTC route time + 9 h).
RACES = [
    {
        "slug": "2026-06-28-triathlon-shichigahama",
        "date": "2026-06-28",
        "title": "Triathlon olympique — みやぎ国際 仙台ベイ七ヶ浜 (2026-06-28)",
        "splits": (
            "## Splits officiels (dossard 425, スタンダード)\n\n"
            "Distances off. : nat 1,5 km (750×2) · vélo 39 km (6,5×6) · course 10 km. "
            "Total **2:59:43**.\n\n"
            "| Leg | Distance | Temps | Allure/vitesse | Rang leg |\n"
            "|-----|----------|-------|----------------|----------|\n"
            "| Natation | 1,5 km | 32:17 | 2:09/100m | 124/146 |\n"
            "| T1 | — | 5:01 | — | — |\n"
            "| Vélo | 39 km | 1:21:33 | 28,7 km/h | 114/146 |\n"
            "| T2 | — | 2:20 | — | — |\n"
            "| Course | 10 km | 58:31 | 5:51/km | 90/146 |\n\n"
            "Classement : 109e/146 scratch · 97e H · 23e M40–49.\n"
        ),
        "segments": [
            ("Natation", _t(9, 1, 23),  _t(9, 33, 33)),
            ("T1",       _t(9, 33, 33), _t(9, 38, 48)),
            ("Vélo",     _t(9, 38, 48), _t(11, 1, 26)),
            ("T2",       _t(11, 1, 26), _t(11, 3, 11)),
            ("Course",   _t(11, 3, 11), _t(12, 2, 30)),
        ],
    },
    {
        "slug": "2025-09-27-triathlon-olympique",
        "date": "2025-09-27",
        "title": "Triathlon olympique Kujukuri 2025-09-27 (officiel 3:51:19)",
        "splits": (
            "## Splits officiels (九十九里トライアスロン 2025, dossard 9046)\n\n"
            "**= Kujukuri 2025**, le temps à battre le 3 oct. 2026. Total off. **3:51:19** "
            "(montre 3:51:48). Classement : **602e/~650 scratch, 101e M45-49**.\n\n"
            "| Leg | Distance | Temps off. | Allure/vitesse | Rang leg |\n"
            "|-----|----------|-----------|----------------|----------|\n"
            "| Natation | 1,5 km | 52:08 | 3:28/100m | 646 |\n"
            "| T1 | (long, plage→parc) | 10:34 | — | 174 |\n"
            "| Vélo | 40 km | 1:36:42 | 24,8 km/h | 587 |\n"
            "| T2 | — | 3:03 | — | 185 |\n"
            "| Course | 10 km | 1:08:52 | 6:53/km | 540 |\n\n"
            "FC moy (workout) 164, max 188. Natation près du fond (646e) ; T1 longue = "
            "caractéristique du parcours (plage→parc).\n"
        ),
        "segments": [
            ("Natation", _t(9, 39, 58),  _t(10, 32, 4)),
            ("T1",       _t(10, 32, 4),  _t(10, 43, 10)),
            ("Vélo",     _t(10, 43, 8),  _t(12, 21, 2)),
            ("T2",       _t(12, 21, 2),  _t(12, 22, 14)),
            ("Course",   _t(12, 22, 16), _t(13, 31, 46)),
        ],
    },
    {
        "slug": "2026-05-31-semi-yamanakako",
        "date": "2026-05-31",
        "title": "Semi-marathon Yamanakako 2026-05-31 (~1:59)",
        "splits": (
            "## Résultat\n\n"
            "21,36 km en ~1:59 (5:33/km), D+ ~124 m, parcours dur (~1000 m alt., mur km 20+).\n"
            "Objectif plan 1:55 non atteint mais > 2025 (2:01:56). FC moy (workout) 171, max 189.\n"
            "Workout 09:16 → 11:15 (118,8 min).\n"
        ),
        "segments": [
            ("Course", _t(9, 16, 0), _t(11, 15, 0)),
        ],
    },
]


def extract():
    dates = {r["date"] for r in RACES}
    buckets = {r["slug"]: {seg[0]: [] for seg in r["segments"]} for r in RACES}
    rx = re.compile(
        r'type="HKQuantityTypeIdentifierHeartRate".*?'
        r'startDate="(\d{4}-\d\d-\d\d) (\d\d):(\d\d):(\d\d)[^"]*".*?value="([\d.]+)"'
    )
    with open(EXPORT, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if 'HKQuantityTypeIdentifierHeartRate' not in line:
                continue
            if not any(f'startDate="{d} ' in line for d in dates):
                continue
            m = rx.search(line)
            if not m:
                continue
            d, hh, mm, ss, val = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), float(m.group(5))
            tt = time(hh, mm, ss)
            secs = hh * 3600 + mm * 60 + ss
            for r in RACES:
                if r["date"] != d:
                    continue
                for name, lo, hi in r["segments"]:
                    if lo <= tt < hi:
                        buckets[r["slug"]][name].append((secs, val))
                        break
    return buckets


def write_files(buckets):
    os.makedirs(OUTDIR, exist_ok=True)
    zone_hdr = " · ".join(z[0] for z in ZONES)
    for r in RACES:
        lines = [f"# {r['title']}", "", r["splits"], "## Heart rate par segment (zones)", ""]
        lines.append("| Segment | n | avg | min | max | " + " | ".join(z[0].split()[0] for z in ZONES) + " |")
        lines.append("|---|---|---|---|---|" + "---|" * len(ZONES))
        all_vals = []
        for name, _, _ in r["segments"]:
            vals = buckets[r["slug"]][name]
            all_vals += vals
            s = summarize(vals)
            if not s:
                lines.append(f"| {name} | 0 | — | — | — | " + " | ".join("—" for _ in ZONES) + " |")
                continue
            zc = " | ".join(f"{s['zones'][z[0]]:.0f}%" for z in ZONES)
            lines.append(f"| {name} | {s['n']} | {s['avg']:.0f} | {s['min']:.0f} | {s['max']:.0f} | {zc} |")
        s = summarize(all_vals)
        if s:
            zc = " | ".join(f"{s['zones'][z[0]]:.0f}%" for z in ZONES)
            lines.append(f"| **TOTAL** | {s['n']} | {s['avg']:.0f} | {s['min']:.0f} | {s['max']:.0f} | {zc} |")
        lines += ["", f"Zones (bpm) : {zone_hdr}.", "", "## Dérive (tiers) sur les segments longs", ""]
        for name, _, _ in r["segments"]:
            th = thirds(buckets[r["slug"]][name])
            if len(th) == 3:
                seg = "  ".join(f"{lab} avg {a:.0f}/max {mx:.0f}" for lab, a, mx in th)
                lines.append(f"- **{name}** : {seg}")
        lines += ["", f"> Source : raw `export.xml` ({EXPORT}), HR par échantillon. Fenêtres = segments GPX (JST)."]
        path = os.path.join(OUTDIR, r["slug"] + ".md")
        with open(path, "w", encoding="utf-8") as out:
            out.write("\n".join(lines) + "\n")
        n = summarize(all_vals)["n"] if summarize(all_vals) else 0
        print(f"wrote {os.path.normpath(path)}  ({n} HR samples)")


def main(argv: list[str] | None = None) -> int:
    """Archive every race in the RACES registry to `data/races/`.

    Takes no options: the export path comes from `AH_EXPORT` and the races
    themselves from the registry in this module. The parser exists so `--help`
    behaves like every other entry point and documents both.
    """
    argparse.ArgumentParser(
        description="Archive per-segment HR detail for the races in the RACES "
                    "registry. Set AH_EXPORT to point at a different export.xml; "
                    f"currently {EXPORT}. Output goes to {OUTDIR}.",
    ).parse_args(argv)
    write_files(extract())
    return 0


if __name__ == "__main__":
    write_files(extract())
