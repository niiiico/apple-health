"""Streaming parser for Apple Health ``export.xml``.

The export is several gigabytes, so it is parsed with ``iterparse`` and the
element tree is cleared after every top-level element to keep memory flat.

Two top-level element kinds matter here:

* ``Record``  — a single quantity/category sample. Quantity samples are folded
  into per-day aggregates (``daily_metrics``); a small allowlist of sparse,
  high-value types is additionally kept raw (``records``).
* ``Workout`` — one workout, with optional ``WorkoutStatistics`` children that
  carry per-workout average/max heart rate, distance and energy.

Aggregation is done in memory: the number of distinct (day, type) pairs is
bounded (≈ years × ~60 types), so a dict easily holds it.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Quantity types kept as raw rows (sparse, one or few per day, high analytic value).
SPARSE_TYPES = {
    "RestingHeartRate",
    "VO2Max",
    "BodyMass",
    "HeartRateVariabilitySDNN",
    "WalkingHeartRateAverage",
    "BodyFatPercentage",
}

# Prefixes stripped to normalise type / activity names.
_TYPE_PREFIXES = (
    "HKQuantityTypeIdentifier",
    "HKCategoryTypeIdentifier",
    "HKDataTypeIdentifier",
)
_ACTIVITY_PREFIX = "HKWorkoutActivityType"


def _strip(name: str, prefixes: tuple[str, ...] | str) -> str:
    if isinstance(prefixes, str):
        prefixes = (prefixes,)
    for p in prefixes:
        if name.startswith(p):
            return name[len(p):]
    return name


def _day(iso: str) -> str:
    """Extract YYYY-MM-DD from an Apple Health date like '2024-03-01 07:12:33 +0900'."""
    return iso[:10]


def _f(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


@dataclass
class _Agg:
    unit: str | None = None
    count: int = 0
    sum: float = 0.0
    min: float = float("inf")
    max: float = float("-inf")

    def add(self, value: float, unit: str | None) -> None:
        self.count += 1
        self.sum += value
        if value < self.min:
            self.min = value
        if value > self.max:
            self.max = value
        if unit:
            self.unit = unit


@dataclass
class ParseResult:
    """Collected rows, ready for bulk insert."""

    workouts: list[tuple] = field(default_factory=list)
    records: list[tuple] = field(default_factory=list)
    # keyed by (day, type) -> _Agg
    daily: dict[tuple[str, str], _Agg] = field(default_factory=dict)
    n_records_seen: int = 0
    n_workouts_seen: int = 0


def _workout_stats(elem: ET.Element) -> dict[str, float | None]:
    """Pull avg/max HR, distance, energy from a Workout's children."""
    out: dict[str, float | None] = {
        "avg_hr": None, "max_hr": None, "distance_km": None,
        "energy_kcal": None, "indoor": None,
    }
    for child in elem:
        tag = child.tag
        if tag == "WorkoutStatistics":
            stype = _strip(child.get("type", ""), _TYPE_PREFIXES)
            if stype == "HeartRate":
                out["avg_hr"] = _f(child.get("average"))
                out["max_hr"] = _f(child.get("maximum"))
            elif stype in ("DistanceWalkingRunning", "DistanceCycling", "DistanceSwimming"):
                d = _f(child.get("sum"))
                if d is not None:
                    unit = (child.get("unit") or "").lower()
                    out["distance_km"] = d / 1000.0 if unit in ("m", "meter") else d
            elif stype == "ActiveEnergyBurned":
                out["energy_kcal"] = _f(child.get("sum"))
        elif tag == "MetadataEntry" and child.get("key") == "HKIndoorWorkout":
            out["indoor"] = 1 if child.get("value") in ("1", "true") else 0
    return out


def parse_export(path: str | Path, progress_every: int = 1_000_000) -> ParseResult:
    """Stream-parse ``export.xml`` into a :class:`ParseResult`.

    Args:
        path: path to export.xml.
        progress_every: print a progress line every N records (0 to silence).
    """
    res = ParseResult()
    context = ET.iterparse(str(path), events=("start", "end"))
    _, root = next(context)

    for event, elem in context:
        if event != "end":
            continue
        tag = elem.tag

        if tag == "Record":
            res.n_records_seen += 1
            rtype = _strip(elem.get("type", ""), _TYPE_PREFIXES)
            start = elem.get("startDate", "")
            unit = elem.get("unit")
            value = _f(elem.get("value"))
            if value is not None and start:
                key = (_day(start), rtype)
                agg = res.daily.get(key)
                if agg is None:
                    agg = res.daily[key] = _Agg()
                agg.add(value, unit)
                if rtype in SPARSE_TYPES:
                    res.records.append((rtype, start, value, unit, elem.get("sourceName")))
            root.clear()
            if progress_every and res.n_records_seen % progress_every == 0:
                print(f"  …{res.n_records_seen:,} records")

        elif tag == "Workout":
            res.n_workouts_seen += 1
            activity = _strip(elem.get("workoutActivityType", ""), _ACTIVITY_PREFIX)
            dur = _f(elem.get("duration"))
            if dur is not None and (elem.get("durationUnit") or "min").lower().startswith("s"):
                dur = dur / 60.0
            dist = _f(elem.get("totalDistance"))
            if dist is not None and (elem.get("totalDistanceUnit") or "").lower() in ("m", "meter"):
                dist = dist / 1000.0
            energy = _f(elem.get("totalEnergyBurned"))
            stats = _workout_stats(elem)
            res.workouts.append((
                activity,
                elem.get("startDate", ""),
                elem.get("endDate"),
                dur,
                stats["distance_km"] if stats["distance_km"] is not None else dist,
                stats["energy_kcal"] if stats["energy_kcal"] is not None else energy,
                stats["avg_hr"],
                stats["max_hr"],
                elem.get("sourceName"),
                stats["indoor"],
            ))
            root.clear()

    return res


def write(conn, res: ParseResult) -> None:
    """Bulk-insert a :class:`ParseResult` into an initialised DB connection."""
    conn.executemany(
        "INSERT INTO workouts "
        "(activity, start, end, duration_min, distance_km, energy_kcal, avg_hr, max_hr, source, indoor) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        res.workouts,
    )
    conn.executemany(
        "INSERT INTO records (type, start, value, unit, source) VALUES (?,?,?,?,?)",
        res.records,
    )
    daily_rows = [
        (day, rtype, a.unit, a.count, a.sum,
         None if a.min == float("inf") else a.min,
         None if a.max == float("-inf") else a.max,
         a.sum / a.count if a.count else None)
        for (day, rtype), a in res.daily.items()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO daily_metrics (day, type, unit, count, sum, min, max, avg) "
        "VALUES (?,?,?,?,?,?,?,?)",
        daily_rows,
    )
    conn.commit()
