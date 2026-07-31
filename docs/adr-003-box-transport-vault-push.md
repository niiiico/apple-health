# ADR-003 — Box as sync transport + automated Vault push

Date: 2026-07-14 · Status: **partially superseded** by
[ADR-004](adr-004-revert-to-icloud-transport.md) (2026-07-31)

> **The transport decision below was reverted.** It was implemented but never
> activated, and iCloud Drive is the delta transport again. The *automated
> Vault push* half of this ADR remains in force — `vault_push.py`,
> `box_client.py` and `box_auth.py` are live, because the Claude Vault is on
> Box regardless of transport. The transport code is archived at tag
> `box-transport-v1`. Read ADR-004 for why, and the *Durability (producer)*
> section below for the part still worth keeping if Box ever returns.

## Context

Since ADR-002 the HealthSync iOS app has written delta files to an iCloud
Drive folder, which the Mac reads via file sync (`ah-ingest`). Separately, the
Box "Claude Vault" holds curated training summaries (`sport-week-current.md`,
`sport-natation-sessions.md`, …) that were rendered on the Mac but uploaded
**manually** through an interactive Box MCP connector during Claude sessions.

Two dots were unconnected:

1. The transport (iCloud) is opaque — no API access from scripts, propagation
   lag is unobservable, and the inbox only exists on machines signed into the
   user's iCloud account.
2. The Vault only updates when a human (or Claude in an interactive session)
   pushes to it.

## Decision

**Box replaces iCloud Drive as the delta transport, and a Mac-side pipeline
automates ingest and the curated Vault push.**

- The iOS app uploads delta JSON + sidecars (route GPX, HR CSV) directly to a
  **`HealthSync/` folder at the Box root** — *not* inside the Vault. Raw data
  stays out of the Vault by design (its convention is 100–500-token curated
  files).
- The Mac runs `tools/sync_cycle.py` (launchd, periodic):
  `box_fetch` (download new inbox files) → `ah-ingest` → `session_detail`
  render → `vault_push` (rolling per-discipline session files + weekly brief
  into the Vault, `_changelog.md` append, `_map.md` rows ensured).
- Vault session files are **volatile rolling windows** (last 5 sessions per
  discipline, regenerated from `health.db` + inbox series on every push).
  The durable archive is `data/sessions/` on disk plus the DB — both
  git-ignored, like all of `data/` (personal data never goes in git).

## Auth model

One Box Platform "Custom App" (OAuth 2.0 user auth), two independent grants:

- **iPhone** — in-app `ASWebAuthenticationSession` login once; refresh token
  in the Keychain. Box refresh tokens are single-use and rotate on every
  refresh; they expire after 60 days *unused* — any sync inside that window
  keeps the chain alive indefinitely.
- **Mac** — `tools/box_auth.py` one-time localhost-callback login; tokens in
  `~/.config/apple-health/box_tokens.json` (0600), rotated on every run.

Client id/secret are embedded per device (personal, single-user app — the
secret protects nothing beyond the user's own account here).

## Durability (producer)

iCloud gave free durability: a local write *was* the durable write. Box makes
publishing a network call, so the app uses an **outbox**: files are written to
local Documents first (sidecars, then JSON — same ordering rule as the
contract), then uploaded **sidecars before delta JSONs, deltas oldest-first**,
each local file deleted only after its upload succeeds.

**Anchors advance on the local outbox write, not on upload.** The outbox is
the durability gate: staged files are retried on every subsequent sync, so
delivery is guaranteed without ever re-querying a window — gating anchors on
the upload instead would re-emit the same samples into a new delta after a
transient network failure, double-counting `daily_metrics` (the merge is
additive; see ADR-002). The consumer still never sees a delta before its
sidecars, because within every drain all sidecars upload before any JSON.

## Consequences

- `ah-ingest`, `session_detail`, `race_detail` keep reading a **local inbox
  directory**; only its source changes (Box download instead of iCloud sync).
  The inbox moves to `/Volumes/nicolas-data/HealthData/healthsync-inbox/`;
  existing iCloud inbox files are copied there once at cutover.
- A sync from the phone now requires network + a valid Box token; the outbox
  makes failures safe but delayed. iCloud code paths are removed (no
  fallback), per project practice.
- The Vault gains automated writers. To keep the append-only changelog
  readable, `vault_push` appends at most one line per day and only when
  content actually changed.
- Rotating refresh tokens mean two devices must never share a token store —
  hence the two independent grants.
- Recovery: if a token chain dies (>60 days idle), re-run the one-time login
  on that device. If the Box folder is lost, the app cannot re-emit applied
  deltas (anchors advanced) — the recovery path remains a full `ah-build`
  re-base (ADR-002), unchanged.
