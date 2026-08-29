"""``ah-routes`` — load GPX track points into ``route_points``.

ADR-007 step 2, and the step with a deadline: the GPX files live on the Mac's
disk, and ADR-007 removes the Mac from the path. Once it goes, anything still
only on that disk is gone with it.

``routes`` has held a summary since ADR-001 — distance, duration, a bounding
box — which is enough to say a ride happened somewhere and nothing else. A map
and an elevation profile need the points.

Two sources, and they do not overlap:

- the sync inbox, ``route-<uuid>.gpx``, which name their workout directly;
- the full export's ``workout-routes/``, which do not, and are matched on time.

Usage::

    uv run ah-routes --inbox PATH --routes PATH [--limit N] [--force]

Idempotent: points are keyed ``(route_id, idx)`` and a route already loaded is
skipped unless ``--force``.
"""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..store import Store

_NS = "{http://www.topografix.com/GPX/1/1}"
DEFAULT_INBOX = Path("/Volumes/nicolas-data/HealthData/healthsync-inbox")


@dataclass(frozen=True, slots=True)
class Point:
    """One track point."""

    t: datetime | None
    lat: float
    lon: float
    ele: float | None


def read_gpx(path: Path) -> list[Point]:
    """Track points from a GPX file, in file order.

    Streamed rather than parsed whole: a long ride is tens of thousands of
    points and there are 1,300 files.
    """
    points: list[Point] = []
    for _ev, el in ET.iterparse(str(path), events=("end",)):
        if el.tag != f"{_NS}trkpt":
            continue
        lat, lon = el.get("lat"), el.get("lon")
        if lat is None or lon is None:
            el.clear()
            continue
        t_el = el.find(f"{_NS}time")
        e_el = el.find(f"{_NS}ele")
        when = None
        if t_el is not None and t_el.text:
            try:
                when = datetime.fromisoformat(t_el.text.replace("Z", "+00:00"))
            except ValueError:
                when = None
        ele = None
        if e_el is not None and e_el.text:
            try:
                ele = float(e_el.text)
            except ValueError:
                ele = None
        points.append(Point(when, float(lat), float(lon), ele))
        el.clear()
    return points


def _route_id_for(cur, path: Path, points: list[Point]) -> int | None:
    """The `routes` row this file belongs to, by filename then by time.

    Inbox files are named for their workout uuid and match directly. Export
    files carry only a timestamp in the name, so they are matched on the first
    point's instant falling inside a route's window — which is exact enough,
    since two routes never overlap.
    """
    cur.execute("SELECT id FROM routes WHERE filename = %s", (path.name,))
    row = cur.fetchone()
    if row:
        return row["id"]

    if path.name.startswith("route-") and path.stem[6:]:
        cur.execute(
            """SELECT r.id FROM routes r JOIN workouts w ON w.id = r.workout_id
                WHERE w.uuid = %s""", (path.stem[6:],))
        row = cur.fetchone()
        if row:
            return row["id"]

    if not points or points[0].t is None:
        return None
    cur.execute(
        """SELECT id FROM routes
            WHERE started_at <= %s AND ended_at >= %s
         ORDER BY ended_at - started_at LIMIT 1""",
        (points[0].t, points[0].t))
    row = cur.fetchone()
    return row["id"] if row else None


def load_file(cur, path: Path, force: bool) -> tuple[str, int]:
    """Load one GPX. Returns (outcome, points written)."""
    points = read_gpx(path)
    if not points:
        return ("empty", 0)

    route_id = _route_id_for(cur, path, points)
    if route_id is None:
        # Reported, never silently dropped: an unmatched track is a ride whose
        # map will simply never appear, and a count of them is the only way to
        # notice.
        return ("unmatched", 0)

    if not force:
        cur.execute("SELECT 1 FROM route_points WHERE route_id = %s LIMIT 1",
                    (route_id,))
        if cur.fetchone():
            return ("present", 0)

    cur.executemany(
        """INSERT INTO route_points (route_id, idx, t, lat, lon, ele_m)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (route_id, idx) DO UPDATE SET
               t = excluded.t, lat = excluded.lat, lon = excluded.lon,
               ele_m = excluded.ele_m""",
        [(route_id, i, p.t, p.lat, p.lon, p.ele)
         for i, p in enumerate(points, start=1)])
    return ("loaded", len(points))


def link_routes(store: Store) -> int:
    """Attach unlinked routes to the workout they were recorded during.

    `routes.workout_id` is NULL for every row loaded from the full export: the
    GPX files carry no workout reference and the parser never matched them.
    Nothing complained, because a route with no workout is only visible as a map
    that never appears — the points load, the page renders, and the absence
    looks like a workout that had no route.

    Matched on overlap rather than containment: the watch starts the route a
    moment after the workout and stops it a moment before, or the reverse, and
    requiring one to contain the other loses most of them.
    """
    with store.cursor() as cur:
        cur.execute(
            """UPDATE routes r SET workout_id = w.id
                 FROM workouts w
                WHERE r.workout_id IS NULL
                  AND r.started_at IS NOT NULL
                  AND w.started_at < r.ended_at
                  AND w.ended_at   > r.started_at
                  AND w.id = (
                      SELECT w2.id FROM workouts w2
                       WHERE w2.started_at < r.ended_at
                         AND w2.ended_at   > r.started_at
                    ORDER BY GREATEST(w2.started_at, r.started_at)
                             - LEAST(w2.ended_at, r.ended_at)
                       LIMIT 1)""")
        linked = cur.rowcount
    store.commit()
    return linked


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Load GPX track points into route_points.")
    ap.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    ap.add_argument("--routes", type=Path, default=None,
                    help="the full export's workout-routes/ directory")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="reload routes already loaded")
    ap.add_argument("--link-only", action="store_true",
                    help="only attach routes to workouts; load no points")
    args = ap.parse_args(argv)

    if args.link_only:
        store = Store(None)
        try:
            print(f"linked {link_routes(store):,} route(s)")
        finally:
            store.close()
        return 0

    files: list[Path] = []
    if args.inbox and args.inbox.is_dir():
        files += sorted(args.inbox.glob("route-*.gpx"))
    if args.routes and args.routes.is_dir():
        files += sorted(args.routes.glob("*.gpx"))
    if args.limit:
        files = files[: args.limit]
    print(f"{len(files)} GPX file(s)")

    tally: dict[str, int] = {}
    total = 0
    store = Store(None)
    try:
        with store.cursor() as cur:
            for n, path in enumerate(files, start=1):
                try:
                    outcome, written = load_file(cur, path, args.force)
                except ET.ParseError as exc:
                    print(f"  ! {path.name}: unreadable ({exc})")
                    outcome, written = "unreadable", 0
                tally[outcome] = tally.get(outcome, 0) + 1
                total += written
                if n % 200 == 0:
                    print(f"  {n}/{len(files)}…")
        store.commit()
    finally:
        store.close()

    store = Store(None)
    try:
        linked = link_routes(store)
    finally:
        store.close()
    if linked:
        print(f"linked {linked:,} route(s) to their workout")

    print(f"{total:,} points written")
    for outcome, n in sorted(tally.items()):
        print(f"  {outcome:11} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
