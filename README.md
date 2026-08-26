# apple-health

Turn an Apple Health export into a small, queryable **SQLite** dataset and a
training **headline-stats** report. Built for analysing years of running /
triathlon data without dragging the multi-gigabyte raw export around.

## Why

An Apple Health export is unwieldy:

| Source | Size | Problem |
|--------|------|---------|
| `export.xml` | ~4.4 GB | Tens of millions of raw samples |
| `workout-routes/*.gpx` | ~1.6 GB | 1300+ per-second GPS tracks |

This project reduces it to a lean DB (a few hundred MB) that answers training
questions instantly. The raw export stays **immutable on cold storage (NAS)**;
the DB is a disposable projection, rebuilt from source whenever needed.

## Design

```
sources  ──▶  store  ──▶  derive  ──▶  sinks
(export,      (sqlite,     (zones,      (markdown report, html report,
 healthsync,   postgres)    cadence,     session files, vault brief,
 gpx, icloud)               drift)       race archive)
```

Sources and derivations are the expensive, stable layer. **Where the data lands
is a plugin** — adding a destination is a file under `sinks/`, not a redesign.
Four earlier ADRs each rewrote the pipeline to change the destination; see
[ADR-006](docs/adr-006-sinks-are-plugins.md) for why that stopped.

Two properties the sinks depend on:

- **Coverage is a recorded fact.** Every query knows the instant HealthKit was
  last observed through, and says so when a request runs past it. A view that
  omitted this once produced a training conclusion that stood wrong for a month.
- **Derivations are computed, never stored.** HR zone boundaries are defined on
  the watch and change; a persisted zone percentage is only valid for the model
  that produced it, so the raw series is the thing that is kept.

## Data model

See [`docs/adr-001-storage-model.md`](docs/adr-001-storage-model.md) for the
rationale. In short:

- **`workouts`** — one row per workout (activity, distance, duration, energy,
  avg/max HR from `WorkoutStatistics`), fully kept.
- **`daily_metrics`** — every quantity type folded to one row per `(day, type)`
  with `count/sum/min/max/avg`. Compact daily time series for dense metrics
  (heart rate, cadence, energy, …).
- **`records`** — raw rows kept only for a sparse, high-value allowlist
  (resting HR, VO2max, body mass, HRV).
- **`routes`** — one summary row per GPX (distance, duration, elevation gain,
  bounding box). Raw track points are not stored; they can be loaded on demand
  for a heatmap later.

## Usage

```bash
# Build the dataset (run from the repo root; uses uv — see global conventions)
# Raw export archive lives at /Volumes/nicolas-data/HealthData/apple_health_export
uv run ah-build \
    --export "/Volumes/nicolas-data/HealthData/apple_health_export/export.xml" \
    --routes "/Volumes/nicolas-data/HealthData/apple_health_export/workout-routes" \
    --db data/health.db

# Print the headline-stats report
uv run ah-report --db data/health.db
# or write it to a file
uv run ah-report --db data/health.db --out report.md

# Generate a standalone HTML dashboard (D3.js charts + training recommendations)
uv run ah-html --db data/health.db --out reports/health-report.html
```

The HTML report (`ah-html`) is a single self-contained file: it embeds its data
as JSON and renders trend charts (VO2max recovery, yearly/monthly volume,
intensity-zone distribution, cadence drift, resting-HR/HRV) client-side with
D3.js loaded from a CDN — so it needs an internet connection to draw, but no
build step or server. It closes with a data-driven recommendations section.

`data/`, `*.db` and the raw `*.xml` / `*.gpx` are git-ignored — only code is
versioned.

## Keeping it up to date (incremental sync)

Apple's "Export All Health Data" is all-or-nothing — re-exporting the multi-GB
archive just to add a week of runs is painful. The incremental path avoids it:
a small on-device app reads only what's new since the last sync and drops a
compact **delta file** into an iCloud Drive folder, which `ah-ingest` merges
into the DB. See [`docs/adr-002-incremental-sync.md`](docs/adr-002-incremental-sync.md)
and the [delta contract](docs/delta-contract.md). (A Box-based transport was
built and reverted before activation —
[ADR-004](docs/adr-004-revert-to-icloud-transport.md).)

```bash
# Merge any new delta files (idempotent — safe to re-run, e.g. from cron)
uv run ah-ingest \
    --inbox "~/Library/Mobile Documents/iCloud~net~dev2~healthsync/Documents/HealthSync" \
    --db data/health.db
```

