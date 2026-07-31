"""Tests for the incremental delta-ingest path."""

from __future__ import annotations

import json
import textwrap

import pytest

from apple_health import db, ingest

# A GPX matching Apple's workout-route shape (two points ~111 m apart, 60 s).
GPX = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <gpx version="1.1" creator="HealthSync" xmlns="http://www.topografix.com/GPX/1/1">
    <trk><trkseg>
      <trkpt lat="35.700000" lon="139.700000"><ele>10.0</ele><time>2024-03-01T07:00:00Z</time></trkpt>
      <trkpt lat="35.701000" lon="139.700000"><ele>15.0</ele><time>2024-03-01T07:01:00Z</time></trkpt>
    </trkseg></trk></gpx>
    """)


def _delta(seq: int, **sections) -> dict:
    """Build a minimal valid delta with the given sections filled in."""
    return {
        "schema": 1,
        "generated_at": "2024-03-01T12:00:00Z",
        "device": "test",
        "app_version": "test",
        "anchor_seq": seq,
        "workouts": sections.get("workouts", {"added": [], "deleted": []}),
        "records": sections.get("records", {"added": [], "deleted": []}),
        "daily_metrics": sections.get("daily_metrics", {"added": []}),
    }


def _write_delta(inbox, name: str, delta: dict) -> None:
    (inbox / name).write_text(json.dumps(delta), encoding="utf-8")


def _workout(uuid="W1", route_file=None):
    return {
        "uuid": uuid, "activity": "Running",
        "start": "2024-03-01 07:00:00 +0900", "end": "2024-03-01 08:00:00 +0900",
        "duration_min": 60.0, "distance_km": 10.0, "energy_kcal": 600.0,
        "avg_hr": 155.0, "max_hr": 178.0, "source": "Watch", "indoor": 0,
        "route_file": route_file,
    }


def _bucket(day="2024-03-01", type="HeartRate", count=2, sum=260.0, min=120.0, max=140.0):
    return {"day": day, "type": type, "unit": "count/min",
            "count": count, "sum": sum, "min": min, "max": max}


def _conn(tmp_path):
    return db.connect(tmp_path / "health.db")


def test_apply_basic(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_delta(inbox, "delta-20240301T120000Z-0001.json", _delta(
        1,
        workouts={"added": [_workout()], "deleted": []},
        records={"added": [{"type": "RestingHeartRate", "start": "2024-03-01 06:00:00 +0900",
                            "value": 52.0, "unit": "count/min", "source": "Watch"}], "deleted": []},
        daily_metrics={"added": [_bucket()]},
    ))
    conn = _conn(tmp_path)
    s = ingest.ingest_dir(conn, inbox)

    assert s.files == 1 and s.workouts == 1 and s.records == 1 and s.daily == 1
    assert conn.execute("SELECT count(*) FROM workouts").fetchone()[0] == 1
    assert conn.execute("SELECT uuid FROM workouts").fetchone()[0] == "W1"
    avg = conn.execute("SELECT avg FROM daily_metrics WHERE type='HeartRate'").fetchone()[0]
    assert avg == 130.0
    assert conn.execute("SELECT value FROM records WHERE type='RestingHeartRate'").fetchone()[0] == 52


def test_idempotent_rerun(tmp_path):
    """Re-running over the same inbox applies nothing the second time."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_delta(inbox, "delta-20240301T120000Z-0001.json", _delta(
        1, workouts={"added": [_workout()]}, daily_metrics={"added": [_bucket()]}))
    conn = _conn(tmp_path)

    ingest.ingest_dir(conn, inbox)
    s2 = ingest.ingest_dir(conn, inbox)  # same files, already applied

    assert s2.files == 0
    assert conn.execute("SELECT count(*) FROM workouts").fetchone()[0] == 1
    # daily_metrics must NOT have doubled.
    row = conn.execute("SELECT count, sum FROM daily_metrics WHERE type='HeartRate'").fetchone()
    assert row == (2, 260.0)


