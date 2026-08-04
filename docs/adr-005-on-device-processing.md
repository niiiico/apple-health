# ADR-005 — Move processing on-device; Box holds curated output only

- Status: **accepted**
- Date: 2026-08-01 (accepted 2026-08-04)
- Supersedes the topology of [ADR-002](adr-002-incremental-sync.md); retires the
  transport debate in [ADR-003](adr-003-box-transport-vault-push.md) /
  [ADR-004](adr-004-revert-to-icloud-transport.md) as moot.

## Context

Three ADRs have argued about how to move Apple Health data from the phone to the
Mac. None of them examined whether the Mac should be in the path at all.

The premise entered in ADR-002, in its opening diagram:

```
iPhone  ──▶  iCloud folder  ──▶  Mac / NAS
                                 ah-ingest → health.db
```

"Consumer — `ah-ingest`" on the Mac. Every later decision inherited that as
unexamined background. ADR-003 chose Box, but only as the pipe *into* the Mac
("the Mac runs `tools/sync_cycle.py` … → `vault_push`"). ADR-004 reverted to
iCloud. Both debated **which pipe**; neither asked **whether a pipe is needed**.

The actual goal — never written down — was the opposite topology: **run the
process on the iPhone and remove the Mac altogether, with the result stored on
Box.** Under that goal, ADR-004's reasoning is locally correct and strategically
irrelevant: "iCloud's durability is free" and "the outbox bought back what iCloud
gave for nothing" are both true *given* a phone→Mac hop, and apply to nothing
once the hop is gone.

The failure record supports the reframing. Every incident has been in the Mac
hop, never in HealthKit:

- deltas `0008`–`0009` lost during the Box detour, never applied;
- delta `0007` stuck for 17 days as an unmaterialised iCloud placeholder;
- delta `0012` sat in iCloud for a day because nothing invoked `icloud_fetch`;
- ADR-004 lists "staleness is still silent" as knowingly unresolved.

Each was repaired by hand. The schema-2 backfill mechanism (commit `56be817`)
is itself a repair tool for a class of gap that only a transport can create.

## Decision

**A new on-device app — `VaultBrief` — reads HealthKit directly, renders the
curated Vault markdown, and uploads it to Box. It replaces the delta pipeline
entirely.**

It is **new software, not a refactor of HealthSync** — the data flow inverts, so
there is no incremental path from one to the other. It lives at
`ios/VaultBrief/`, alongside the untouched `ios/App/` (HealthSync).

```
iPhone
┌──────────────────────────────────────────┐        ┌──────────────┐
│ HealthKit  ──▶  render curated markdown  │ ─────▶ │ Box: Vault   │
│ (queried on demand, no anchors)          │        │ (curated .md)│
└──────────────────────────────────────────┘        └──────────────┘
```

- **Box carries curated Vault markdown only** — the rolling per-discipline
  session files and the weekly brief, matching the Vault's 100–500-token
  convention. No delta JSON, no GPX, no HR CSV, no database.
- **HealthKit is the store.** There is no on-device DB. The Mac needed
  `health.db` only because the Mac has no HealthKit; the phone has no such gap.
  Rolling windows (last 5 sessions per discipline, current week) are shallow
  queries, not analytics over nine years.
- **No anchors, no deltas, no idempotency bookkeeping.** Each run renders the
  current window from scratch and overwrites. Rendering is naturally idempotent;
  nothing accumulates, so nothing can double-count or go stale silently.
- **`BoxClient.swift` is recovered from `box-transport-v1`**, retargeted from
  delta upload to Vault markdown. Its OAuth model (in-app
  `ASWebAuthenticationSession`, refresh token in the Keychain, rotating
  single-use refresh tokens) carries over from ADR-003 unchanged.

### What this deletes at cutover

All of it exists to serve a hop that will no longer exist:

| Component | Purpose |
|---|---|
| `docs/delta-contract.md`, schema 1 + 2 | Describe HealthKit to a non-HealthKit consumer |
| `AnchorStore.swift`, `HKAnchoredObjectQuery` path | Send only what is new |
| `DeltaWriter.swift`, `DeltaModels.swift`, `SyncEngine.swift` | Produce delta files |
| route GPX / HR CSV sidecars | Carry series the Mac cannot query |
| `ingest.py`, `applied_deltas`, uuid dedup | Idempotent replay |
| `icloud_fetch.py`, the NAS inbox | Durable hand-off |
| `sync_cycle.py`, `tools/launchd/` | Drive the whole chain |
| `vault_push.py`, `box_client.py`, `box_auth.py` | Mac-side Vault writer |

## Side-by-side transition

The existing pipeline **keeps running unchanged** until the new app is trusted.
Both read HealthKit concurrently, which is safe — HealthKit reads are
non-exclusive and carry no lease.

Two constraints during the overlap:

- **Two writers to one Vault is the real hazard.** `vault_push.py` and the new
  app would clobber each other's session files and both append to
  `_changelog.md`. The new app MUST write to a distinct staging folder
  (`Vault-next/` or equivalent) until cutover. Diffing staging against the live
  Vault is also how the new renderer gets validated.
- **HealthSync must be upgraded in place, never deleted and reinstalled.** Its
  anchor store lives in the app container; losing it forces a full `ah-build`
  re-base (ADR-002, restated in ADR-004).

### Validating the renderers

The two implementations must agree before the Mac's copy is retired. The
renderers in `ios/VaultBrief/VaultBrief/` (`Zones.swift`, `VaultRender.swift`)
are deliberately pure functions over value types with no HealthKit import, so
they compile for the host and can be fed the same data `health.db` holds.

    ios/VaultBrief/Parity/check.sh [YYYY-MM-DD]

renders all four files both ways and diffs them. It passes today against the
live DB: every data line — session lines, zone percentages, drift thirds, weekly
totals, wellness, significant workouts — is byte-identical. Only the two
provenance lines differ, by design, and the check filters exactly those.

This is what makes the staging comparison meaningful rather than a vibe check,
and it runs without a device.

**`health.db` retires at cutover, not before.** During the overlap it remains the
fallback and the reference output. Retiring it early would remove the only thing
the new renderer can be checked against.

## Consequences

- **The daily path loses its most fragile component.** No transport means no
  transport gap: the failure class behind every incident above disappears rather
  than being monitored.
- **The analytical layer loses its home.** `report.py`, `html_report.py` (the D3
  dashboard) and `race_detail.py` all read `health.db`; the phone renders only
  curated markdown. At cutover these stop unless separately ported. This is the
  main cost of the decision and is accepted deliberately.
- **It is reversible.** ADR-001's cold archive on the NAS is immutable and
  untouched, and `ah-build` reconstructs `health.db` from it in full. The DB is a
  disposable projection by design — retiring it forecloses nothing.
- **Race archives keep their existing dependency.** `race_detail.py` mines raw
  `export.xml` directly, not the delta path, so archived races (`data/races/`)
  are unaffected by the transport going away — only by the DB retiring.
- **One Box grant, on the phone.** The Mac needs no Box credentials once
  `vault_push` retires. ADR-003's rule stands: two devices must never share a
  token store, because refresh tokens rotate on every use.
- **Freshness becomes visible.** The Vault files carry a render timestamp, and
  the phone is the only writer — a stale brief is now visible in the artifact
  itself rather than requiring a check on a machine that may not have run.
- **The HR-zone model needs re-siting.** `ZONES` currently lives in
  `tools/race_detail.py` and is duplicated as a SQL `CASE` in `html_report.py`.
  Both are Mac-side. The on-device renderer needs its own copy; this is the
  moment to make it one definition rather than a third.
