"""Tests for the streaming export.xml parser."""

from __future__ import annotations

import textwrap

from apple_health import db, parse_export

SAMPLE = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <HealthData locale="en_JP">
      <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Watch" unit="count/min"
              startDate="2024-03-01 07:12:33 +0900" endDate="2024-03-01 07:12:33 +0900" value="120"/>
      <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Watch" unit="count/min"
              startDate="2024-03-01 07:13:00 +0900" endDate="2024-03-01 07:13:00 +0900" value="140"/>
      <Record type="HKQuantityTypeIdentifierRestingHeartRate" sourceName="Watch" unit="count/min"
              startDate="2024-03-01 06:00:00 +0900" endDate="2024-03-01 06:00:00 +0900" value="52"/>
      <Record type="HKQuantityTypeIdentifierVO2Max" sourceName="Watch" unit="mL/min·kg"
              startDate="2024-03-02 06:00:00 +0900" endDate="2024-03-02 06:00:00 +0900" value="48.5"/>
      <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="60"
               durationUnit="min" totalDistance="10" totalDistanceUnit="km"
               totalEnergyBurned="600" sourceName="Watch"
               startDate="2024-03-01 07:00:00 +0900" endDate="2024-03-01 08:00:00 +0900">
        <MetadataEntry key="HKIndoorWorkout" value="0"/>
        <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" average="155" maximum="178"/>
        <WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned" sum="612" unit="kcal"/>
      </Workout>
    </HealthData>
    """)


def _write_sample(tmp_path):
    p = tmp_path / "export.xml"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


def test_aggregates_and_sparse(tmp_path):
    res = parse_export.parse_export(_write_sample(tmp_path), progress_every=0)

    # Two heart-rate samples on the same day collapse to one daily row.
    hr = res.daily[("2024-03-01", "HeartRate")]
    assert hr.count == 2
    assert hr.min == 120 and hr.max == 140
    assert hr.sum == 260

    # HeartRate is dense → not kept raw; RestingHeartRate/VO2Max are sparse → kept raw.
    kept_types = {r[0] for r in res.records}
    assert kept_types == {"RestingHeartRate", "VO2Max"}
    assert "HeartRate" not in kept_types


def test_workout_fields(tmp_path):
    res = parse_export.parse_export(_write_sample(tmp_path), progress_every=0)
    assert len(res.workouts) == 1
    activity, start, _end, dur, dist, energy, avg_hr, max_hr, source, indoor = res.workouts[0]
    assert activity == "Running"
    assert dur == 60.0
    assert dist == 10.0
    assert avg_hr == 155 and max_hr == 178
    assert energy == 612  # WorkoutStatistics sum preferred over totalEnergyBurned
    assert indoor == 0


def test_write_roundtrip(tmp_path):
    res = parse_export.parse_export(_write_sample(tmp_path), progress_every=0)
    conn = db.connect(tmp_path / "h.db")
    db.init_schema(conn)
    parse_export.write(conn, res)

    assert conn.execute("SELECT count(*) FROM workouts").fetchone()[0] == 1
    avg = conn.execute(
        "SELECT avg FROM daily_metrics WHERE type='HeartRate'").fetchone()[0]
    assert avg == 130.0
    assert conn.execute(
        "SELECT value FROM records WHERE type='RestingHeartRate'").fetchone()[0] == 52
