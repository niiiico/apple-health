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

# Points kept for the page's map and elevation profile.
ROUTE_POINTS = 400

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
        cur.execute("""SELECT born_on, background, philosophy, constraints
                         FROM profile WHERE id = 1""")
        row = cur.fetchone()
        athlete = None
        if row:
            athlete = {k: row[k] for k in
                       ("background", "philosophy", "constraints") if row[k]}
            if row["born_on"]:
                born = row["born_on"]
                today = date.today()
                athlete["born_on"] = born.isoformat()
                athlete["age"] = (today.year - born.year
                                  - ((today.month, today.day) < (born.month, born.day)))
        cur.execute("""SELECT note, learned_at FROM advisor_memory
                        WHERE archived_at IS NULL ORDER BY learned_at DESC LIMIT 40""")
        memory = [{"note": r["note"], "learned_at": r["learned_at"].isoformat()}
                  for r in cur.fetchall()]

        cur.execute("""SELECT session_id, asked_at, question, answer, queries
                         FROM chat_turns WHERE archived_at IS NULL
                     ORDER BY asked_at DESC LIMIT 30""")
        history = [{"session_id": r["session_id"],
                    "asked_at": r["asked_at"].isoformat(),
                    "question": r["question"], "answer": r["answer"],
                    "queries": r["queries"]} for r in cur.fetchall()]
        # The advisor writes slug 'plan'; the athlete's own race plan and log
        # were loaded under their own slugs. Reading only 'plan' meant the page
        # said "no plan" while holding a six-thousand-word race plan that no
        # route in the app could reach.
        # The advisor's own slug first, then anything named like a plan, then
        # newest. Ordering on updated_at alone put the *log* — a retrospective
        # journal — under a heading that says Plan, because both were loaded in
        # the same second and the log landed last.
        cur.execute("""SELECT slug, body, updated_at FROM documents
                        ORDER BY (slug = 'plan') DESC,
                                 (slug LIKE '%%plan%%') DESC,
                                 updated_at DESC""")
        docs = [{"slug": r["slug"], "body": r["body"],
                 "updated_at": r["updated_at"].isoformat()} for r in cur.fetchall()]
        cur.execute("""SELECT target_key, count(*) n FROM revisions
                        WHERE target = 'documents' GROUP BY target_key""")
        doc_versions = {r["target_key"]: r["n"] for r in cur.fetchall()}
        for d in docs:
            d["versions"] = doc_versions.get(d["slug"], 0)
        plan = docs[0] if docs else None
        cur.execute(
            """SELECT g.id, g.goal, g.target_date,
                      (SELECT count(*) FROM revisions v
                        WHERE v.target = 'goals' AND v.target_key = g.id::text) AS versions
                 FROM goals g WHERE g.archived_at IS NULL
             ORDER BY g.target_date NULLS LAST, g.created_at""")
        goals = [{"id": r["id"], "goal": r["goal"], "versions": r["versions"],
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
        # Who he is. Absent for years, and its absence is why advice read like
        # it was written to a stranger.
        "athlete": athlete,
        "memory": memory,
        "plan": plan,
        "documents": docs,
        "chat_history": history,
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


def _swim_summary(laps: list) -> dict | None:
    """Benchmark distances a swim actually contains, wall to wall.

    Each window is measured start-of-first to end-of-last, *including* the rests
    inside it, and reports them: eight lengths with twenty seconds between each
    is a set, not a 200, and a number that hides the difference flatters.
    """
    if not laps or len(laps) < 2:
        return None
    from .commands.swimsplits import Length, best_window

    lengths = [Length(l["started_at"],
                      l["started_at"] + timedelta(seconds=l["duration_s"] or 0),
                      l["distance_m"] or 0.0)
               for l in laps if l["started_at"] and l["distance_m"]]
    if len(lengths) < 2:
        return None

    out: dict = {
        "lengths": len(lengths),
        "distance_m": round(sum(l.metres for l in lengths)),
        "length_m": lengths[0].metres,
        "swum_s": round(sum(l.seconds for l in lengths)),
        "note": "Chaque fenêtre est mesurée bord à bord, repos compris ; "
                "`continuous` est vrai sous 5 s de repos cumulé.",
    }
    for metres in (100.0, 200.0, 400.0):
        w = best_window(lengths, metres)
        if w:
            out[f"best_{int(metres)}m"] = {
                "elapsed_s": round(w.elapsed, 1),
                "rest_s": round(w.rest, 1),
                "continuous": w.continuous,
                "per_100m_s": round(w.elapsed / (metres / 100), 1),
                "from_length": w.index + 1,
            }
    return out


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

    with store.cursor() as cur:
        # Downsampled in SQL rather than in Python: a long ride is twenty
        # thousand points, and a page does not need more than a few hundred to
        # draw a recognisable line. `ntile` keeps the shape by taking one point
        # per bucket rather than the first N, which would draw the first
        # kilometre in detail and stop.
        cur.execute(
            """WITH p AS (
                   SELECT rp.idx, rp.lat, rp.lon, rp.ele_m,
                          ntile(%s) OVER (ORDER BY rp.idx) AS bucket
                     FROM route_points rp
                     JOIN routes r ON r.id = rp.route_id
                    WHERE r.workout_id = %s)
               SELECT DISTINCT ON (bucket) idx, lat, lon, ele_m
                 FROM p ORDER BY bucket, idx""",
            (ROUTE_POINTS, workout_id))
        pts = cur.fetchall()
        route = None
        if pts:
            eles = [p["ele_m"] for p in pts if p["ele_m"] is not None]
            route = {
                "points": [[p["lat"], p["lon"]] for p in pts],
                "elevation": [p["ele_m"] for p in pts],
                "bounds": {"min_lat": min(p["lat"] for p in pts),
                           "max_lat": max(p["lat"] for p in pts),
                           "min_lon": min(p["lon"] for p in pts),
                           "max_lon": max(p["lon"] for p in pts)},
                "ele_min": min(eles) if eles else None,
                "ele_max": max(eles) if eles else None,
                # Sampled, so this is the shape of the climb rather than its
                # exact total; `workouts.elevation_ascended_m` is the watch's
                # own figure and is the one to quote.
                "sampled": True,
            }

        cur.execute(
            """SELECT idx, activity, started_at, ended_at, stats
                 FROM workout_segments WHERE workout_id = %s ORDER BY idx""",
            (workout_id,))
        segments = [
            {"idx": r["idx"], "activity": r["activity"],
             "started_at": r["started_at"].isoformat(),
             "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
             "duration_s": ((r["ended_at"] - r["started_at"]).total_seconds()
                            if r["ended_at"] else None),
             "stats": r["stats"]}
            for r in cur.fetchall()]

        cur.execute(
            """SELECT kind, count(*) n, min(started_at) first_at
                 FROM workout_events WHERE workout_id = %s
             GROUP BY kind ORDER BY count(*) DESC""", (workout_id,))
        event_counts = [{"kind": r["kind"], "count": r["n"],
                         "first_at": r["first_at"].isoformat()}
                        for r in cur.fetchall()]

        cur.execute("SELECT review, model, created_at,"
                    " basis->>'observed_through' AS observed_through"
                    " FROM session_reviews WHERE workout_id = %s", (workout_id,))
        row = cur.fetchone()
        review = ({"review": row["review"], "model": row["model"],
                   "created_at": row["created_at"].isoformat(),
                   "observed_through": row["observed_through"]} if row else None)

    with store.cursor() as cur:
        cur.execute(
            """SELECT body, archived_at, replaced_by FROM revisions
                WHERE target = 'session_notes' AND target_key = %s
             ORDER BY archived_at DESC""", (str(workout_id),))
        history_rows = [{"note": r["body"],
                         "archived_at": r["archived_at"].isoformat(),
                         "replaced_by": r["replaced_by"]} for r in cur.fetchall()]

    return {
        "coverage": _coverage(store, day, tz),
        "zone_model": _zone_basis(),
        "session": {
            "id": w["id"], "date": day.isoformat(),
            "started_at": w["started_at"].isoformat(), "tz": w["tz_name"],
            "activity": w["activity"], "distance_km": w["distance_km"],
            "duration_min": w["duration_min"], "energy_kcal": w["energy_kcal"],
            "avg_hr": w["avg_hr"], "max_hr": w["max_hr"], "indoor": w["indoor"],
            "weather_temp_c": w["weather_temp_c"],
            "weather_humidity_pct": w["weather_humidity_pct"],
            "elevation_ascended_m": w["elevation_ascended_m"],
            "elevation_descended_m": w["elevation_descended_m"],
            "avg_mets": w["avg_mets"], "pool_length_m": w["pool_length_m"],
            "swim_location": w["swim_location"],
            "note": w["note"],
        },
        "hr": hr,
        "route": route,
        # `started_at` is not decoration. Without it the rest between lengths
        # cannot be computed, and the rest is what separates a continuous 200
        # from eight lengths with a breather in each — which is the whole
        # question a swim benchmark asks. A caller given only durations was
        # being asked for arithmetic it had no data for.
        "laps": [{"idx": l["idx"], "started_at": l["started_at"].isoformat(),
                  "duration_s": l["duration_s"], "distance_m": l["distance_m"]}
                 for l in laps] or None,
        # And the benchmark itself, computed rather than left as an exercise:
        # finding the fastest continuous 200 in sixty-six lengths is a scan a
        # model should not be doing by hand in prose.
        "swim": _swim_summary(laps),
        "segments": segments or None,
        # Grouped rather than listed: a run carries hundreds of motionPaused
        # markers and one lap that matters, and a raw list buries the second in
        # the first.
        "events": event_counts or None,
        # The advisor's own reading of this session, if it has written one. Kept
        # apart from `session.note`, which is the athlete's: a model's opinion
        # and the athlete's recollection must never become indistinguishable.
        "review": review,
        # Superseded versions, newest first. A note edited a week later still
        # describes the day it was written about.
        "note_history": history_rows or None,
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


def reviews(store: Store, start: date, end: date) -> dict:
    """Reviews already written for sessions in a window.

    For continuity — so a review does not repeat last week's point as though it
    were new, and so the plan can be built from what was observed rather than
    re-derived.

    **These are prior opinions, not data.** The caller is told so, because a
    conclusion restated often enough starts to read as an established fact, and
    the underlying record is one query away.
    """
    with store.cursor() as cur:
        cur.execute(
            """SELECT w.id, w.activity, w.started_at, r.review, r.created_at,
                      r.basis->>'observed_through' AS observed_through
                 FROM session_reviews r
                 JOIN workouts w ON w.id = r.workout_id
                WHERE w.started_at >= %s AND w.started_at < %s
             ORDER BY w.started_at DESC""",
            (start, end + timedelta(days=1)))
        rows = cur.fetchall()
    return {
        "caveat": "These are opinions previously written, not observations. "
                  "Anything factual in them should be re-queried before it is "
                  "relied on; they may have been wrong, and the record may have "
                  "grown since.",
        "reviews": [
            {"workout_id": r["id"], "activity": r["activity"],
             "started_at": r["started_at"].isoformat(),
             "written_at": r["created_at"].isoformat(),
             "record_covered_through": r["observed_through"],
             "review": r["review"]}
            for r in rows
        ],
    }


def document(store: Store, slug: str | None = None) -> dict:
    """A reference document, or the list of them when called with no slug.

    Where the athlete's own written material lives — a race plan, a training
    block, anything decided rather than measured. Distinct from everything else
    in this module, which reports what a sensor recorded.
    """
    with store.cursor() as cur:
        if slug is None:
            cur.execute("SELECT slug, volatility, updated_at FROM documents"
                        " ORDER BY slug")
            return {"documents": [
                {"slug": r["slug"], "volatility": r["volatility"],
                 "updated_at": r["updated_at"].isoformat()}
                for r in cur.fetchall()]}
        cur.execute("SELECT slug, body, volatility, updated_at FROM documents"
                    " WHERE slug = %s", (slug,))
        row = cur.fetchone()
        if row is None:
            cur.execute("SELECT slug FROM documents ORDER BY slug")
            return {"error": f"no document {slug!r}",
                    "documents": [r["slug"] for r in cur.fetchall()]}
        return {"slug": row["slug"], "volatility": row["volatility"],
                "updated_at": row["updated_at"].isoformat(), "body": row["body"]}


def chat_history(store: Store, limit: int = 40,
                 session_id: str | None = None) -> dict:
    """Past conversations, most recent first.

    Grouped by session so a thread reads as a thread. `session_id` narrows to
    one; without it this is the whole history, newest first.
    """
    with store.cursor() as cur:
        if session_id:
            cur.execute(
                """SELECT id, session_id, asked_at, question, answer, queries, model
                     FROM chat_turns WHERE session_id = %s
                 ORDER BY asked_at""", (session_id,))
        else:
            cur.execute(
                """SELECT session_id, asked_at, question, answer, queries, model
                     FROM chat_turns ORDER BY asked_at DESC LIMIT %s""", (limit,))
        rows = cur.fetchall()
    return {
        "turns": [
            {"id": r.get("id"), "session_id": r["session_id"],
             "asked_at": r["asked_at"].isoformat(),
             "question": r["question"], "answer": r["answer"],
             "queries": r["queries"], "model": r["model"]}
            for r in rows
        ],
    }


def chat_sessions(store: Store, limit: int = 60,
                  archived: bool = False) -> dict:
    """One row per conversation, newest first.

    Grouped in SQL rather than in Python so the list stays cheap as the history
    grows: only the first question and the counts are needed to draw it.

    Archived threads are excluded by default and reachable on request — removed
    from the list is not removed from the record, and the difference should be
    one parameter rather than one deletion.
    """
    where = "archived_at IS NOT NULL" if archived else "archived_at IS NULL"
    with store.cursor() as cur:
        cur.execute(
            f"""SELECT session_id,
                       min(asked_at) AS started_at,
                       max(asked_at) AS last_at,
                       count(*)      AS turns,
                       (array_agg(question ORDER BY asked_at))[1] AS first_question
                  FROM chat_turns
                 WHERE {where}
              GROUP BY session_id
              ORDER BY max(asked_at) DESC
                 LIMIT %s""", (limit,))
        return {"sessions": [
            {"session_id": r["session_id"],
             "started_at": r["started_at"].isoformat(),
             "last_at": r["last_at"].isoformat(),
             "turns": r["turns"],
             "first_question": r["first_question"]}
            for r in cur.fetchall()]}


def revisions(store: Store, target: str, key: str) -> dict:
    """Superseded versions of one note, goal or document, newest first.

    What something used to say is often the interesting part: a goal reworded
    in September says something about the block that the current text does not.
    """
    with store.cursor() as cur:
        cur.execute(
            """SELECT body, archived_at, replaced_by FROM revisions
                WHERE target = %s AND target_key = %s
             ORDER BY archived_at DESC""", (target, str(key)))
        return {"target": target, "key": key, "versions": [
            {"body": r["body"], "archived_at": r["archived_at"].isoformat(),
             "replaced_by": r["replaced_by"]} for r in cur.fetchall()]}
