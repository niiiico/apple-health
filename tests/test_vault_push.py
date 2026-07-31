"""Tests for the pure (no-network) parts of the Box/Vault pipeline:
vault_push rendering + bookkeeping helpers, and box_fetch download planning.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import box_fetch  # noqa: E402
import vault_push  # noqa: E402
from apple_health import db  # noqa: E402
from apple_health.box_client import BoxItem  # noqa: E402


def _conn(tmp_path):
    conn = db.connect(tmp_path / "health.db")
    db.init_schema(conn)
    db.ensure_incremental_schema(conn)
    conn.execute(
        "INSERT INTO workouts (uuid, activity, start, end, duration_min,"
        " distance_km, energy_kcal, avg_hr, max_hr, source)"
        " VALUES ('U1', 'Swimming', '2026-07-10 12:55:26 +0900',"
        " '2026-07-10 13:39:00 +0900', 44.0, 1.5, 343.0, 143.0, 171.0, 'Watch')")
    conn.commit()
    return conn


def test_render_discipline_with_hr_series(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    def row(i: int) -> str:  # one sample every 20 s from 03:55:00Z
        t = 3 * 3600 + 55 * 60 + i * 20
        return f"2026-07-10T{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}Z,{130 + i % 30}\n"

    (inbox / "hr-U1.csv").write_text("time,bpm\n" + "".join(row(i) for i in range(150)))
    out = vault_push.render_discipline(
        _conn(tmp_path), "Swimming", "natation", inbox, date(2026, 7, 14))
    assert "## 2026-07-10 — 1.50 km / 44 min / FC 143/171 / 343 kcal" in out
    assert "Zones (150 éch.)" in out
    assert "Dérive par tiers" in out
    assert "volatility: high" in out


def test_render_discipline_without_hr_series(tmp_path):
    out = vault_push.render_discipline(
        _conn(tmp_path), "Swimming", "natation", tmp_path, date(2026, 7, 14))
    assert "Séries FC indisponibles" in out


def test_ensure_map_rows_inserts_sorted_and_is_idempotent():
    existing = "\n".join([
        "# _map.md", "", "| File | Tags | Summary |", "|------|------|---------|",
        "| `aaa.md` | a | A |",
        "| `sport-running-plan.md` | sport | plan |",
        "| `zzz.md` | z | Z |", ""])
    out = vault_push._ensure_map_rows(existing, list(vault_push._MAP_ROWS))
    lines = out.splitlines()
    idx = {n: next(i for i, l in enumerate(lines) if f"`{n}`" in l)
           for n in vault_push._MAP_ROWS}
    assert idx["sport-course-sessions.md"] < idx["sport-natation-sessions.md"]
    assert idx["sport-natation-sessions.md"] < idx["sport-velo-sessions.md"]
    # sorted against pre-existing rows too
    assert idx["sport-velo-sessions.md"] < next(
        i for i, l in enumerate(lines) if "`zzz.md`" in l)
    assert vault_push._ensure_map_rows(out, list(vault_push._MAP_ROWS)) == out


def test_append_changelog_once_per_day():
    existing = "# Changelog — Claude Vault\n\n## 2026-07-12 (x)\n- old\n"
    out = vault_push._append_changelog(existing, date(2026, 7, 14), ["a.md"])
    assert out.index("## 2026-07-14 (auto — vault_push)") < out.index("## 2026-07-12")
    assert "`a.md`" in out
    assert vault_push._append_changelog(out, date(2026, 7, 14), ["b.md"]) is None


def test_plan_downloads_orders_sidecars_before_deltas():
    remote = [
        BoxItem("1", "delta-20260701T000000Z-0001.json", "file"),
        BoxItem("2", "hr-B.csv", "file"),
        BoxItem("3", "route-A.gpx", "file"),
        BoxItem("4", "delta-20260630T000000Z-0000.json", "file"),
        BoxItem("5", "already.json", "file"),
        BoxItem("6", "subfolder", "folder"),
    ]
    plan = box_fetch.plan_downloads(remote, {"already.json"})
    names = [i.name for i in plan]
    assert names == ["hr-B.csv", "route-A.gpx",
                     "delta-20260630T000000Z-0000.json",
                     "delta-20260701T000000Z-0001.json"]
