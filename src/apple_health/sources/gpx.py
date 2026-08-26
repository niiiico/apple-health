"""Summarise Apple Health workout-route GPX files.

Each GPX file holds a single track (one workout) with per-second track points
carrying lon/lat/ele/time. Storing every point for 1300+ files is unnecessary
for headline stats, so each file is reduced to one summary row (distance,
duration, elevation gain, bounding box, average speed).

Parsing is done with ``iterparse`` so a single large route does not load wholly
into memory. Distance uses the haversine formula on consecutive points.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_GPX_NS = "{http://www.topografix.com/GPX/1/1}"
_EARTH_R_KM = 6371.0088


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_R_KM * math.asin(math.sqrt(a))


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class RouteSummary:
    filename: str
    start: str | None
    end: str | None
    n_points: int
    distance_km: float
    duration_min: float | None
    elev_gain_m: float
    avg_speed_kmh: float | None
    min_lat: float | None
    min_lon: float | None
    max_lat: float | None
    max_lon: float | None

    def as_row(self) -> tuple:
        return (
            self.filename, self.start, self.end, self.n_points,
            round(self.distance_km, 4),
            round(self.duration_min, 2) if self.duration_min is not None else None,
            round(self.elev_gain_m, 1),
            round(self.avg_speed_kmh, 3) if self.avg_speed_kmh is not None else None,
            self.min_lat, self.min_lon, self.max_lat, self.max_lon,
        )


def summarise_gpx(path: str | Path) -> RouteSummary:
    """Reduce one GPX file to a :class:`RouteSummary`."""
    path = Path(path)
    n = 0
    dist = 0.0
    elev_gain = 0.0
    prev_lat = prev_lon = prev_ele = None
    first_t = last_t = None
    min_lat = min_lon = math.inf
    max_lat = max_lon = -math.inf

    for _event, elem in ET.iterparse(str(path), events=("end",)):
        if elem.tag != f"{_GPX_NS}trkpt":
            continue
        lat = float(elem.get("lat"))
        lon = float(elem.get("lon"))
        n += 1
        min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)
        min_lon, max_lon = min(min_lon, lon), max(max_lon, lon)

        ele_el = elem.find(f"{_GPX_NS}ele")
        ele = float(ele_el.text) if ele_el is not None and ele_el.text else None
        t_el = elem.find(f"{_GPX_NS}time")
        t = _parse_time(t_el.text) if t_el is not None else None
        if t is not None:
            if first_t is None:
                first_t = t
            last_t = t

        if prev_lat is not None:
            dist += _haversine_km(prev_lat, prev_lon, lat, lon)
            if ele is not None and prev_ele is not None and ele > prev_ele:
                elev_gain += ele - prev_ele
        prev_lat, prev_lon, prev_ele = lat, lon, ele
        elem.clear()

    duration_min = None
    avg_speed = None
    if first_t and last_t and last_t > first_t:
        duration_min = (last_t - first_t).total_seconds() / 60.0
        if duration_min > 0:
            avg_speed = dist / (duration_min / 60.0)

    return RouteSummary(
        filename=path.name,
        start=first_t.isoformat() if first_t else None,
        end=last_t.isoformat() if last_t else None,
        n_points=n,
        distance_km=dist,
        duration_min=duration_min,
        elev_gain_m=elev_gain,
        avg_speed_kmh=avg_speed,
        min_lat=None if min_lat is math.inf else min_lat,
        min_lon=None if min_lon is math.inf else min_lon,
        max_lat=None if max_lat is -math.inf else max_lat,
        max_lon=None if max_lon is -math.inf else max_lon,
    )


def parse_routes(routes_dir: str | Path, progress_every: int = 200) -> list[RouteSummary]:
    """Summarise every ``*.gpx`` in a directory."""
    routes_dir = Path(routes_dir)
    files = sorted(routes_dir.glob("*.gpx"))
    out: list[RouteSummary] = []
    for i, f in enumerate(files, 1):
        try:
            out.append(summarise_gpx(f))
        except ET.ParseError as exc:
            print(f"  ! skipped {f.name}: {exc}")
        if progress_every and i % progress_every == 0:
            print(f"  …{i}/{len(files)} routes")
    return out


def write(conn, summaries: list[RouteSummary]) -> None:
    """Insert route summaries into an initialised DB connection."""
    conn.executemany(
        "INSERT OR REPLACE INTO routes "
        "(filename, start, end, n_points, distance_km, duration_min, elev_gain_m, "
        " avg_speed_kmh, min_lat, min_lon, max_lat, max_lon) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [s.as_row() for s in summaries],
    )
    conn.commit()
