"""The heart-rate zone model, and the summaries derived from it.

**This is the single definition.** It previously existed three times — as this
constant in the race tool, as a SQL `CASE` in the HTML report, and again as
`Zones.swift` on the phone. ADR-005 flagged the duplication; ADR-006 requires
one site, and this is it. The Swift copy remains, deliberately, because it runs
on-device; the parity check is what keeps the two honest.

Zone distributions are computed here and never stored, because a stored
percentage is only valid for the model that produced it.

**`ZONES` is the model.** One set of bands for the whole record. HealthKit
cannot report what the watch was configured with — there is no read-back API —
so this constant is not a default standing in for something better; it is the
only statement of the bands that exists.

The cost, stated plainly: if the boundaries on the watch ever change, every
figure computed here becomes wrong for sessions before the change, with nothing
to signal it. `hr_zone_models` exists in the schema for that day and is
deliberately unread until then — reporting one set of bands while computing with
another would be worse than having one set. See `docs/adr-006-sinks-are-plugins.md`.
"""

from __future__ import annotations

import math

# (label, lo, hi) in bpm, both bounds inclusive. Z5 is open-ended: a sentinel
# upper bound would classify a corrupt 1200 bpm sample as Z1 via the fallback.
ZONES: list[tuple[str, float, float]] = [
    # Z1's label reads "<135" because the band includes 134. The old "<134"
    # was off by one against its own bounds, and disagreed with the HTML
    # report, which had it right.
    ("Z1 <135",    0,   134),
    ("Z2 135-159", 135, 159),
    ("Z3 160-169", 160, 169),
    ("Z4 170-177", 170, 177),
    ("Z5 >=178",   178, math.inf),
]


def zone_of(hr: float) -> str:
    """Return the zone label a heart rate falls in.

    Z5 is open-ended, so the only unmatched input is a negative rate. That
    falls back to Z1 rather than raising, matching the behaviour every
    existing archived race file was rendered with.

    Args:
        hr: Heart rate in bpm.

    Returns:
        The matching zone label.
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


def zone_durations(vals: list[tuple[float, float]]) -> dict[str, float] | None:
    """Seconds spent in each zone, from a timestamped series.

    Percentages answer "how was this session distributed"; durations answer
    "did I get my twenty minutes at threshold", which is the question a training
    plan actually asks. Claude's own training journal records both, and only the
    percentages have ever come from this pipeline — the durations were typed in
    by hand off the watch.

    Each sample is credited with the gap to the *next* one, so an irregular
    series is measured rather than assumed. The final sample is credited with
    the median gap, since there is no next one to measure against. Gaps beyond
    ten times the median are treated as pauses and credited the median instead:
    a stop at the side of the pool is not forty minutes in Z1.

    Args:
        vals: `(epoch_seconds, bpm)` pairs, sorted internally.

    Returns:
        Zone label → seconds, or None for an empty series.
    """
    if not vals:
        return None
    vals = sorted(vals)
    if len(vals) == 1:
        return {z[0]: 0.0 for z in ZONES}

    gaps = [b[0] - a[0] for a, b in zip(vals, vals[1:])]
    ordered = sorted(gaps)
    median = ordered[len(ordered) // 2] or 1.0
    cap = median * 10

    out = {z[0]: 0.0 for z in ZONES}
    for (_, bpm), gap in zip(vals, gaps + [median]):
        out[zone_of(bpm)] += median if gap > cap else gap
    return out
