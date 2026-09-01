"""PostgreSQL persistence for sessions, samples, notes and ingest bookkeeping.

The store holds primitives and derives nothing. Heart-rate samples, laps and
route summaries are written once; zone distributions, drift and cadence are
computed at query time. That split is not tidiness: HR zones are defined on the
watch and change, so a stored zone percentage is only valid for the model that
produced it, and the raw series has to outlive any model. See
``docs/adr-006-sinks-are-plugins.md``.

Two facts every caller gets whether it asks or not:

- **Coverage.** ``ingest_runs.observed_through`` records the instant HealthKit
  was queried up to. It is not ``max(started_at)``, which reports the last
  workout and during a rest week answers a different question. A view that
  omitted this once produced a training conclusion that stood wrong for a month.
- **Zone model.** Zone numbers travel with the dated model that classified them,
  because ``Z3 14 %`` means nothing unless you know what Z3 was that week.

This was SQLite while everything ran on one laptop. Ingest and the interaction
layer now run as pods, and a single file makes the data a property of one node.

Schema changes are applied by appending to ``_MIGRATIONS``. If this outgrows a
list of DDL strings, swap in the migration_framework module rather than growing
bespoke logic here.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, tzinfo
from pathlib import Path
from types import TracebackType

DEFAULT_DSN = "postgresql://apple_health@localhost:5432/apple_health"

# Advisory lock id guarding schema migration. A literal, deliberately: it has to
# be identical in every process, which a hash of anything is not (PEP 456).
_MIGRATION_LOCK = 5_512_884

# Kept out of the DSN so it cannot reach committed manifests, shell history or
# a process listing.
_PASSWORD_VAR = "APPLE_HEALTH_DB_PASSWORD"

# Read when the variable is unset, so an unattended run (launchd, a cron pod)
# needs only a DSN. Same directory as the Box token store, same 0600 posture.
_PASSWORD_FILE = Path.home() / ".config/apple-health/db-password"


def _password(dsn: str) -> str | None:
    """The database password, unless the DSN already carries one.

    Deferring to the DSN matters: psycopg's explicit `password` argument wins
    over the connection string, so returning one unconditionally would silently
    override an inline password — pointing a throwaway container's DSN at the
    production password and failing with an authentication error that names
    neither.
    """
    from psycopg.conninfo import conninfo_to_dict

    try:
        if conninfo_to_dict(dsn).get("password"):
            return None
    except Exception:  # unparseable DSN — let psycopg raise the real error
        pass

    from_env = os.environ.get(_PASSWORD_VAR)
    if from_env:
        return from_env
    try:
        return _PASSWORD_FILE.read_text().strip() or None
    except OSError:
        return None

_MIGRATIONS: tuple[str, ...] = (
    # 1 — sessions. `timestamptz` stores the instant; `tz_name` recovers the
    # wall-clock day. Both are needed: an absolute instant cannot say which day
    # a 19:08 run in Paris belongs to once the reader is in Tokyo.
    """
    CREATE TABLE workouts (
        id           bigserial PRIMARY KEY,
        uuid         uuid UNIQUE,
        activity     text NOT NULL,
        started_at   timestamptz NOT NULL,
        ended_at     timestamptz,
        tz_name      text,
        duration_min double precision,
        distance_km  double precision,
        energy_kcal  double precision,
        avg_hr       double precision,
        max_hr       double precision,
        source       text,
        indoor       boolean
    );
    CREATE INDEX ix_workouts_started  ON workouts (started_at);
    CREATE INDEX ix_workouts_activity ON workouts (activity, started_at);
    """,
    # 2 — primitives. Never summarised into the schema.
    """
    -- The primary key is what makes re-loading a series idempotent. Without
    -- it a second load doubles every sample, and zone *percentages* still look
    -- right afterwards, which is what would make it hard to notice.
    CREATE TABLE hr_samples (
        workout_id bigint NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
        t          timestamptz NOT NULL,
        bpm        smallint NOT NULL,
        PRIMARY KEY (workout_id, t)
    );

    CREATE TABLE laps (
        workout_id bigint NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
        idx        int NOT NULL,
        started_at timestamptz NOT NULL,
        duration_s double precision,
        distance_m double precision,
        PRIMARY KEY (workout_id, idx)
    );
    """,
    # 3 — aggregates and sparse series. `daily_metrics` has no stored avg:
    # sum/count is the primitive and the average is a division.
    """
    CREATE TABLE daily_metrics (
        day   date NOT NULL,
        type  text NOT NULL,
        unit  text,
        count bigint NOT NULL,
        sum   double precision NOT NULL,
        min   double precision,
        max   double precision,
        PRIMARY KEY (day, type)
    );

    CREATE TABLE records (
        type        text NOT NULL,
        recorded_at timestamptz NOT NULL,
        value       double precision,
        unit        text,
        source      text
    );
    CREATE INDEX ix_records ON records (type, recorded_at);

    -- Keyed on filename, not workout: the GPX source has no workout linkage
    -- and matching a route to a session is a derivation nobody has written yet.
    -- workout_id is nullable so that step can fill it in later.
    CREATE TABLE routes (
        id            bigserial PRIMARY KEY,
        filename      text NOT NULL UNIQUE,
        workout_id    bigint REFERENCES workouts(id) ON DELETE SET NULL,
        started_at    timestamptz,
        ended_at      timestamptz,
        n_points      int,
        distance_km   double precision,
        duration_min  double precision,
        elev_gain_m   double precision,
        avg_speed_kmh double precision,
        min_lat double precision, min_lon double precision,
        max_lat double precision, max_lon double precision
    );
    """,
    # 4 — coverage. An anchored HealthKit query at instant T has observed
    # everything up to T; that instant was in every delta's `generated_at` and
    # the SQLite schema threw it away. Recording it is what lets a query say
    # "unknown" instead of implying "absent".
    """
    CREATE TABLE ingest_runs (
        id               bigserial PRIMARY KEY,
        source           text NOT NULL,
        ref              text NOT NULL UNIQUE,
        observed_through timestamptz NOT NULL,
        applied_at       timestamptz NOT NULL DEFAULT now(),
        workouts_added   int,
        records_added    int,
        metric_days      int
    );
    """,
    # 5 — dated zone models. Created, and deliberately unused: nothing reads or
    # writes this table today. The zone model is the fixed one in derive/zones.py
    # (ADR-006 (c)), because the boundaries have not changed and machinery for a
    # change that has not happened is machinery that can be wrong.
    #
    # It survives as an empty table rather than being dropped because migrations
    # are history. Do NOT start writing rows here without first making
    # `derive.zones` classify by them: reporting one set of bands while
    # computing with another is worse than having one set.
    """
    CREATE TABLE hr_zone_models (
        id             bigserial PRIMARY KEY,
        effective_from date NOT NULL UNIQUE,
        source         text NOT NULL,
        z1_max smallint NOT NULL,
        z2_max smallint NOT NULL,
        z3_max smallint NOT NULL,
        z4_max smallint NOT NULL,
        note text
    );
    """,
    # 6 — what no sensor records. Periods rather than weeks: the France block
    # ran 16 Jul – 12 Aug and aligned to nothing.
    """
    CREATE TABLE session_notes (
        workout_id bigint PRIMARY KEY REFERENCES workouts(id) ON DELETE CASCADE,
        note       text NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE period_notes (
        id         bigserial PRIMARY KEY,
        starts_on  date NOT NULL,
        ends_on    date,
        note       text NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE documents (
        slug       text PRIMARY KEY,
        body       text NOT NULL,
        volatility text,
        updated_at timestamptz NOT NULL DEFAULT now()
    );
    """,
    # 7 — what you are training for, and what the advisor made of each session.
    #
    # `goals.goal` is free text on purpose. A race, a return-to-load rule, a
    # season's intent and "just stay consistent" are all goals, and an enum of
    # kinds would be a branch in the code standing in for a sentence you can
    # write. `target_date` is nullable because plenty of goals have no date.
    #
    # `session_reviews` is kept apart from `session_notes` deliberately: those
    # are the athlete's words, these are a model's. Merging them would make the
    # two indistinguishable a month later, which is the same provenance failure
    # as reporting one zone model while computing with another.
    #
    # `basis` records what the review was actually written from — the coverage
    # instant, the zone bands, the queries called. A review is an opinion about
    # data; without the data it saw, it cannot be checked or re-run.
    """
    CREATE TABLE goals (
        id          bigserial PRIMARY KEY,
        goal        text NOT NULL,
        target_date date,
        created_at  timestamptz NOT NULL DEFAULT now(),
        archived_at timestamptz
    );

    CREATE TABLE session_reviews (
        workout_id bigint PRIMARY KEY REFERENCES workouts(id) ON DELETE CASCADE,
        review     text NOT NULL,
        model      text NOT NULL,
        basis      jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE INDEX goals_active ON goals (archived_at) WHERE archived_at IS NULL;
    """,
    # 8 — the conversations. Originally these were kept nowhere, on the argument
    # that a chat is a question answered rather than a fact about training. That
    # was wrong in practice: an answer worth acting on is worth re-reading, and
    # a question already asked is worth not asking twice.
    #
    # One row per exchange rather than per message: they are only ever written
    # and read together, and a half-stored exchange is a question with no answer.
    """
    CREATE TABLE chat_turns (
        id         bigserial PRIMARY KEY,
        session_id text NOT NULL,
        asked_at   timestamptz NOT NULL DEFAULT now(),
        question   text NOT NULL,
        answer     text NOT NULL,
        queries    jsonb,
        model      text
    );

    CREATE INDEX chat_turns_recent ON chat_turns (asked_at DESC);
    CREATE INDEX chat_turns_session ON chat_turns (session_id, asked_at);
    """,
    # 9 — everything HealthKit records about a workout that we were discarding.
    #
    # `route_points` is ADR-007 step 2, and the one step with a deadline: the
    # GPX files live on the Mac's disk, and the Mac is being removed from the
    # path. Points rather than the summary `routes` already holds, because a map
    # and an elevation profile cannot be drawn from a bounding box.
    #
    # The workout columns are all things the watch already recorded and the
    # pipeline dropped on the floor — weather on 1,323 of 2,752 archived
    # workouts, elevation on 1,038, METs on 1,151. Nullable throughout: absent
    # means the watch did not record it, which is not the same as zero, and a
    # zero would read as a flat course on a still day.
    #
    # `workout_segments` is what makes a triathlon legible. HealthKit exposes
    # `workoutActivities` — swim, T1, bike, T2, run — each with its own
    # statistics; without them a race is one `SwimBikeRun` row and every per-leg
    # figure has to be typed in by hand from the results PDF.
    """
    CREATE TABLE route_points (
        route_id bigint NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
        idx      int NOT NULL,
        t        timestamptz,
        lat      double precision NOT NULL,
        lon      double precision NOT NULL,
        ele_m    double precision,
        PRIMARY KEY (route_id, idx)
    );

    ALTER TABLE workouts
        ADD COLUMN weather_temp_c        double precision,
        ADD COLUMN weather_humidity_pct  double precision,
        ADD COLUMN elevation_ascended_m  double precision,
        ADD COLUMN elevation_descended_m double precision,
        ADD COLUMN avg_mets             double precision,
        ADD COLUMN pool_length_m        double precision,
        ADD COLUMN swim_location        text,
        ADD COLUMN max_speed_kmh        double precision;

    CREATE TABLE workout_segments (
        workout_id bigint NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
        idx        int NOT NULL,
        activity   text NOT NULL,
        started_at timestamptz NOT NULL,
        ended_at   timestamptz,
        stats      jsonb,
        PRIMARY KEY (workout_id, idx)
    );

    CREATE TABLE workout_events (
        workout_id bigint NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
        idx        int NOT NULL,
        kind       text NOT NULL,
        started_at timestamptz NOT NULL,
        ended_at   timestamptz,
        PRIMARY KEY (workout_id, idx)
    );
    """,
    # 10 — map tiles, cached in the database.
    #
    # A tile layer is a privacy trade and worth naming: the *pod* fetches tiles,
    # never the browser, so the tile server sees one cluster address rather than
    # a phone, and it sees tile coordinates rather than a track. Cached here
    # permanently, so each tile is fetched once ever and a route re-read a
    # hundred times costs nothing and says nothing further.
    #
    # In Postgres rather than a volume because the pod has none, and a cache
    # that empties on every deploy would re-fetch the same neighbourhood weekly
    # — which is exactly the repeated signal the caching exists to avoid.
    """
    CREATE TABLE map_tiles (
        z          int NOT NULL,
        x          int NOT NULL,
        y          int NOT NULL,
        fetched_at timestamptz NOT NULL DEFAULT now(),
        content_type text NOT NULL,
        data       bytea NOT NULL,
        PRIMARY KEY (z, x, y)
    );
    """,
    # 11 — what the advisor changed, and what it changed it from.
    #
    # The model can now write, which it could not before. The boundary is that
    # it may write what a human writes by hand — notes, goals, documents — and
    # never what a sensor measured. `workouts`, `hr_samples`, `laps` and
    # `route_points` come from the watch; a model editing those would corrupt
    # the record this whole project exists to keep honest.
    #
    # `before` is the point of this table. Without it a write is a rumour: you
    # can see that something changed and not what it displaced, and there is
    # nothing to undo it with.
    """
    CREATE TABLE advisor_writes (
        id         bigserial PRIMARY KEY,
        written_at timestamptz NOT NULL DEFAULT now(),
        session_id text,
        target     text NOT NULL,
        target_key text NOT NULL,
        summary    text NOT NULL,
        before     jsonb,
        after      jsonb,
        undone_at  timestamptz
    );

    CREATE INDEX advisor_writes_recent ON advisor_writes (written_at DESC);
    """,
)


@dataclass(frozen=True, slots=True)
class Coverage:
    """How far the record is known to extend, and whether that covers a request.

    Attributes:
        observed_through: Instant the most recent ingest queried HealthKit up
            to. Everything before it is known; everything after is unobserved.
        requested_through: End of the window the caller asked about, when there
            was one.
        warning: Human-readable gap notice, or None when the request is fully
            covered. Phrased as *unknown, not absent* — the distinction a
            reader got wrong once already.
    """

    observed_through: datetime | None
    requested_through: date | None = None
    warning: str | None = None


def assess_coverage(
    observed: datetime | None,
    requested_through: date | None = None,
    tz: tzinfo | None = None,
) -> Coverage:
    """Decide whether `observed` covers every day through `requested_through`.

    Pure, so the comparison can be tested without a database — it is the part
    that was wrong, and wrong in a way that reads as correct.

    Args:
        observed: Instant the last ingest queried HealthKit up to, or None.
        requested_through: Last calendar day the caller asked about.
        tz: Zone that date is expressed in; defaults to the system zone.

    Returns:
        A `Coverage`, with `warning` set when the request outruns the data.
    """
    if observed is None:
        return Coverage(None, requested_through, "No ingest has run; the record is empty.")
    if requested_through is None:
        return Coverage(observed, None)

    zone = tz or datetime.now().astimezone().tzinfo
    if observed >= datetime.combine(requested_through, clock_time.max, tzinfo=zone):
        return Coverage(observed, requested_through)

    local = observed.astimezone(zone)
    return Coverage(
        observed,
        requested_through,
        f"Requested range extends past observed data. HealthKit was last "
        f"observed through {local:%Y-%m-%d %H:%M %Z}; the remainder of that "
        f"day and anything later is UNKNOWN, not absent.",
    )


class Store:
    """PostgreSQL-backed storage for the ingest pipeline.

    Usable as a context manager, which commits on clean exit and rolls back on
    exception.
    """

    def __init__(self, dsn: str | None = None) -> None:
        """Connect to (and migrate) the database.

        Args:
            dsn: Connection string, including any TLS parameters. Defaults to
                `APPLE_HEALTH_DSN`, then to a local database. The password,
                when one is needed, comes from `APPLE_HEALTH_DB_PASSWORD`.
        """
        # Imported here, not at module scope, so the value types and
        # `assess_coverage` stay usable without the driver installed — psycopg
        # is an optional extra until a command actually reads from Postgres.
        import psycopg
        from psycopg.rows import dict_row

        self._dsn = dsn or os.environ.get("APPLE_HEALTH_DSN") or DEFAULT_DSN
        password = _password(self._dsn)
        self._connection = psycopg.connect(
            self._dsn,
            row_factory=dict_row,
            # Read `timestamptz` back in UTC regardless of the server's
            # timezone. Wall-clock days come from each workout's own `tz_name`,
            # never from the session — that is the whole point of storing it.
            options="-c timezone=UTC",
            **({"password": password} if password else {}),
        )
        try:
            self._migrate()
        except Exception:
            # __init__ raised, so there is no object for __exit__ to run
            # on. Leaving the connection open would hold the advisory lock
            # and block every other pod's startup.
            self._connection.close()
            raise

    def _migrate(self) -> None:
        """Apply any migrations the database has not seen yet.

        Advisory-locked: several pods share this database and may start at the
        same moment, and two of them running the same DDL is an error rather
        than a race that resolves itself. The lock is transaction-scoped so it
        is released at commit, not before `schema_version` is durable.
        """
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            cursor.execute("SELECT MAX(version) AS v FROM schema_version")
            current = cursor.fetchone()["v"] or 0
            if current > len(_MIGRATIONS):
                # An older build against a newer database. Proceeding would run
                # queries the schema may not support, so say so.
                raise RuntimeError(
                    f"database is at schema {current} but this build knows "
                    f"only {len(_MIGRATIONS)}; deploy a newer apple-health"
                )
            for version, ddl in enumerate(_MIGRATIONS[current:], start=current + 1):
                cursor.execute(ddl)
                cursor.execute(
                    "INSERT INTO schema_version (version) VALUES (%s)", (version,)
                )
        self._connection.commit()

    @contextmanager
    def cursor(self) -> Iterator[object]:
        """Yield a cursor on the open connection.

        Deliberately thin: the SQLite→Postgres migration is a one-shot and its
        SQL belongs to it, not as permanent API here.
        """
        with self._connection.cursor() as cur:
            yield cur

    def commit(self) -> None:
        """Commit the open transaction."""
        self._connection.commit()

    def rollback(self) -> None:
        """Discard the open transaction."""
        self._connection.rollback()

    def close(self) -> None:
        """Close the connection, discarding anything uncommitted."""
        self._connection.close()

    def coverage(
        self,
        requested_through: date | None = None,
        tz: tzinfo | None = None,
    ) -> Coverage:
        """How far the record extends, and whether it covers `requested_through`.

        Every query response carries this. A caller that asks about a window
        running past the last ingest is told so explicitly rather than being
        handed a short list that reads as a complete one.

        A calendar day is only covered once the ingest instant reaches its
        *end*, in the zone the caller means by that date. Comparing
        `observed.date()` to `requested_through` instead is wrong in both
        directions and by a full day: an 08:00 JST sync reads back as
        `2026-08-25T23:00Z`, which would report a just-synced record as a day
        stale; an 18:00 PDT sync reads back as the following UTC date, which
        would report full coverage for a day observed only through the evening.

        Args:
            requested_through: Last calendar day the caller asked about.
            tz: Zone that date is expressed in. Defaults to the system zone,
                which is the athlete's own in the single-user case this serves.

        Returns:
            A `Coverage`, with `warning` set when the request outruns the data.
        """
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT MAX(observed_through) AS t FROM ingest_runs")
            observed = cursor.fetchone()["t"]

        return assess_coverage(observed, requested_through, tz)

    def __enter__(self) -> Store:
        """Enter the context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Commit on clean exit, roll back on exception, then close."""
        if exc is None:
            self._connection.commit()
        else:
            self._connection.rollback()
        self._connection.close()
