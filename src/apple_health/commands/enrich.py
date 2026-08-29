"""``ah-enrich`` — backfill workout metadata the pipeline used to discard.

The watch records weather, elevation, METs and pool length on the workout
itself, and none of it has ever been carried: the delta path sends a fixed set
of fields, and the full-export parser reads the attributes but not the
``MetadataEntry`` children. Of 2,752 archived workouts, 1,323 carry weather,
1,038 elevation and 1,151 METs.

This recovers them for everything the export covers. Workouts after it need the
app to send them, which it now does.

Units are normalised on the way in, because Apple's are not consistent: weather
temperature is written in °F on this device but the key does not promise it,
elevation is in centimetres despite reading like metres, and humidity is a
percentage times one hundred.

Usage::

    uv run ah-enrich --export /path/export.xml [--report]
"""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

from ..store import Store

_APPLE = "%Y-%m-%d %H:%M:%S %z"


def _quantity(raw: str | None) -> tuple[float, str] | None:
    """Split Apple's "79 degF" into a number and its unit."""
    if not raw:
        return None
    parts = raw.strip().split()
    try:
        value = float(parts[0])
    except (ValueError, IndexError):
        return None
    return value, (parts[1] if len(parts) > 1 else "")


def temperature_c(raw: str | None) -> float | None:
    """Celsius, whatever Apple wrote.

    The key does not promise a unit and this device writes °F, so converting on
    the value's own unit is the only safe reading — assuming Celsius would put
    a 79 °F morning at 79 °C and make every hot day look survivable.
    """
    q = _quantity(raw)
    if q is None:
        return None
    value, unit = q
    if unit.lower() in ("degf", "°f", "f"):
        return (value - 32) * 5 / 9
    if unit.lower() in ("degc", "°c", "c", ""):
        return value
    if unit.lower() == "k":
        return value - 273.15
    return None


def humidity_pct(raw: str | None) -> float | None:
    """Percent. Apple writes "6100 %" for 61 %, so a plain read is 100× wrong."""
    q = _quantity(raw)
    if q is None:
        return None
    value, _unit = q
    return value / 100 if value > 100 else value


def length_m(raw: str | None) -> float | None:
    """Metres, from whatever length unit was written."""
    q = _quantity(raw)
    if q is None:
        return None
    value, unit = q
    factor = {"cm": 0.01, "m": 1.0, "km": 1000.0, "in": 0.0254,
              "ft": 0.3048, "yd": 0.9144, "mi": 1609.344}.get(unit.lower())
    return value * factor if factor else (value if not unit else None)


def speed_kmh(raw: str | None) -> float | None:
    q = _quantity(raw)
    if q is None:
        return None
    value, unit = q
    # "m/s" normalises to "ms", not "mh" — the first version mapped it to the
    # metres-per-hour key and returned None for every speed the watch records.
    u = unit.lower().replace("/", "").replace("hr", "h").replace("sec", "s")
    return {
        "kmh": value,
        "ms": value * 3.6,              # metres per second, what HealthKit writes
        "mh": value / 1000.0,           # metres per hour, for completeness
        "mih": value * 1.609344,
        "mis": value * 5793.6384,
    }.get(u)


def read_workouts(export: Path) -> list[dict[str, Any]]:
    """Every workout's start instant and the metadata attached to it."""
    out: list[dict[str, Any]] = []
    for _ev, el in ET.iterparse(str(export), events=("end",)):
        if el.tag != "Workout":
            # Deliberately not cleared: clearing a child wipes the attributes
            # its parent is about to be asked for, and this silently produced
            # "no metadata anywhere" the first time round.
            continue
        meta = {m.get("key"): m.get("value") for m in el.findall("MetadataEntry")}
        try:
            start = datetime.strptime(el.get("startDate"), _APPLE)
        except (TypeError, ValueError):
            el.clear()
            continue
        out.append({
            "start": start,
            "weather_temp_c": temperature_c(meta.get("HKWeatherTemperature")),
            "weather_humidity_pct": humidity_pct(meta.get("HKWeatherHumidity")),
            "elevation_ascended_m": length_m(meta.get("HKElevationAscended")),
            "elevation_descended_m": length_m(meta.get("HKElevationDescended")),
            "avg_mets": (_quantity(meta.get("HKAverageMETs")) or (None,))[0],
            "pool_length_m": length_m(meta.get("HKLapLength")),
            "swim_location": meta.get("HKSwimmingLocationType"),
            "max_speed_kmh": speed_kmh(meta.get("HKMaximumSpeed")),
        })
        el.clear()
    return out


FIELDS = ("weather_temp_c", "weather_humidity_pct", "elevation_ascended_m",
          "elevation_descended_m", "avg_mets", "pool_length_m",
          "swim_location", "max_speed_kmh")


def apply(store: Store, rows: list[dict[str, Any]]) -> tuple[int, int]:
    """Attach metadata to workouts matched on their start instant.

    Only ever fills a NULL. A value already in the row came from the delta path,
    which is closer to the source than a months-old export, and overwriting it
    would quietly walk the record backwards.
    """
    updated = unmatched = 0
    sets = ", ".join(f"{f} = COALESCE({f}, %s)" for f in FIELDS)
    with store.cursor() as cur:
        for row in rows:
            if all(row[f] is None for f in FIELDS):
                continue
            cur.execute(
                f"UPDATE workouts SET {sets} WHERE started_at = %s",
                tuple(row[f] for f in FIELDS) + (row["start"],))
            if cur.rowcount:
                updated += cur.rowcount
            else:
                unmatched += 1
    store.commit()
    return updated, unmatched


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Backfill workout weather, elevation and METs from an export.")
    ap.add_argument("--export", type=Path, required=True)
    ap.add_argument("--report", action="store_true", help="count only, write nothing")
    args = ap.parse_args(argv)

    rows = read_workouts(args.export)
    print(f"{len(rows):,} workouts in the export")
    for f in FIELDS:
        n = sum(1 for r in rows if r[f] is not None)
        if n:
            print(f"  {f:22} {n:,}")
    if args.report:
        return 0

    store = Store(None)
    try:
        updated, unmatched = apply(store, rows)
    finally:
        store.close()
    print(f"enriched {updated:,} workouts; {unmatched:,} had no matching row")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
