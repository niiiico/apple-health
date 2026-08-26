"""Tests for the HTML report generator over a tiny in-memory dataset."""

from __future__ import annotations

import sqlite3

import pytest

from apple_health.sinks import html_report


@pytest.fixture
def con() -> sqlite3.Connection:
    """A minimal health.db with just enough rows to exercise every series."""
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE workouts (id INTEGER PRIMARY KEY, activity TEXT, start TEXT,
            end TEXT, duration_min REAL, distance_km REAL, energy_kcal REAL,
            avg_hr REAL, max_hr REAL, source TEXT, indoor INTEGER);
        CREATE TABLE daily_metrics (day TEXT, type TEXT, unit TEXT, count INTEGER,
            sum REAL, min REAL, max REAL, avg REAL, PRIMARY KEY(day,type));
        CREATE TABLE records (type TEXT, start TEXT, value REAL, unit TEXT, source TEXT);
        """
    )
    c.executemany(
        "INSERT INTO workouts(activity,start,duration_min,distance_km,avg_hr) VALUES(?,?,?,?,?)",
        [
            ("Running", "2026-02-28 08:00:00 +0900", 139.0, 21.5, 159),  # long, Z2
            ("Running", "2022-11-12 09:00:00 +0900", 105.0, 21.12, 172),  # PR (Z4)
            ("Running", "2026-05-31 07:00:00 +0900", 119.0, 21.4, 171),  # race (Z4)
            ("Running", "2025-05-25 10:00:00 +0900", 122.0, 21.5, 178),  # Z5
            ("Cycling", "2026-04-01 10:00:00 +0900", 60.0, 25.0, 130),
        ],
    )
    c.executemany(
        "INSERT INTO daily_metrics(day,type,count,avg) VALUES(?,?,1,?)",
        [
            ("2023-12-01", "RunningCadence", 164.0),
            ("2026-05-01", "RunningCadence", 160.0),
            ("2024-12-01", "RestingHeartRate", 62.0),
            ("2026-05-01", "RestingHeartRate", 57.0),
            ("2024-09-01", "HeartRateVariabilitySDNN", 38.0),
            ("2026-05-01", "HeartRateVariabilitySDNN", 41.0),
            ("2026-06-07", "BodyMass", 78.3),
        ],
    )
    c.executemany(
        "INSERT INTO records(type,start,value) VALUES(?,?,?)",
        [
            ("VO2Max", "2023-12-01", 48.8),
            ("VO2Max", "2024-12-01", 39.5),
            ("VO2Max", "2026-06-06", 47.6),
        ],
    )
    c.commit()
    return c


def test_gather_kpis(con):
    data = html_report.gather(con)
    k = data["kpis"]
    assert k["workouts"] == 5
    assert k["vo2_now"] == 47.6
    assert k["mass_now"] == 78.3
    assert k["span_lo"] == "2022-11-12"
    assert k["span_hi"] == "2026-05-31"
    # PR = fastest half-distance run
    assert k["pr"]["date"] == "2022-11-12"
    assert k["pr"]["pace"] == "4:58"


def test_gather_series(con):
    data = html_report.gather(con)
    assert {r["q"] for r in data["vo2"]} == {"2023-Q4", "2024-Q4", "2026-Q2"}
    assert any(r["yr"] == "2022" for r in data["yearly"])
    # zones computed over the most recent year present (2026)
    assert data["zones_year"] == "2026"
    assert sum(r["runs"] for r in data["zones"]) == 2  # two 2026 runs with HR


def test_pace_formatting():
    assert html_report._pace(4.9667) == "4:58"
    assert html_report._pace(5.0) == "5:00"
    assert html_report._pace(5.999) == "6:00"  # rounds up cleanly


def test_render_produces_html(con):
    data = html_report.gather(con)
    html = html_report.render(data, "2026-06-14 00:00 UTC")
    assert html.lstrip().startswith("<!doctype html>")
    assert "__DATA__" not in html and "__GENERATED__" not in html and "__SPAN__" not in html
    assert "d3.v7.min.js" in html
    assert "Recommendations" in html
    assert '"vo2_now": 47.6' in html
