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
- **Unattended:** `tools/sync_cycle.py` chains the whole pipeline
  (`icloud_fetch` → `ah-ingest` → `session_detail` → `vault_push`) and runs
  every 30 min under launchd — copy `tools/launchd/net.dev2.healthsync.sync.plist`
  into `~/Library/LaunchAgents/` and `launchctl load` it. The final step pushes
  curated summaries to the Claude Vault on Box and needs a one-time
  `uv run python tools/box_auth.py` on the Mac.
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

## Race archive

The DB keeps only *daily* HR aggregates, so per-race detail (heart-rate by leg,
zone distribution, drift) is extracted from the raw `export.xml` and kept as one
markdown file per race under `data/races/`:

```bash
# Add a race to the RACES registry in the tool, then:
AH_EXPORT=/path/to/export.xml uv run python tools/race_detail.py
```

This is the durable record for re-analysis later — segment windows come from the
GPX route times (JST). `data/races/` is git-ignored (personal data) but lives on
the NAS repo.

## Tests

```bash
uv run --with pytest pytest -q
```

Tests run the parsers over tiny inline fixtures (no real export needed).

## Roadmap

- Route heatmap (folium) from on-demand full track points.
- Validate race-pacing models against historical race-effort runs.
- Training-load vs injury overlay.
- Sync distilled insights back to the Claude Vault `sport-*` notes.
