"""SQLite schema and connection helpers for the Apple Health dataset.

The schema is intentionally compact. Apple Health exports contain tens of
millions of raw quantity samples (HeartRate alone can be millions of rows over
several years). Storing every sample is wasteful for training analysis, so:

* ``workouts``       — one row per workout, fully kept.
* ``daily_metrics``  — every quantity type collapsed to one row per (day, type)
                       with count/sum/min/max/avg. Compact daily time series.
* ``records``        — raw rows kept ONLY for a small allowlist of sparse,
                       high-value types (resting HR, VO2max, body mass, HRV).
* ``routes``         — one summary row per GPX file (distance, duration, bbox,
                       elevation gain). Raw track points are NOT stored here;
                       they can be loaded on demand for a heatmap later.

A plain schema is used rather than the SQLite migration framework because this
is a single-version, rebuild-from-source dataset: the DB is disposable and
regenerated from the immutable export, so migrations buy nothing here.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS workouts (
    id           INTEGER PRIMARY KEY,
    activity     TEXT NOT NULL,      -- normalised activity type (e.g. "Running")
    start        TEXT NOT NULL,      -- ISO8601 startDate
    end          TEXT,               -- ISO8601 endDate
    duration_min REAL,               -- minutes
    distance_km  REAL,               -- kilometres (NULL if none)
    energy_kcal  REAL,               -- active energy burned
    avg_hr       REAL,               -- from WorkoutStatistics, if present
    max_hr       REAL,
    source       TEXT,               -- sourceName (device/app)
    indoor       INTEGER             -- 1 indoor, 0 outdoor, NULL unknown
);
CREATE INDEX IF NOT EXISTS ix_workouts_start ON workouts(start);
CREATE INDEX IF NOT EXISTS ix_workouts_activity ON workouts(activity);

-- One row per (day, quantity type): compact daily aggregate of every metric.
CREATE TABLE IF NOT EXISTS daily_metrics (
    day   TEXT NOT NULL,             -- YYYY-MM-DD (local-ish, from startDate)
    type  TEXT NOT NULL,             -- normalised metric name (e.g. "HeartRate")
    unit  TEXT,
    count INTEGER NOT NULL,
    sum   REAL,
    min   REAL,
    max   REAL,
    avg   REAL,
    PRIMARY KEY (day, type)
);
CREATE INDEX IF NOT EXISTS ix_daily_type ON daily_metrics(type);

-- Raw samples kept only for the sparse allowlist (see parse_export.SPARSE_TYPES).
CREATE TABLE IF NOT EXISTS records (
    type  TEXT NOT NULL,
    start TEXT NOT NULL,
    value REAL,
    unit  TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS ix_records_type_start ON records(type, start);

CREATE TABLE IF NOT EXISTS routes (
    id            INTEGER PRIMARY KEY,
    filename      TEXT NOT NULL UNIQUE,
    start         TEXT,              -- first track point time (ISO8601)
    end           TEXT,
    n_points      INTEGER,
    distance_km   REAL,
    duration_min  REAL,
    elev_gain_m   REAL,
    avg_speed_kmh REAL,
    min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL
);
CREATE INDEX IF NOT EXISTS ix_routes_start ON routes(start);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating parent dirs as needed) a tuned SQLite connection."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes if they do not exist."""
    conn.executescript(SCHEMA)
    conn.commit()


def derive_cadence(conn: sqlite3.Connection) -> int:
    """Derive a daily ``RunningCadence`` (steps/min) metric.

    Apple Health does not export running cadence as a quantity type. It is
    recovered from running speed and stride length, which it does export::

        cadence [steps/min] = speed [km/h] * 1000 / 60 / stride_length [m]

    One row per day where both inputs exist is written into ``daily_metrics``
    as the synthetic type ``RunningCadence``. Returns the number of days
    derived. Validated against logged footing cadence (~162–165 spm).
    """
    cur = conn.execute(
        """
        INSERT OR REPLACE INTO daily_metrics (day, type, unit, count, sum, min, max, avg)
        SELECT s.day, 'RunningCadence', 'spm', s.count, NULL, NULL, NULL,
               round(s.avg * 1000.0 / 60.0 / l.avg, 1)
        FROM daily_metrics s
        JOIN daily_metrics l ON s.day = l.day
        WHERE s.type = 'RunningSpeed' AND l.type = 'RunningStrideLength'
          AND l.avg > 0 AND s.avg IS NOT NULL
        """
    )
    conn.commit()
    return cur.rowcount
