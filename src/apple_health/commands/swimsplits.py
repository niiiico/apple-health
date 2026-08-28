"""``ah-swim`` — recover per-length swim splits from the raw export.

HealthKit records swimming as one ``DistanceSwimming`` sample per length, each
carrying a start *and* an end. That is finer than lap events: the swim time for
a length is ``end - start``, the rest before the next is the gap between them,
and a benchmark 200 is any eight consecutive 25 m lengths.

None of that ever reached the database. The delta path folds dense quantities
into daily buckets on the phone, and ``DistanceSwimming`` was not among the
types it observed at all, so swim distance stopped at the 2026-06-29 export and
the per-length detail was never carried by anything.

This recovers it for every swim the raw export covers, which is the whole
archive up to that date. Swims after it need the ``swim-<uuid>.csv`` sidecar the
app now writes — see ``docs/delta-contract.md``.

Usage::

    uv run ah-swim --export /path/export.xml --report          # print splits
    uv run ah-swim --export /path/export.xml --load            # into `laps`

``--load`` is idempotent: lengths are keyed by ``(workout_id, idx)`` and
re-running replaces them.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..store import Store

# Parsed by regex rather than an XML parser: the export is several gigabytes and
# a streaming line scan is an order of magnitude faster than iterparse for a
# single attribute-only record type. `health_export.py` uses iterparse where the
# structure actually matters.
_SAMPLE = re.compile(
    r'type="HKQuantityTypeIdentifierDistanceSwimming".*?'
    r'startDate="([^"]+)" endDate="([^"]+)" value="([^"]+)"')
_APPLE = "%Y-%m-%d %H:%M:%S %z"


@dataclass(frozen=True, slots=True)
class Length:
    """One length of the pool."""

    start: datetime
    end: datetime
    metres: float

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def read_lengths(export: Path) -> list[Length]:
    """Every swim length in the export, in time order."""
    out: list[Length] = []
    with open(export, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "DistanceSwimming" not in line:
                continue
            m = _SAMPLE.search(line)
            if not m:
                continue
            try:
                start = datetime.strptime(m.group(1), _APPLE)
                end = datetime.strptime(m.group(2), _APPLE)
                metres = float(m.group(3))
            except ValueError:
                continue
            if end <= start or metres <= 0:
                # A length with no duration would divide by zero in every pace
                # computed downstream; one with no distance is not a length.
                continue
            out.append(Length(start, end, metres))
    out.sort(key=lambda l: l.start)
    return out


@dataclass(frozen=True, slots=True)
class Window:
    """A candidate benchmark distance, and how honest it is.

    `rest` is the load-bearing field. A 200 measured wall to wall with fifty
    seconds of rest inside it is not a benchmark 200, and a report giving only
    `elapsed` invites reading it as one. Both are kept so a continuous swim can
    be told from a set.
    """

    elapsed: float
    rest: float
    index: int

    @property
    def continuous(self) -> bool:
        """Near-unbroken: under five seconds of rest across the whole distance."""
        return self.rest < 5.0


def best_window(lengths: list[Length], metres: float = 200.0) -> Window | None:
    """Fastest `metres` over consecutive lengths, wall to wall.

    Wall to wall — *including* the rests inside it — because that is what a
    benchmark measures. Summing only the swim times would report eight lengths
    with thirty seconds rest between each as a 200, which is exactly the
    flattering error this project exists to avoid. The rest inside the window is
    reported alongside, so the difference is visible rather than assumed.
    """
    best: Window | None = None
    for i in range(len(lengths)):
        total = 0.0
        for j in range(i, len(lengths)):
            total += lengths[j].metres
            if total < metres:
                continue
            if total > metres:
                break                       # cannot land exactly; try next start
            elapsed = (lengths[j].end - lengths[i].start).total_seconds()
            swum = sum(l.seconds for l in lengths[i:j + 1])
            if best is None or elapsed < best.elapsed:
                best = Window(elapsed, elapsed - swum, i)
            break
    return best


def _mmss(seconds: float) -> str:
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def group_by_session(lengths: list[Length], gap_minutes: float = 30.0) -> list[list[Length]]:
    """Split the stream into sessions on long gaps.

    The export has no workout id on these samples, so sessions are inferred.
    Thirty minutes is far longer than any rest inside a set and far shorter than
    the interval between swims.
    """
    sessions: list[list[Length]] = []
    for length in lengths:
        if sessions and (length.start - sessions[-1][-1].end).total_seconds() < gap_minutes * 60:
            sessions[-1].append(length)
        else:
            sessions.append([length])
    return sessions


def report(sessions: list[list[Length]], limit: int) -> None:
    """Print each session's totals and its best continuous 200."""
    for session in sessions[-limit:]:
        total = sum(l.metres for l in session)
        swum = sum(l.seconds for l in session)
        span = (session[-1].end - session[0].start).total_seconds()
        best = best_window(session)
        line = (f"{session[0].start:%Y-%m-%d %H:%M}  {len(session):3} lengths  "
                f"{total:6.0f} m  swum {_mmss(swum)}  elapsed {_mmss(span)}")
        if best:
            pace = best.elapsed / 2
            line += (f"  best 200 {_mmss(best.elapsed)} ({_mmss(pace)}/100m)"
                     + ("  continu" if best.continuous
                        else f"  [dont {best.rest:.0f}s repos]"))
        else:
            line += "  best 200 —"
        print(line)


