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


# --- backfill deltas (schema 2) -----------------------------------------------

def _backfill(seq: int, start: str, end: str, **sections) -> dict:
    """A schema-2 delta declaring authoritative coverage of [start, end]."""
    d = _delta(seq, **sections)
    d["schema"] = 2
    d["backfill"] = {"from": start, "to": end}
    return d


def test_backfill_replaces_instead_of_adding(tmp_path):
    """A backfill bucket overwrites a day the DB already partially holds."""
    conn, inbox = _conn(tmp_path), tmp_path
    _write_delta(inbox, "delta-20240301T120000Z-0001.json", _delta(
        1, daily_metrics={"added": [_bucket(count=2, sum=260.0, min=120.0, max=140.0)]}))
    ingest.ingest_dir(conn, inbox)

    # Same day re-shipped as a whole-day authoritative aggregate.
    _write_delta(inbox, "delta-20240305T120000Z-0002.json", _backfill(
        2, "2024-03-01", "2024-03-01",
        daily_metrics={"added": [_bucket(count=10, sum=1300.0, min=100.0, max=170.0)]}))
    ingest.ingest_dir(conn, inbox)

    row = conn.execute(
        "select count, sum, min, max, avg from daily_metrics "
        "where day='2024-03-01' and type='HeartRate'").fetchone()
    assert row == (10, 1300.0, 100.0, 170.0, 130.0)  # replaced, not 12/1560


def test_backfill_is_content_idempotent(tmp_path):
    """Re-applying the same backfill under a new filename changes nothing."""
    conn, inbox = _conn(tmp_path), tmp_path
    for name in ("delta-20240305T120000Z-0001.json", "delta-20240306T120000Z-0002.json"):
        _write_delta(inbox, name, _backfill(
            1, "2024-03-01", "2024-03-01",
            workouts={"added": [_workout()], "deleted": []},
            daily_metrics={"added": [_bucket(count=10, sum=1300.0)]}))
        ingest.ingest_dir(conn, inbox)

    assert conn.execute("select count(*) from workouts").fetchone()[0] == 1
    assert conn.execute(
        "select count from daily_metrics where day='2024-03-01'").fetchone()[0] == 10


def test_backfill_rejects_range_including_today(tmp_path):
    """Today is still open, so it can never be authoritative."""
    from datetime import date
    conn, inbox = _conn(tmp_path), tmp_path
    today = date.today().isoformat()
    _write_delta(inbox, "delta-20240305T120000Z-0001.json",
                 _backfill(1, today, today,
                           daily_metrics={"added": [_bucket(day=today)]}))
    with pytest.raises(ValueError, match="must end before today"):
        ingest.ingest_dir(conn, inbox)


def test_backfill_rejects_bucket_outside_declared_range(tmp_path):
    conn, inbox = _conn(tmp_path), tmp_path
    _write_delta(inbox, "delta-20240305T120000Z-0001.json", _backfill(
        1, "2024-03-01", "2024-03-02",
        daily_metrics={"added": [_bucket(day="2024-03-09")]}))
    with pytest.raises(ValueError, match="outside the declared backfill range"):
        ingest.ingest_dir(conn, inbox)


def test_backfill_block_requires_schema_2(tmp_path):
    """A schema-1 file carrying 'backfill' would be merged additively by an
    older ingest, so it must be refused outright."""
    conn, inbox = _conn(tmp_path), tmp_path
    d = _delta(1, daily_metrics={"added": [_bucket()]})
    d["backfill"] = {"from": "2024-03-01", "to": "2024-03-01"}
    _write_delta(inbox, "delta-20240305T120000Z-0001.json", d)
    with pytest.raises(ValueError, match="requires schema 2"):
        ingest.ingest_dir(conn, inbox)


def test_schema_2_requires_backfill_block(tmp_path):
    conn, inbox = _conn(tmp_path), tmp_path
    d = _delta(1, daily_metrics={"added": [_bucket()]})
    d["schema"] = 2
    _write_delta(inbox, "delta-20240305T120000Z-0001.json", d)
    with pytest.raises(ValueError, match="requires a 'backfill' block"):
        ingest.ingest_dir(conn, inbox)


def test_normal_delta_after_backfill_still_adds(tmp_path):
    """Backfill mode must not leak: a later incremental for an *uncovered*
    day still merges additively."""
    conn, inbox = _conn(tmp_path), tmp_path
    _write_delta(inbox, "delta-20240305T120000Z-0001.json", _backfill(
        1, "2024-03-01", "2024-03-01",
        daily_metrics={"added": [_bucket(count=10, sum=1300.0)]}))
    _write_delta(inbox, "delta-20240306T120000Z-0002.json", _delta(
        2, daily_metrics={"added": [_bucket(day="2024-03-02", count=3, sum=400.0)],
                          }))
    _write_delta(inbox, "delta-20240307T120000Z-0003.json", _delta(
        3, daily_metrics={"added": [_bucket(day="2024-03-02", count=1, sum=100.0)]}))
    ingest.ingest_dir(conn, inbox)

    assert conn.execute(
        "select count, sum from daily_metrics where day='2024-03-02'").fetchone() == (4, 500.0)
