# ADR 002 — Incremental sync from the device

- Status: accepted
- Date: 2026-06-26
- Supersedes nothing; extends [ADR 001](adr-001-storage-model.md).

## Context

ADR 001 treats `health.db` as a disposable projection rebuilt from a full Apple
Health export. That export is the problem: **"Export All Health Data" is
all-or-nothing** — there is no incremental option. Each refresh means the phone
zips ~4.4 GB of `export.xml` plus ~1.6 GB of GPX, AirDrops it, and `ah-build`
re-parses tens of millions of samples. Keeping the dataset current is too slow
to do often.

We want to fold in only what is *new* since the last update, without ever moving
the full archive again.

## Decision

Add an **incremental sync path** alongside (not replacing) the full rebuild.

```
iPhone                         iCloud / Dropbox folder        Mac / NAS
┌────────────────────┐   write  ┌──────────────────┐   read   ┌─────────────┐
│ HealthSync.app     │ ───────▶ │ delta-<ts>.json  │ ───────▶ │ ah-ingest   │
│ HKAnchoredObject-  │  nightly │ route-<uuid>.gpx │          │ → health.db │
│ Query + anchors    │          │ (append-only)    │          │             │
└────────────────────┘          └──────────────────┘          └─────────────┘
```

1. **Producer — a small on-device app (HealthSync).** Uses
   `HKAnchoredObjectQuery`, which is purpose-built for incremental sync: given a
   persisted anchor token it returns only samples added or deleted since that
   anchor. The app pre-aggregates dense quantity types into per-day buckets
   (the `daily_metrics` shape), carries workouts and the sparse allowlist as
   rows, and pulls routes via `HKWorkoutRouteQuery` into GPX. It writes a delta
   file per run on a daily `BGAppRefreshTask` plus a manual "Sync now" button.

2. **Transport — a synced cloud folder.** Deltas land in an iCloud Drive (or
   Dropbox) folder visible to the Mac/NAS. No server to host, no inbound port;
   the existing file-sync handles delivery and offline buffering.

3. **Consumer — `ah-ingest`.** Reads delta files it has not yet applied (tracked
   in a new `applied_deltas` table), and merges each in one transaction:
   workouts and records by `INSERT OR IGNORE`, routes by re-using
   `parse_gpx.summarise_gpx`, and `daily_metrics` by an **additive upsert**
   (`count`/`sum` add, `min`/`max` fold) because a day stays "open" across
   nightly syncs. `RunningCadence` is re-derived afterwards.

The wire format is frozen in the [delta contract](delta-contract.md).

### Idempotency

The additive `daily_metrics` upsert is deliberately *not* content-idempotent —
re-applying a delta would double-count. Idempotency comes instead from the
**`applied_deltas` guard**: each delta filename is recorded inside the same
transaction that applies it, so a file is applied exactly once even if
`ah-ingest` is re-run over the same folder. This requires deltas to be immutable
and the producer to never re-emit a sample under a new anchor.

### Minimal schema changes

- `workouts.uuid TEXT` (nullable, `UNIQUE`) — stable identity for incremental
  dedupe and exact deletion. Full-export rows keep `uuid = NULL` (SQLite allows
  many NULLs under a `UNIQUE` constraint, so this does not constrain rebuilds).
- New `applied_deltas (filename PRIMARY KEY, applied_at, anchor_seq, …)`.

No table is dropped or restructured; the full-rebuild path is untouched.

## Alternatives considered

- **Health Auto Export → REST/iCloud.** A third-party app that does much of
  this. Rejected for the primary path: subscription, opaque export semantics,
  and less control over the route/aggregate contract. Remains a viable fallback.
- **Apple Shortcuts automation.** Native and free, but coverage of quantity
  types is patchy and it **cannot export workout-route GPX** — a hard miss for a
  running/triathlon dataset.
- **Keep full-export only, run it rarely.** Zero code, but does not solve the
  pain; relegated to the periodic reconciliation role below.

## Consequences

- Day-to-day updates cost one small delta + an `ah-ingest` run instead of a
  multi-GB export and full parse.
- **Deletions are only partially honoured.** `HKDeletedObject` carries just a
  UUID, so workout deletes apply exactly, but deleted *dense samples* cannot be
  subtracted from aggregates and deleted *sparse records* cannot be matched
  (`records` stores no UUID). Drift from this is expected to be negligible for
  training analysis.
- **The full rebuild stays the source of truth.** It remains the recovery path
  for a lost anchor (app reinstall), a schema change, or to reconcile missed
  deletions — fully consistent with ADR 001's "disposable projection" stance. An
  occasional full `ah-build` re-bases the DB; incremental `ah-ingest` keeps it
  current in between.
- A new build artifact lives outside Python: the Swift app under `ios/`. It is
  built and signed by the user on their Mac; this repo holds its source and
  setup notes but cannot CI-build it.
