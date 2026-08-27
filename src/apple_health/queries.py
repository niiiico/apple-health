"""The read surface the advisor, Claude Code and any future MCP sink share.

Five functions, deliberately few: a sprawling tool set degrades selection, and
a typed surface is what makes the two guarantees below enforceable — raw SQL
would let a caller bypass both.

**Every response carries its own basis.** A session list without its coverage
boundary, or a zone percentage without the model that produced it, is the shape
of answer that reads as complete and is not. One of those once put "vélo = 0"
into a training plan for a month; see `docs/adr-006-sinks-are-plugins.md`.

**No thresholds.** Brevity belongs to whatever renders the answer, not to the
layer that decides what exists.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path

from .config import repo_root
from .derive.zones import ZONES, summarize, thirds, zone_durations
from .sources.hr_series import StoreSeries
from .store import Store

BUCKETS = {"day": "day", "week": "week", "month": "month"}


def _zone_basis() -> dict:
    """The zone model every figure in this module is computed with.

    One fixed model, defined in `derive.zones`. Reported rather than assumed,
    so a caller reading "Z3 4:52" can see which bands produced it — the number
    is meaningless without them.
    """
    return {
        "source": "fixed",
        "note": "One model for the whole record, defined in derive/zones.py. "
                "HealthKit cannot report what the watch was set to, so if the "
                "bands change these become wrong for sessions before the change.",
        "boundaries": {label: f"{lo}-{hi}" for label, lo, hi in ZONES},
    }


def _coverage(store: Store, through: date | None, tz: tzinfo | None) -> dict:
    cov = store.coverage(through, tz)
    out: dict = {"observed_through": cov.observed_through.isoformat()
                 if cov.observed_through else None}
    if cov.requested_through:
        out["requested_through"] = cov.requested_through.isoformat()
    if cov.warning:
        out["warning"] = cov.warning
    return out


def context(store: Store, tz: tzinfo | None = None) -> dict:
    """Orient: how far the record extends, which bands apply, the notes, the goals.

    Worth calling before drawing any conclusion from the others — it is the one
    place that says outright what is *not* known.
    """
    with store.cursor() as cur:
        cur.execute("SELECT starts_on, ends_on, note FROM period_notes"
                    " ORDER BY starts_on DESC LIMIT 50")
        notes = [{"from": r["starts_on"].isoformat(),
                  "to": r["ends_on"].isoformat() if r["ends_on"] else None,
                  "note": r["note"]} for r in cur.fetchall()]
        cur.execute("SELECT min(started_at) lo, max(started_at) hi, count(*) n FROM workouts")
        span = cur.fetchone()
        cur.execute("SELECT id, goal, target_date FROM goals WHERE archived_at IS NULL"
                    " ORDER BY target_date NULLS LAST, created_at")
        goals = [{"id": r["id"], "goal": r["goal"],
                  "target_date": r["target_date"].isoformat() if r["target_date"] else None}
                 for r in cur.fetchall()]

    return {
        "coverage": _coverage(store, None, tz),
        "record": {
            "workouts": span["n"],
            "earliest": span["lo"].isoformat() if span["lo"] else None,
            "latest": span["hi"].isoformat() if span["hi"] else None,
        },
        "zone_model": _zone_basis(),
        "period_notes": notes,
        # In the athlete's own words. Empty means none recorded — which is not
        # the same as "no goals", and advice given without one should say so
        # rather than inventing a plausible objective to advise towards.
        "goals": goals,
    }


def list_sessions(store: Store, start: date, end: date,
                  activity: str | None = None, tz: tzinfo | None = None) -> dict:
    """Every session in the window. No distance floors, no filtering by interest.

    Rows are compact so a six-week window stays cheap; `session_detail` is a
    second call on the one that matters.
    """
    sql = """SELECT w.id, w.uuid, w.activity, w.started_at, w.tz_name, w.duration_min,
                    w.distance_km, w.energy_kcal, w.avg_hr, w.max_hr, w.indoor,
                    (SELECT count(*) > 0 FROM hr_samples s WHERE s.workout_id = w.id) has_hr_series,
                    (SELECT count(*) > 0 FROM laps l WHERE l.workout_id = w.id) has_laps,
                    n.note
               FROM workouts w
          LEFT JOIN session_notes n ON n.workout_id = w.id
              WHERE w.started_at >= %s AND w.started_at < %s"""
    params: list = [start, end + timedelta(days=1)]
    if activity:
        sql += " AND w.activity = %s"
        params.append(activity)
    sql += " ORDER BY w.started_at"

    with store.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return {
        "coverage": _coverage(store, end, tz),
        "sessions": [
            {"id": r["id"], "date": r["started_at"].isoformat()[:10],
             "activity": r["activity"], "distance_km": r["distance_km"],
             "duration_min": r["duration_min"], "avg_hr": r["avg_hr"],
             "max_hr": r["max_hr"], "energy_kcal": r["energy_kcal"],
             "indoor": r["indoor"], "tz": r["tz_name"],
             "has_hr_series": r["has_hr_series"], "has_laps": r["has_laps"],
             "note": r["note"]}
            for r in rows
        ],
    }


def session_detail(store: Store, workout_id: int, tz: tzinfo | None = None) -> dict:
    """One session in full: stats, zone shares *and* durations, drift, laps, note.

    `hr` is None when no series was recorded — a different fact from a series
    that recorded nothing, and the caller should say something different about
    each rather than inferring from avg/max.
    """
    with store.cursor() as cur:
        cur.execute(
            """SELECT w.*, n.note FROM workouts w
          LEFT JOIN session_notes n ON n.workout_id = w.id
              WHERE w.id = %s""", (workout_id,))
        w = cur.fetchone()
        if not w:
            return {"error": f"no workout with id {workout_id}"}
        cur.execute("SELECT idx, started_at, duration_s, distance_m FROM laps"
                    " WHERE workout_id = %s ORDER BY idx", (workout_id,))
        laps = [dict(r) for r in cur.fetchall()]

    day = w["started_at"].date()
    series = StoreSeries(store).series_for(w["uuid"])

    hr = None
    if series:
        s = summarize(series)
        durations = zone_durations(series)
        hr = {
            "samples": s["n"], "avg": round(s["avg"], 1),
            "min": s["min"], "max": s["max"],
            "zone_percent": {k: round(v, 1) for k, v in s["zones"].items()},
            "zone_seconds": {k: round(v) for k, v in durations.items()},
            "drift_thirds": [{"third": lab, "avg": round(a, 1), "max": mx}
                             for lab, a, mx in thirds(series)],
        }

    return {
        "coverage": _coverage(store, day, tz),
        "zone_model": _zone_basis(),
        "session": {
            "id": w["id"], "date": day.isoformat(),
            "started_at": w["started_at"].isoformat(), "tz": w["tz_name"],
            "activity": w["activity"], "distance_km": w["distance_km"],
            "duration_min": w["duration_min"], "energy_kcal": w["energy_kcal"],
            "avg_hr": w["avg_hr"], "max_hr": w["max_hr"], "indoor": w["indoor"],
            "note": w["note"],
        },
        "hr": hr,
        "laps": [{"idx": l["idx"], "duration_s": l["duration_s"],
                  "distance_m": l["distance_m"]} for l in laps] or None,
    }


def metric_history(store: Store, metric: str, start: date, end: date,
                   bucket: str = "week", tz: tzinfo | None = None) -> dict:
    """A metric over time, bucketed — the question a rolling window cannot answer.

    Handles both dense daily aggregates (`HeartRate`, `StepCount`,
    `DistanceWalkingRunning`) and sparse records (`RestingHeartRate`, `VO2Max`,
    `BodyMass`, `HeartRateVariabilitySDNN`), picking whichever holds the metric.
    """
    if bucket not in BUCKETS:
        return {"error": f"bucket must be one of {sorted(BUCKETS)}"}

    with store.cursor() as cur:
        # Pick by which source actually *covers* the window, not by which has
        # any row at all. Several types live in both tables, and daily_metrics
        # stops at the last full export because the delta path only writes
        # records — choosing on existence returns an empty range for anything
        # recent, which reads as "you did none of this" rather than "ask
        # elsewhere". Exactly the coverage failure in miniature.
        cur.execute("SELECT max(day) d FROM daily_metrics WHERE type = %s", (metric,))
        dense_last = cur.fetchone()["d"]
        cur.execute("SELECT max(recorded_at) d FROM records WHERE type = %s", (metric,))
        sparse_last = cur.fetchone()["d"]
        sparse_last = sparse_last.date() if sparse_last else None

        if dense_last is None and sparse_last is None:
            return {"coverage": _coverage(store, end, tz), "metric": metric,
                    "error": f"no data of type {metric!r} in either table"}
        if dense_last is None:
            dense = False
        elif sparse_last is None:
            dense = True
        else:
            # Both hold it: take whichever reaches further into the window.
            dense = dense_last >= sparse_last

        source_last = dense_last if dense else sparse_last

        if dense:
            cur.execute(
                f"""SELECT date_trunc('{bucket}', day)::date AS b,
                           sum(count) AS n, sum(sum) AS total,
                           min(min) AS lo, max(max) AS hi
                      FROM daily_metrics
                     WHERE type = %s AND day >= %s AND day <= %s
                  GROUP BY b ORDER BY b""", (metric, start, end))
            points = [{"bucket": r["b"].isoformat(), "samples": int(r["n"]),
                       "total": float(r["total"]) if r["total"] is not None else None,
                       "mean": round(float(r["total"]) / float(r["n"]), 2)
                               if r["n"] and r["total"] is not None else None,
                       "min": r["lo"], "max": r["hi"]} for r in cur.fetchall()]
            source = "daily_metrics"
        else:
            cur.execute(
                f"""SELECT date_trunc('{bucket}', recorded_at)::date AS b,
                           count(*) AS n, avg(value) AS mean,
                           min(value) AS lo, max(value) AS hi
                      FROM records
                     WHERE type = %s AND recorded_at >= %s AND recorded_at < %s
                  GROUP BY b ORDER BY b""", (metric, start, end + timedelta(days=1)))
            points = [{"bucket": r["b"].isoformat(), "samples": int(r["n"]),
                       "mean": round(float(r["mean"]), 2), "min": r["lo"], "max": r["hi"]}
                      for r in cur.fetchall()]
            source = "records"

    out = {
        "coverage": _coverage(store, end, tz),
        "metric": metric, "bucket": bucket, "source": source,
        "source_last": source_last.isoformat() if source_last else None,
        "points": points,
    }
    if source_last and source_last < end:
        out["source_warning"] = (
            f"{source} holds {metric} only through {source_last.isoformat()}; "
            f"anything after that is UNKNOWN, not zero.")
    return out


def race_detail(race: str | None = None) -> dict:
    """Archived per-leg race breakdowns from `data/races/`.

    File-backed rather than stored: these are mined from the raw export by
    `ah-races` and are the durable record ADR-001 keeps outside the database.
    Call with no argument to list what exists.
    """
    outdir = repo_root() / "data" / "races"
    if not outdir.is_dir():
        return {"races": [], "note": f"no archive directory at {outdir}"}
    available = sorted(p.stem for p in outdir.glob("*.md"))
    if race is None:
        return {"races": available}
    path = outdir / f"{race}.md"
    if not path.exists():
        return {"error": f"no archive for {race!r}", "races": available}
    return {"race": race, "content": path.read_text()}
