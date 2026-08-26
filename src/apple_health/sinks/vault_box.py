"""Push curated training markdown to the Claude Vault on Box (ADR-003).

Maintains, as **volatile rolling windows** (the durable archive stays in
``health.db`` and ``data/sessions/``):

- ``sport-natation-sessions.md`` / ``sport-course-sessions.md`` /
  ``sport-velo-sessions.md`` — last 5 sessions per discipline, with HR
  zones/drift when the inbox has the ``hr-<uuid>.csv`` series.
- ``sport-week-current.md`` — the weekly brief (render from
  ``vault_sport_week``).

Vault bookkeeping: missing ``_map.md`` rows are added (existing rows are left
untouched), and ``_changelog.md`` gets at most one auto section per day, only
when something actually changed.

Usage::

    uv run ah-vault [--db PATH] [--inbox PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date, datetime
from pathlib import Path

from ..config import repo_root
from ..sources.hr_series import HRSeriesSource, Series, default_source
from .box_client import BoxClient
from ..derive.zones import ZONES, summarize, thirds
from .session_files import _mmss
from . import weekly_brief

VAULT_FOLDER_ID = "380962826177"
DEFAULT_INBOX = Path("/Volumes/nicolas-data/HealthData/healthsync-inbox")
SESSIONS_PER_FILE = 5

# activity → (vault file name, French discipline label)
DISCIPLINES = {
    "Swimming": ("sport-natation-sessions.md", "natation"),
    "Running": ("sport-course-sessions.md", "course"),
    "Cycling": ("sport-velo-sessions.md", "vélo"),
}

_MAP_ROWS = {
    "sport-natation-sessions.md":
        "| `sport-natation-sessions.md` | sport, natation, sessions, fc | "
        "Détail rolling des 5 dernières nages (zones FC + dérive, auto vault_push) |",
    "sport-course-sessions.md":
        "| `sport-course-sessions.md` | sport, course, sessions, fc | "
        "Détail rolling des 5 dernières courses (zones FC + dérive, auto vault_push) |",
    "sport-velo-sessions.md":
        "| `sport-velo-sessions.md` | sport, velo, sessions, fc | "
        "Détail rolling des 5 dernières sorties vélo (zones FC + dérive, auto vault_push) |",
}


def _hr_lines(vals: Series) -> list[str]:
    """Compact zone + drift lines from a heart-rate series ('' if unusable).

    Takes samples rather than a path: see `sources.hr_series`.
    """
    s = summarize(vals)
    if not s:
        return []
    zones = " · ".join(f"{z[0].split()[0]} {s['zones'][z[0]]:.0f} %" for z in ZONES)
    out = [f"- Zones ({s['n']} éch.) : {zones}."]
    th = thirds(vals)
    if len(th) == 3:
        out.append("- Dérive par tiers : moy "
                   + " → ".join(f"{a:.0f}" for _, a, _ in th)
                   + f" ; max {max(mx for _, _, mx in th):.0f}.")
    return out


def _session_entry(w: sqlite3.Row, inbox: Path,
                   series: HRSeriesSource | None = None) -> list[str]:
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
    lines = [f"## {w['start'][:10]} — " + " / ".join(parts)]
    vals = (series or default_source(inbox)).series_for(w["uuid"])
    if vals:
        lines += _hr_lines(vals)
    else:
        lines.append("- Séries FC indisponibles — avg/max seulement.")
    return lines + [""]


def render_discipline(conn: sqlite3.Connection, activity: str, label: str,
                      inbox: Path, today: date,
                      series: HRSeriesSource | None = None) -> str:
    """Rolling session file for one discipline (most recent first).

    `series` chooses where heart-rate samples come from; the default reads the
    inbox sidecars, which is what this did before `hr_samples` existed.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM workouts WHERE activity = ? ORDER BY start DESC LIMIT ?",
        (activity, SESSIONS_PER_FILE)).fetchall()
    zone_hdr = " · ".join(z[0] for z in ZONES)
    lines = [
        "---",
        f"tags: [sport, {label}, sessions, fc]",
        "volatility: high",
        f"last_updated: {today.isoformat()}",
        "---",
        f"# {label.capitalize()} — {SESSIONS_PER_FILE} dernières séances (rolling, auto)",
        "",
        f"Généré par `tools/vault_push.py` depuis `health.db` + séries FC HealthSync ; "
        f"écrasé à chaque refresh (archive durable : repo `data/sessions/`). "
        f"Zones (bpm) : {zone_hdr}.",
        "",
    ]
    for w in rows:
        lines += _session_entry(w, inbox, series)
    return "\n".join(lines).rstrip() + "\n"


def _ensure_map_rows(existing: str, names: list[str]) -> str:
    """Insert missing manifest rows, keeping the table sorted by file name."""
    lines = existing.splitlines()
    for name in names:
        if f"`{name}`" in existing:
            continue
        row = _MAP_ROWS[name]
        anchor = next((i for i, l in enumerate(lines)
                       if l.startswith("| `") and l.split("`")[1] > name), len(lines))
        lines.insert(anchor, row)
        existing = "\n".join(lines)
    return "\n".join(lines).rstrip() + "\n"


def _append_changelog(existing: str, today: date, changed: list[str]) -> str | None:
    """New changelog content, or None if today's auto section already exists."""
    header = f"## {today.isoformat()} (auto — vault_push)"
    if header in existing:
        return None
    entry = f"{header}\n- UPDATED {', '.join(f'`{n}`' for n in changed)} — refresh rolling/brief automatique.\n"
    head, _, tail = existing.partition("\n## ")
    if not tail:
        return existing.rstrip() + "\n\n" + entry
    return f"{head.rstrip()}\n\n{entry}\n## {tail}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Push rolling session files + weekly brief to the Vault.")
    ap.add_argument("--db", type=Path, default=repo_root() / "data/health.db")
    ap.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    ap.add_argument("--dry-run", action="store_true", help="print what would change, upload nothing")
    args = ap.parse_args(argv)
    today = date.today()

    conn = sqlite3.connect(args.db)
    renders: dict[str, str] = {
        name: render_discipline(conn, activity, label, args.inbox, today)
        for activity, (name, label) in DISCIPLINES.items()
    }
    renders["sport-week-current.md"] = weekly_brief.render(conn, today)
    conn.close()

    box = BoxClient()
    vault = {i.name: i for i in box.list_folder(VAULT_FOLDER_ID)}

    changed = []
    for name, content in renders.items():
        current = box.download(vault[name].id).decode() if name in vault else None
        if current == content:
            continue
        changed.append(name)
        if not args.dry_run:
            box.upload(VAULT_FOLDER_ID, name, content.encode())
        print(f"{'would push' if args.dry_run else 'pushed'} {name}")
    if not changed:
        print("vault up to date")
        return 0

    map_now = box.download(vault["_map.md"].id).decode()
    map_new = _ensure_map_rows(map_now, list(_MAP_ROWS))
    if map_new != map_now and not args.dry_run:
        box.upload_version(vault["_map.md"].id, "_map.md", map_new.encode())
        print("updated _map.md")

    log_now = box.download(vault["_changelog.md"].id).decode()
    log_new = _append_changelog(log_now, today, changed)
    if log_new and not args.dry_run:
        box.upload_version(vault["_changelog.md"].id, "_changelog.md", log_new.encode())
        print("appended _changelog.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
