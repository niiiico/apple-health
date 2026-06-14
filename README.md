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
uv run ah-build \
    --export "/path/to/apple_health_export/export.xml" \
    --routes "/path/to/apple_health_export/workout-routes" \
    --db data/health.db

# Print the headline-stats report
uv run ah-report --db data/health.db
# or write it to a file
uv run ah-report --db data/health.db --out report.md
```

`data/`, `*.db` and the raw `*.xml` / `*.gpx` are git-ignored — only code is
versioned.

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
