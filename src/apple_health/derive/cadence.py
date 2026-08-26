"""Running cadence, derived from speed and stride length.

Apple Health does not export cadence as a quantity type. It is recovered from
two types it does export::

    cadence [spm] = speed [km/h] * 1000 / 60 / stride_length [m]

The SQLite schema stored the result as a synthetic `RunningCadence` row in
`daily_metrics`, which is why those rows carry an `avg` and no `sum`: there is
no sum, because a ratio of two daily averages is not the mean of anything that
was measured.

Under ADR-006 corollary (d) that row should not exist — derivations are
computed on read. The Postgres schema keeps `daily_metrics.sum` NOT NULL, so it
refuses to store one, and this module holds the formula instead. Validated
against logged footing cadence (~162–165 spm).
"""

from __future__ import annotations

SPEED_TYPE = "RunningSpeed"
STRIDE_TYPE = "RunningStrideLength"


def cadence_spm(speed_kmh: float, stride_m: float) -> float | None:
    """Steps per minute for a day's mean speed and stride length.

    Args:
        speed_kmh: Mean running speed for the day, km/h.
        stride_m: Mean stride length for the day, metres.

    Returns:
        Cadence in steps per minute, rounded to one decimal as the stored
        version was, or None when stride length is missing or non-positive —
        the join that produced these rows required `stride > 0`.
    """
    if not stride_m or stride_m <= 0 or speed_kmh is None:
        return None
    return round(speed_kmh * 1000.0 / 60.0 / stride_m, 1)


DAILY_SQL = """
SELECT s.day,
       round((s.sum / s.count) * 1000.0 / 60.0 / (l.sum / l.count))::numeric(6,1) AS spm,
       s.count AS n
  FROM daily_metrics s
  JOIN daily_metrics l ON l.day = s.day AND l.type = %(stride)s
 WHERE s.type = %(speed)s
   AND l.sum > 0 AND s.sum IS NOT NULL
 ORDER BY s.day
"""
"""Postgres query yielding the same series the stored rows held.

Kept beside the scalar so the two cannot drift: this is the read-time
replacement for the `RunningCadence` rows, not a second definition of them.
"""
