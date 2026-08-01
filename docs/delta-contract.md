# Delta file contract (v1, v2)

The interface between the on-device **HealthSync** app (producer) and the
`ah-ingest` command (consumer). The app writes delta files into its iCloud
Drive folder `iCloud.net.dev2.healthsync/Documents/HealthSync/` (ADR-002,
restored by ADR-004); `tools/icloud_fetch.py` mirrors them into a local inbox,
and `ah-ingest` reads files it has not yet applied and merges them
idempotently into `health.db`. Both sides MUST agree on this document.

> Versioned by the top-level `schema` integer. A consumer MUST refuse a file
> whose `schema` it does not understand rather than guess.

## Why deltas are pre-aggregated

The whole point of this sync is to never move millions of raw samples again. So
for dense quantity types the **app aggregates samples into per-day buckets
before writing** — exactly the shape of the `daily_metrics` table. Each delta
carries only the *new* samples since the last anchor, so a given day's bucket is
**partial** and `ah-ingest` adds it onto whatever is already stored (see
[Merge semantics](#merge-semantics)). Sparse, high-value types and workouts are
carried as individual rows.

## Files

A single sync run (anchor advance) produces:

| File | Naming | Notes |
|------|--------|-------|
| Delta | `delta-<UTC>-<seq>.json` | `<UTC>` = `YYYYMMDDTHHMMSSZ`, `<seq>` = zero-padded monotonic counter, e.g. `delta-20260626T030000Z-0042.json` |
| Route | `route-<workout-uuid>.gpx` | One per workout that has a route; GPX 1.1, same shape as Apple's `workout-routes/*.gpx` so `parse_gpx` reads it unchanged |
| HR series | `hr-<workout-uuid>.csv` | One per workout with heart-rate samples; header `time,bpm`, one ISO-8601-UTC row per sample. **Not ingested into the DB** — read straight from the inbox by `tools/session_detail.py` for per-session zone/drift analysis |

- Files are **append-only and immutable** once written. Never rewrite a delta.
- `ah-ingest` processes pending deltas in **ascending filename order** (which is
  chronological) and records each applied filename so it is never re-applied.
- A delta references its sidecar files by name; they sit beside the JSON.
- **A missing sidecar is not retried.** If a delta is applied while its
  `route_file` has not yet arrived, `ah-ingest` warns (`! route file missing,
  skipped`) but still records the delta as applied, so that `routes` row is
  lost until a full `ah-build`. The producer writes sidecars first and
  `icloud_fetch` copies them first, but iCloud does not guarantee propagation
  order — see ADR-004. Consumers MUST NOT assume a referenced sidecar exists.
- **Exception — HR-series backfill.** `hr-<uuid>.csv` files MAY exist with no
  delta referencing them: the app's "Backfill HR series" pass (added
  2026-07-12) writes the missing CSVs for workouts synced by app versions
  older than 2026-07-11, whose deltas predate the `hr_file` field. This is
  safe only because HR CSVs are never ingested into the DB — consumers look
  them up by workout uuid, and a delta's `hr_file: null` does not mean the
  file is absent. Nothing else may be written outside a delta.

## Delta JSON

```jsonc
{
  "schema": 1,
  "generated_at": "2026-06-26T03:00:00Z",   // ISO-8601 UTC, informational
  "device": "iPhone15,2",                    // informational
  "app_version": "1.0.0",                    // informational
  "anchor_seq": 42,                          // monotonic; matches filename <seq>

  "workouts": {
    "added": [
      {
        "uuid": "5C3A…",                      // HKWorkout.uuid — stable identity
        "activity": "Running",                // HK type with HKWorkoutActivityType stripped
        "start": "2026-06-25 07:12:33 +0200", // Apple Health date string (with offset)
        "end":   "2026-06-25 08:01:10 +0200",
        "duration_min": 48.6,
        "distance_km": 10.21,                 // null if none
        "energy_kcal": 612.0,                 // null if none
        "avg_hr": 152.0,                      // null if none
        "max_hr": 178.0,                      // null if none
        "source": "Apple Watch",
        "indoor": 0,                          // 1 indoor, 0 outdoor, null unknown
        "route_file": "route-5C3A….gpx",      // null if the workout has no route
        "hr_file": "hr-5C3A….csv"             // null if no HR samples (added 2026-07-11; older deltas lack it)
      }
    ],
    "deleted": ["<uuid>", "…"]                // workouts removed on device since last anchor
  },

  "records": {                                // sparse allowlist only (see SPARSE_TYPES)
    "added": [
      {
        "type": "RestingHeartRate",           // HK type with identifier prefix stripped
        "start": "2026-06-25 06:00:00 +0200",
        "value": 44.0,
        "unit": "count/min",
        "source": "Apple Watch"
      }
    ],
    "deleted": []                             // best-effort; see Deletions
  },

  "daily_metrics": {                          // partial per-(day,type) aggregates
    "added": [
      {
        "day": "2026-06-25",                  // YYYY-MM-DD, local day of the sample start
        "type": "HeartRate",                  // HK type with identifier prefix stripped
        "unit": "count/min",
        "count": 3120,                        // # samples in THIS delta for (day,type)
        "sum": 474240.0,
        "min": 41.0,
        "max": 181.0
      }
    ]
  }
}
```

All four top-level sections are optional; omit empty ones or send empty arrays.
A delta with no `added`/`deleted` anywhere is valid (a heartbeat) but the app
SHOULD simply not write one.

### Field rules

- **Type / activity names are already normalised** — the app strips
  `HKQuantityTypeIdentifier` / `HKCategoryTypeIdentifier` and
  `HKWorkoutActivityType` so values match what the full-export parser stores
  (e.g. `HeartRate`, `Running`). `ah-ingest` does not re-strip.
- **Units must be stable per type.** The app MUST request each quantity type in
  one fixed unit for the life of the install (e.g. `HeartRate` always
  `count/min`, distances always `km`, energy `kcal`). `sum`/`min`/`max` are only
  additive across deltas if the unit never changes. The full-export builder
  makes the same stable-unit assumption.
- **`day`** is the local calendar day of the sample's `startDate`, matching the
  full-export parser's `_day()` (first 10 chars of the Apple date string).
- **`daily_metrics` carries no `avg`.** `ah-ingest` derives `avg = sum / count`
  after merging, so partial averages never need to be reconciled.
- **`RunningCadence` is not sent.** It is a synthetic type derived on the
  consumer side from `RunningSpeed` / `RunningStrideLength` (see
  `db.derive_cadence`); `ah-ingest` re-derives affected days after each merge.

## Merge semantics

`ah-ingest` applies one delta file in a single transaction, then records its
filename in `applied_deltas` within that same transaction (so a file is applied
exactly once, atomically).

| Section | Operation | Key | Idempotency |
|---------|-----------|-----|-------------|
| `workouts.added` | `INSERT OR IGNORE` | `workouts.uuid` (`UNIQUE`) | dup uuid ignored |
| `workouts.deleted` | `DELETE WHERE uuid = ?` | `uuid` | no-op if absent |
| `records.added` | `INSERT OR IGNORE` | `(type, start)` | dup ignored |
| `daily_metrics.added` | additive upsert | `(day, type)` | **file-level** — see below |
| `daily_metrics.added` *(schema 2)* | replacing upsert | `(day, type)` | content-idempotent — see [Backfill deltas](#backfill-deltas-schema-2) |
| route GPX | `summarise_gpx` → `INSERT OR REPLACE` | `routes.filename` (`UNIQUE`) | re-summarise is safe |

The additive upsert for `daily_metrics`:

```sql
INSERT INTO daily_metrics (day, type, unit, count, sum, min, max, avg)
VALUES (?,?,?,?,?,?,?, ?/?)
ON CONFLICT(day, type) DO UPDATE SET
    count = count + excluded.count,
    sum   = sum   + excluded.sum,
    min   = min(min, excluded.min),
    max   = max(max, excluded.max),
    unit  = excluded.unit,
    avg   = (sum + excluded.sum) / (count + excluded.count);
```

Because this **adds**, it is *not* content-idempotent — applying the same delta
twice would double-count. Idempotency therefore comes from the `applied_deltas`
guard, **not** from the SQL. This is why deltas must be immutable and the
producer must never re-emit a sample under a new anchor — *except* via a
backfill delta, which is exactly the escape hatch for re-emitting.

## Backfill deltas (schema 2)

A normal delta reports "samples since the anchor", so its day buckets are
partial and must be added. That makes it impossible to re-ship a day the DB
already holds. A **backfill delta** inverts that: the producer re-queries a
closed date range *by date, ignoring anchors*, so each bucket covers a whole
day and is authoritative. `ah-ingest` therefore **replaces** those `(day, type)`
rows instead of adding to them.

```jsonc
{
  "schema": 2,                              // MUST be 2 when "backfill" is present
  "backfill": { "from": "2026-07-16", "to": "2026-07-28" },   // inclusive, local days
  "anchor_seq": 11,
  // ... workouts / records / daily_metrics exactly as in v1
}
```

Rules, all enforced by `ah-ingest` *before* any write:

- `schema: 2` and the `backfill` block imply each other; either alone is refused.
  A schema-1 file carrying `backfill` would be merged **additively** by an older
  consumer, silently double-counting — hence the version bump.
- `to` MUST be strictly before today. A range covering today would be replaced
  and then added to by that day's next incremental delta.
- Every `daily_metrics` bucket MUST fall inside `[from, to]`.
- The producer MUST NOT advance any anchor when writing a backfill (it consumes
  a sequence number only), so the incremental stream is unaffected and the same
  range can be backfilled repeatedly.

Why this cannot duplicate:

| Section | Why re-shipping is safe |
|---------|-------------------------|
| `workouts.added` | deduped on `uuid`, then on the natural key `(activity, start)` |
| `records.added` | deduped on `(type, start, source)` |
| `daily_metrics.added` | whole-day buckets **replace** the stored row |
| route GPX | `INSERT OR REPLACE` on `routes.filename` |

So a backfill delta is *content*-idempotent: applying it twice, under different
filenames, leaves the same DB. That is the one place this contract does not
depend on the `applied_deltas` guard.

## Deletions

`HKAnchoredObjectQuery` reports deletions, but a deleted object carries **only a
UUID** — never its value or day. Consequences:

- **Workouts** — deletable exactly (`workouts.uuid` is stored). Applied.
- **Sparse records** — `records` stores no UUID, and a delete gives no
  `(type, start)`, so deletes **cannot** be matched. `records.deleted` is
  accepted in the schema but **ignored** by `ah-ingest` v1.
- **`daily_metrics`** — aggregates cannot be decremented without the deleted
  sample's value/day. Deletions of dense samples are **not** reflected.

This is acceptable under [ADR-002](adr-002-incremental-sync.md): the DB is a
disposable projection, and an occasional full rebuild from a fresh export
reconciles any drift from missed deletions.

## Producer checklist (HealthSync app)

1. **Never emit a sample whose `startDate` predates the bootstrap cutoff**
   (`SyncEngine.bootstrapCutoff` — the day after the full export the DB was
   built from). History before the cutoff is owned by the full export; because
   the `daily_metrics` merge is additive, re-sending it would double-count.
   The cutoff also keeps the anchor-less first sync bounded instead of pulling
   the entire HealthKit history.
2. Persist one `HKQueryAnchor` per observed type; advance only after the delta
   file has been written to the iCloud folder — that local write is the
   durable write, and iCloud uploads it afterwards (ADR-004). A failed write
   leaves anchors untouched, so the same window is simply re-queried.
3. Aggregate dense quantity samples into `(day, type)` buckets in the fixed unit
   before writing; emit sparse-allowlist types and workouts as rows.
4. Write sidecars (route GPX, HR-series CSV) next to the JSON, referenced by
   `route_file` / `hr_file`.
5. Write the JSON **last** (after its sidecars) and atomically (temp name +
   rename), so a consumer never sees a delta before its files exist.
6. Never rewrite or delete a delta the app has already published.