def test_daily_additive_merge(tmp_path):
    """Two deltas for the same (day,type) accumulate count/sum and fold min/max."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_delta(inbox, "delta-20240301T120000Z-0001.json", _delta(
        1, daily_metrics={"added": [_bucket(count=2, sum=260.0, min=120.0, max=140.0)]}))
    _write_delta(inbox, "delta-20240302T120000Z-0002.json", _delta(
        2, daily_metrics={"added": [_bucket(count=1, sum=100.0, min=100.0, max=100.0)]}))
    conn = _conn(tmp_path)
    ingest.ingest_dir(conn, inbox)

    count, ssum, mn, mx, avg = conn.execute(
        "SELECT count, sum, min, max, avg FROM daily_metrics WHERE type='HeartRate'").fetchone()
    assert count == 3
    assert ssum == 360.0
    assert mn == 100.0 and mx == 140.0
    assert avg == 120.0  # 360 / 3


def test_workout_dedup_by_uuid(tmp_path):
    """The same workout uuid arriving in two deltas inserts once."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_delta(inbox, "delta-20240301T120000Z-0001.json", _delta(
        1, workouts={"added": [_workout(uuid="W1")]}))
    _write_delta(inbox, "delta-20240302T120000Z-0002.json", _delta(
        2, workouts={"added": [_workout(uuid="W1")]}))
    conn = _conn(tmp_path)
    ingest.ingest_dir(conn, inbox)
    assert conn.execute("SELECT count(*) FROM workouts").fetchone()[0] == 1


def test_workout_delete(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_delta(inbox, "delta-20240301T120000Z-0001.json", _delta(
        1, workouts={"added": [_workout(uuid="W1")]}))
    _write_delta(inbox, "delta-20240302T120000Z-0002.json", _delta(
        2, workouts={"added": [], "deleted": ["W1"]}))
    conn = _conn(tmp_path)
    s = ingest.ingest_dir(conn, inbox)
    assert s.deleted_workouts == 1
    assert conn.execute("SELECT count(*) FROM workouts").fetchone()[0] == 0


def test_routes_ingested(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "route-W1.gpx").write_text(GPX, encoding="utf-8")
    _write_delta(inbox, "delta-20240301T120000Z-0001.json", _delta(
        1, workouts={"added": [_workout(uuid="W1", route_file="route-W1.gpx")]}))
    conn = _conn(tmp_path)
    s = ingest.ingest_dir(conn, inbox)
    assert s.routes == 1
    row = conn.execute("SELECT filename, n_points FROM routes").fetchone()
    assert row[0] == "route-W1.gpx" and row[1] == 2


def test_cadence_rederived(tmp_path):
    """RunningSpeed + RunningStrideLength buckets yield a derived RunningCadence."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_delta(inbox, "delta-20240301T120000Z-0001.json", _delta(1, daily_metrics={"added": [
        {"day": "2024-03-01", "type": "RunningSpeed", "unit": "km/hr", "count": 100,
         "sum": 960.0, "min": 9.6, "max": 9.6},
        {"day": "2024-03-01", "type": "RunningStrideLength", "unit": "m", "count": 100,
         "sum": 100.0, "min": 1.0, "max": 1.0},
    ]}))
    conn = _conn(tmp_path)
    s = ingest.ingest_dir(conn, inbox)
    assert s.cadence_days == 1
    avg = conn.execute("SELECT avg FROM daily_metrics WHERE type='RunningCadence'").fetchone()[0]
    assert abs(avg - 160.0) < 0.05  # 9.6 km/h / 1.0 m stride → 160 spm


def test_refuses_full_build_db(tmp_path):
    """Ingesting into a mode=full DB is blocked unless forced."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_delta(inbox, "delta-20240301T120000Z-0001.json", _delta(
        1, daily_metrics={"added": [_bucket()]}))
    conn = _conn(tmp_path)
    db.init_schema(conn)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('mode', 'full')")
    conn.commit()

    with pytest.raises(SystemExit):
        ingest.ingest_dir(conn, inbox)

    # --force lets it through.
    s = ingest.ingest_dir(conn, inbox, force=True)
    assert s.files == 1


def test_unsupported_schema_raises(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    d = _delta(1, daily_metrics={"added": [_bucket()]})
    d["schema"] = 99
    _write_delta(inbox, "delta-20240301T120000Z-0001.json", d)
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        ingest.ingest_dir(conn, inbox)


def test_ensure_incremental_schema_adds_uuid(tmp_path):
    """A workouts table predating the uuid column gets it added, idempotently."""
    conn = db.connect(tmp_path / "old.db")
    conn.executescript(
        "CREATE TABLE workouts (id INTEGER PRIMARY KEY, activity TEXT NOT NULL, "
        "start TEXT NOT NULL, end TEXT, duration_min REAL, distance_km REAL, "
        "energy_kcal REAL, avg_hr REAL, max_hr REAL, source TEXT, indoor INTEGER);")
    conn.commit()
    db.ensure_incremental_schema(conn)
    db.ensure_incremental_schema(conn)  # second call is a no-op
    cols = {row[1] for row in conn.execute("PRAGMA table_info(workouts)")}
    assert "uuid" in cols
