"""Per-workout heart-rate series, from whichever store holds them.

The series arrive as `hr-<uuid>.csv` sidecars in the sync inbox, which is why
every renderer that wanted zones needed an `--inbox` path and why the inbox had
to outlive the deltas it shipped with. `ah-migrate` folds them into
`hr_samples`, and this is the seam that lets a renderer stop caring which one it
is reading.

Both providers return the same shape — `(epoch_seconds, bpm)` pairs, sorted —
because that is what `derive.zones.summarize` and `.thirds` consume. Returning
`None` rather than an empty list is deliberate: *no series was recorded* and
*the series is empty* are different facts, and a renderer says something
different about each.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

Series = list[tuple[float, float]]


class HRSeriesSource(Protocol):
    """Somewhere per-workout heart-rate samples can be read from."""

    def series_for(self, uuid: str | None) -> Series | None:
        """Samples for one workout, or None when none were recorded."""
        ...


class InboxSeries:
    """Reads the `hr-<uuid>.csv` sidecars the sync inbox holds."""

    def __init__(self, inbox: Path) -> None:
        self.inbox = inbox

    def series_for(self, uuid: str | None) -> Series | None:
        """Samples from `hr-<uuid>.csv`, or None when the sidecar is absent."""
        if not uuid:
            return None
        path = self.inbox / f"hr-{uuid}.csv"
        if not path.exists():
            return None
        out: Series = []
        for line in path.read_text().splitlines()[1:]:
            ts, bpm = line.split(",")
            out.append(
                (datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp(), float(bpm))
            )
        return out


class StoreSeries:
    """Reads `hr_samples` from the Postgres store.

    Rounds each sample's bpm the same way the migration stored it, so the two
    providers agree to the digit — the point of the seam is that a renderer
    cannot tell which one it was handed.
    """

    def __init__(self, store: object) -> None:
        self.store = store

    def series_for(self, uuid: str | None) -> Series | None:
        """Samples for the workout with this HealthKit uuid, or None."""
        if not uuid:
            return None
        with self.store.cursor() as cur:
            cur.execute(
                """SELECT s.t, s.bpm
                     FROM hr_samples s
                     JOIN workouts w ON w.id = s.workout_id
                    WHERE w.uuid = %s
                 ORDER BY s.t""",
                (uuid,),
            )
            rows = cur.fetchall()
        if not rows:
            return None
        return [(r["t"].timestamp(), float(r["bpm"])) for r in rows]


class FallbackSeries:
    """Try each source in turn and take the first that has a series.

    Postgres first, inbox second. That ordering means a workout `ah-pgsync` has
    not reached yet still renders from its sidecar rather than losing its zones
    — the failure that kept the reader default on the inbox until now. It also
    means the answer is never *worse* than the inbox-only behaviour it replaces.
    """

    def __init__(self, *sources: HRSeriesSource) -> None:
        self.sources = sources

    def series_for(self, uuid: str | None) -> Series | None:
        """First non-None series among the sources, or None if none has one.

        A source that *raises* is treated as one that has nothing: the Pi can
        drop a connection mid-render, and one failed query leaves psycopg's
        connection in `InFailedSqlTransaction` so every later read raises too.
        Guarding construction alone would leave the "never worse than the
        inbox" promise false for exactly the case it exists to cover.
        """
        for source in self.sources:
            try:
                series = source.series_for(uuid)
            except Exception as exc:
                print(f"  ! {type(source).__name__} failed ({exc.__class__.__name__}); "
                      f"trying the next source")
                continue
            if series is not None:
                return series
        return None


def default_source(inbox: Path) -> HRSeriesSource:
    """The provider a command should use unless told otherwise.

    Postgres-backed when a DSN is configured, with the inbox behind it; the
    inbox alone when it is not. Connecting lazily here rather than at import
    keeps every command that never reads a series free of the dependency.
    """
    import os

    if not os.environ.get("APPLE_HEALTH_DSN"):
        return InboxSeries(inbox)
    try:
        from ..store import Store

        return FallbackSeries(StoreSeries(Store()), InboxSeries(inbox))
    except Exception as exc:  # unreachable server, missing driver, bad password
        print(f"  ! Postgres unavailable ({exc.__class__.__name__}); reading HR series "
              f"from the inbox")
        return InboxSeries(inbox)