def load(store: Store, sessions: list[list[Length]]) -> int:
    """Attach lengths to the workouts they fall inside, as `laps` rows.

    Matched on time rather than uuid, because the export's samples carry no
    workout reference. A length is attributed to a swim whose window contains
    its start; anything unmatched is reported rather than dropped silently.
    """
    attached = unmatched = 0
    with store.cursor() as cur:
        for session in sessions:
            # Matched on the session's *start* falling inside the workout, not
            # on full containment: a length ending a second past the workout's
            # recorded end is normal, and requiring containment silently
            # dropped whole sessions.
            #
            # No activity filter either. A race swim is one leg of a
            # `SwimBikeRun` workout, not a `Swimming` one — filtering on
            # Swimming discarded exactly the swims that matter most here, the
            # Kujukuri and Shichigahama legs.
            #
            # Shortest match wins, so a dedicated swim beats a triathlon that
            # happens to span the same minute.
            cur.execute(
                """SELECT id FROM workouts
                    WHERE started_at <= %s AND ended_at >= %s
                 ORDER BY ended_at - started_at
                    LIMIT 1""",
                (session[0].start, session[0].start))
            row = cur.fetchone()
            if row is None:
                unmatched += 1
                print(f"  ! no swim workout covers {session[0].start:%Y-%m-%d %H:%M} "
                      f"({len(session)} lengths) — left unattached")
                continue
            cur.executemany(
                """INSERT INTO laps (workout_id, idx, started_at, duration_s, distance_m)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (workout_id, idx) DO UPDATE SET
                       started_at = excluded.started_at,
                       duration_s = excluded.duration_s,
                       distance_m = excluded.distance_m""",
                [(row["id"], i, l.start, l.seconds, l.metres)
                 for i, l in enumerate(session, start=1)])
            attached += len(session)
    store.commit()
    if unmatched:
        print(f"  {unmatched} session(s) matched no workout")
    return attached


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Recover per-length swim splits from a raw Apple Health export.")
    ap.add_argument("--export", type=Path, required=True, help="path to export.xml")
    ap.add_argument("--report", action="store_true", help="print splits, store nothing")
    ap.add_argument("--load", action="store_true", help="write lengths into `laps`")
    ap.add_argument("--limit", type=int, default=20, help="sessions to print")
    args = ap.parse_args(argv)

    if not args.report and not args.load:
        ap.error("choose --report or --load")

    lengths = read_lengths(args.export)
    sessions = group_by_session(lengths)
    print(f"{len(lengths):,} lengths across {len(sessions)} sessions")

    if args.report:
        report(sessions, args.limit)
    if args.load:
        store = Store(None)
        try:
            print(f"attached {load(store, sessions):,} lengths")
        finally:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
