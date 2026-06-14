# ADR 001 — Storage model for the Apple Health dataset

- Status: accepted
- Date: 2026-06-14

## Context

The Apple Health export is large and awkward:

- `export.xml` ≈ 4.4 GB — tens of millions of `Record` samples (HeartRate alone
  is millions of rows over several years).
- `workout-routes/` ≈ 1.6 GB — 1320 GPX files, per-second GPS tracks.

The goal is training analysis over ~9 years (running / triathlon), not a faithful
clinical replica. We need fast queries on workouts, daily metric trends, and
route summaries — without lugging the raw export around or blowing up the DB.

## Decision

**Three-tier storage.**

1. **Cold archive (raw export) — NAS, immutable.** The original export is the
   source of truth and is never edited. `export_cda.xml` (a redundant CDA copy)
   may be dropped.
2. **Working dataset — SQLite, disposable, rebuilt from source.**
   - `workouts`: one row per workout, fully kept.
   - `daily_metrics`: every quantity type folded to one row per `(day, type)`
     with `count/sum/min/max/avg`. Turns millions of dense samples into a
     compact daily series.
   - `records`: raw rows kept **only** for a sparse allowlist (resting HR,
     VO2max, body mass, HRV SDNN) — low volume, high analytic value, exact
     values worth preserving.
   - `routes`: one summary row per GPX (distance, duration, elevation gain,
     bounding box). Raw track points are **not** stored.
3. **Distilled insights — Claude Vault `sport-*` notes.** Only conclusions.

## Consequences

- DB shrinks from ~6 GB of XML/GPX to a few hundred MB; queries are instant.
- Dense raw samples (e.g. individual HR readings) are lost — acceptable, since
  daily aggregates answer the training questions and the raw export is retained
  if exact samples are ever needed.
- Route heatmaps need track points, which aren't stored. Mitigation: a later
  on-demand loader reads GPX directly for the heatmap rather than bloating the DB.
- No migration framework: the DB is a rebuildable projection of an immutable
  source, so schema migrations add no value — a plain `CREATE TABLE` schema and
  full rebuild is simpler.