- **Producer:** the `HealthSync` iOS app under [`ios/`](ios/)
  (`HKAnchoredObjectQuery` for new/deleted samples; pre-aggregates dense metrics
  into daily buckets; exports route GPX). Build/sign it from Xcode — see its
  README.
- **Unattended:** `ah-sync` chains the whole pipeline
  (`ah-fetch` → `ah-ingest` → `ah-sessions` → `ah-vault`) and can run
  every 30 min under launchd — copy `tools/launchd/net.dev2.healthsync.sync.plist`
  into `~/Library/LaunchAgents/` and `launchctl load` it. The final step pushes
  curated summaries to the Claude Vault on Box and needs a one-time
  `uv run python tools/box_auth.py` on the Mac (an operator script, not part
  of the pipeline).
- **Bootstrap:** the first sync has no anchor, so it ships your full history as
  one delta — point `ah-ingest` at a **fresh** `--db` to bootstrap. (`ah-ingest`
  refuses to merge into a DB made by full `ah-build`, since unioning the two
  would double-count daily aggregates; pass `--force` only if you know better.)
- **Reconciliation:** `ah-build` from a fresh full export stays the recovery /
  re-base tool (e.g. after a lost anchor or to honour sample deletions). The two
  paths target the same dataset but are not meant to be unioned on one DB.

Deletions are only partly honoured: workout deletes apply exactly, but deleted
*dense samples* can't be subtracted from aggregates (HealthKit gives only a UUID
on delete) — an occasional full rebuild reconciles. This is consistent with the
"DB is a disposable projection" stance in ADR-001.

## Postgres store (ADR-006)

The SQLite dataset is still the source of record. The Postgres store exists and
is populated by a one-shot migration, which also recovers two things SQLite
could not hold: the heart-rate series (from the `hr-<uuid>.csv` sidecars, so a
session no longer needs an `--inbox` to render its zones) and **coverage** — the
instant each delta had observed HealthKit through, read from its `generated_at`.

The estate's server is `postgres.int.dev2.net` (ras12, PostgreSQL 17.6), where
the dataset now lives:

```bash
export APPLE_HEALTH_DSN='postgresql://apple_health@postgres.int.dev2.net:5432/apple_health?sslmode=require'

uv run --extra pg ah-migrate --dry-run    # counts, then rolls back
uv run --extra pg ah-migrate              # commits
```

The password is **not** part of the DSN — it comes from `APPLE_HEALTH_DB_PASSWORD`
or, failing that, `~/.config/apple-health/db-password` (0600, beside the Box
token store), so an unattended run needs only the DSN and nothing lands in a
manifest, `ps`, or shell history. Provisioning is `tmp/provision-pg.sh`.

A throwaway server is enough to try any of this without touching the estate:

```bash
docker run --rm -d --name ah-pg -e POSTGRES_PASSWORD=test \
    -e POSTGRES_DB=apple_health -e POSTGRES_USER=apple_health \
    -p 55433:5432 postgres:17-alpine
export APPLE_HEALTH_DSN=postgresql://apple_health:test@localhost:55433/apple_health
```

The migration is one-shot and refuses a populated target rather than doubling
rows. `RunningCadence` is deliberately not carried over: it is a derivation
(speed ÷ stride), the schema's `NOT NULL` on `sum` is what refuses to store it,
and the formula lives in `derive/cadence.py`.

Why coverage is a recorded fact rather than `max(started_at)`: on the current
data the last workout starts at `2026-08-25T03:46Z` while HealthKit was observed
through `2026-08-25T10:14Z` — six hours apart. Asking "do you cover the 25th?"
correctly warns, because in JST that day was not over when the sync ran.

## Race archive

The DB keeps only *daily* HR aggregates, so per-race detail (heart-rate by leg,
zone distribution, drift) is extracted from the raw `export.xml` and kept as one
markdown file per race under `data/races/`:

```bash
# Add a race to the RACES registry in the sink, then:
AH_EXPORT=/path/to/export.xml uv run ah-races
```

This is the durable record for re-analysis later — segment windows come from the
GPX route times (JST). `data/races/` is git-ignored (personal data) but lives on
the NAS repo.

## Tests

```bash
uv run --extra dev pytest -q
```

Tests run the parsers over tiny inline fixtures (no real export needed).

## Roadmap

- Route heatmap (folium) from on-demand full track points.
- Validate race-pacing models against historical race-effort runs.
- Training-load vs injury overlay.
- Sync distilled insights back to the Claude Vault `sport-*` notes.
