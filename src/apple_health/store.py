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
from dataclasses import dataclass
from datetime import date, datetime
from types import TracebackType

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = "postgresql://apple_health@localhost:5432/apple_health"

# Advisory lock id guarding schema migration. A literal, deliberately: it has to
# be identical in every process, which a hash of anything is not (PEP 456).
_MIGRATION_LOCK = 5_512_884

# Kept out of the DSN so it cannot reach committed manifests, shell history or
# a process listing.
_PASSWORD_VAR = "APPLE_HEALTH_DB_PASSWORD"

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
    CREATE TABLE hr_samples (
        workout_id bigint NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
        t          timestamptz NOT NULL,
        bpm        smallint NOT NULL
    );
    CREATE INDEX ix_hr_samples ON hr_samples (workout_id, t);

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

    CREATE TABLE routes (
        workout_id  bigint PRIMARY KEY REFERENCES workouts(id) ON DELETE CASCADE,
        n_points    int,
        distance_km double precision,
        elev_gain_m double precision,
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
    # 5 — dated zone models. HealthKit exposes zones only for live workout
    # building, so these are recorded by hand. Each runs until the next one
    # supersedes it; z5 is open-ended.
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


@dataclass(frozen=True, slots=True)
class ZoneModel:
    """HR zone boundaries in effect from a given date.

    Attributes:
        effective_from: First day this model applies; it runs until superseded.
        source: How the boundaries were obtained — 'watch-auto', 'manual', 'lab'.
        z1_max, z2_max, z3_max, z4_max: Upper bounds in bpm. Z5 is open-ended.
    """

    effective_from: date
    source: str
    z1_max: int
    z2_max: int
    z3_max: int
    z4_max: int

    def zone_of(self, bpm: float) -> int:
        """Return the zone number (1-5) this heart rate falls in."""
        for zone, upper in enumerate(
            (self.z1_max, self.z2_max, self.z3_max, self.z4_max), start=1
        ):
            if bpm <= upper:
                return zone
        return 5


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
        self._dsn = dsn or os.environ.get("APPLE_HEALTH_DSN") or DEFAULT_DSN
        password = os.environ.get(_PASSWORD_VAR) or None
        self._connection = psycopg.connect(
            self._dsn,
            row_factory=dict_row,
            # Read `timestamptz` back in UTC regardless of the server's
            # timezone. Wall-clock days come from each workout's own `tz_name`,
            # never from the session — that is the whole point of storing it.
            options="-c timezone=UTC",
            **({"password": password} if password else {}),
        )
        self._migrate()

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

    def coverage(self, requested_through: date | None = None) -> Coverage:
        """How far the record extends, and whether it covers `requested_through`.

        Every query response carries this. A caller that asks about a window
        running past the last ingest is told so explicitly rather than being
        handed a short list that reads as a complete one.

        Args:
            requested_through: End of the window the caller asked about.

        Returns:
            A `Coverage`, with `warning` set when the request outruns the data.
        """
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT MAX(observed_through) AS t FROM ingest_runs")
            observed = cursor.fetchone()["t"]

        if observed is None:
            return Coverage(None, requested_through, "No ingest has run; the record is empty.")
        if requested_through is None or requested_through <= observed.date():
            return Coverage(observed, requested_through)

        days = (requested_through - observed.date()).days
        return Coverage(
            observed,
            requested_through,
            f"Requested range extends {days} day(s) past observed data "
            f"({observed.date().isoformat()}). Sessions after that date are "
            f"UNKNOWN, not absent.",
        )

    def zone_model_at(self, when: date) -> ZoneModel | None:
        """The zone model in effect on `when`, or None if none was recorded.

        Defaulting to the model of the day — rather than the current one — is
        what keeps a zone edit from reading as a change in fitness. Callers
        comparing across periods should pin one model instead.
        """
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT effective_from, source, z1_max, z2_max, z3_max, z4_max
                  FROM hr_zone_models
                 WHERE effective_from <= %s
              ORDER BY effective_from DESC
                 LIMIT 1
                """,
                (when,),
            )
            row = cursor.fetchone()
        return ZoneModel(**row) if row else None

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
