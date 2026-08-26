"""The heart-rate zone model, and the summaries derived from it.

**This is the single definition.** It previously existed three times — as this
constant in the race tool, as a SQL `CASE` in the HTML report, and again as
`Zones.swift` on the phone. ADR-005 flagged the duplication; ADR-006 requires
one site, and this is it. The Swift copy remains, deliberately, because it runs
on-device; the parity check is what keeps the two honest.

Zone distributions are computed here and never stored. Boundaries are defined on
the watch and change over time, so a persisted percentage is only valid for the
model that produced it — see `store.hr_zone_models` and
`docs/adr-006-sinks-are-plugins.md`.

The literal below is the interim model: it predates dated models and applies to
the whole record. Once `hr_zone_models` is populated, callers resolve boundaries
through `Store.zone_model_at()` and this constant becomes the fallback for
periods with no recorded model.
"""

from __future__ import annotations

# (label, lo, hi) in bpm, inclusive. Z5's upper bound is a sentinel, not a limit.
ZONES: list[tuple[str, int, int]] = [
    ("Z1 <134",    0,   134),
    ("Z2 135-159", 135, 159),
    ("Z3 160-169", 160, 169),
    ("Z4 170-177", 170, 177),
    ("Z5 >=178",   178, 999),
]


def zone_of(hr: float) -> str:
    """Return the zone label a heart rate falls in.

    Args:
        hr: Heart rate in bpm.

    Returns:
        The matching zone label; Z1 for anything below the first band, which
        only happens for implausible readings.
    """
    for name, lo, hi in ZONES:
        if lo <= hr <= hi:
            return name
    return ZONES[0][0]


def summarize(vals: list[tuple[object, float]]) -> dict[str, object] | None:
    """Aggregate a heart-rate series into counts, extremes and zone shares.

    Args:
        vals: `(timestamp, bpm)` pairs. Timestamps are ignored here; they matter
            only to `thirds`, which needs the ordering.

    Returns:
        A dict with `n`, `avg`, `min`, `max` and `zones` (label → percent), or
        None when the series is empty — absent data is a value, not a silently
        omitted section.
    """
    hrs = [v for _, v in vals]
    if not hrs:
        return None
    n = len(hrs)
    zc = {z[0]: 0 for z in ZONES}
    for h in hrs:
        zc[zone_of(h)] += 1
    return {
        "n": n, "avg": sum(hrs) / n, "min": min(hrs), "max": max(hrs),
        "zones": {z: 100.0 * c / n for z, c in zc.items()},
    }


def thirds(vals: list[tuple[object, float]]) -> list[tuple[str, float, float]]:
    """Split a series into equal thirds and report mean and peak of each.

    Cardiac drift shows up as a rising mean across the thirds — the reason this
    is reported per-session rather than as a single average.

    Args:
        vals: `(timestamp, bpm)` pairs, sorted internally by timestamp.

    Returns:
        `(label, mean, max)` per third, or an empty list when the series is too
        short (<120 samples) for the split to mean anything.
    """
    vals = sorted(vals)
    n = len(vals)
    if n < 120:
        return []
    out = []
    for i, label in enumerate(["1/3", "2/3", "3/3"]):
        chunk = vals[i * (n // 3):(i + 1) * (n // 3)] if i < 2 else vals[2 * (n // 3):]
        hrs = [v for _, v in chunk]
        if hrs:
            out.append((label, sum(hrs) / len(hrs), max(hrs)))
    return out
