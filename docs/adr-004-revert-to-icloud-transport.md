# ADR-004 — Revert the delta transport to iCloud Drive; keep the Vault push

Date: 2026-07-31 · Status: accepted, but **made moot** by
[ADR-005](adr-005-on-device-processing.md) (2026-08-04) · Supersedes the
transport half of [ADR-003](adr-003-box-transport-vault-push.md)

> **Read [ADR-005](adr-005-on-device-processing.md) first.** This ADR settles
> *which pipe* carries deltas from phone to Mac. ADR-005 removes the Mac from
> the path entirely, so the question stops applying at VaultBrief cutover —
> until then, the decision here is what runs.

## Context

[ADR-003](adr-003-box-transport-vault-push.md) bundled two changes that only
looked like one:

1. **Transport** — Box replaced iCloud Drive as the path from phone to Mac.
2. **Automated Vault push** — the Mac renders and uploads the curated Claude
   Vault files instead of a human doing it through the Box MCP connector.

The code for both was written on 2026-07-14 and **never activated**: the Box
Platform app was never created, so `BoxConfig` kept its
`REPLACE_WITH_CLIENT_ID` placeholders, no Mac token store was bootstrapped,
and the launchd agent was never loaded. The phone kept running the previous
iCloud build.

The cost of that gap was quiet and real. The last delta the Mac ingested was
`0006` (2026-07-14). The phone wrote `delta-…0007.json` to iCloud on 07-15 and
nothing after; `health.db` sat 17 days stale until 07-31 with no signal that
anything had stopped. A transport that needs manual activation is a transport
that can sit disconnected indefinitely.

Re-examining ADR-003's premises against that experience:

- **"iCloud is opaque."** True, and it cost real debugging time here: delta
  `0007` was an undownloaded `.icloud` placeholder that could not be read on
  demand. But this is a *tractable* problem (materialise on read, retry next
  cycle) rather than a reason to change transports.
- **"Box unifies transport and destination."** The unification was thinner
  than it looked. The Vault push needs Box because the *Vault* is on Box; the
  transport needed Box only by choice. Sharing a client library is not the
  same as sharing a requirement.
- **What ADR-003 undervalued:** iCloud's durability is free. A local write
  into the ubiquity container *is* the durable write. Box made publishing a
  network call, which is why the app grew a local outbox, a drain ordering
  rule, and the subtle "anchors advance on the outbox write, not the upload"
  invariant. That machinery existed **only** to buy back a property iCloud
  gave for nothing.

## Decision

**iCloud Drive is the delta transport again. The automated Vault push stays.**

- The iOS app writes delta JSON + sidecars directly into
  `iCloud.net.dev2.healthsync/Documents/HealthSync/`, sidecars first, JSON
  last, atomically. The outbox, the drain, and `BoxClient.swift` are gone;
  anchors advance as soon as the local write returns.
- `tools/icloud_fetch.py` replaces `tools/box_fetch.py`. It mirrors new files
  into the durable inbox at `/Volumes/nicolas-data/HealthData/healthsync-inbox/`,
  keeping the ADR-003 inbox location — iCloud evicts cold local copies, and
  `session_detail` needs HR CSVs readable long after ingest. It preserves the
  sidecars-before-deltas copy order, and each file is copied to a temp name
  and atomically renamed, so an interrupted copy can never leave a truncated
  file that later runs would treat as complete.
- **Eviction here is dataless-file based** (verified: `SF_DATALESS` set, real
  directory entries, no `.icloud` files in the container). An evicted file
  keeps its name and true size, and reading it faults the data back in — so
  copying *is* the materialisation. The legacy `.<name>.icloud` plist
  representation is handled defensively but is not what this container uses;
  in that representation the real name has no directory entry, so the copy
  fails and the file is retried next cycle rather than downloaded.
- `sync_cycle.py` is otherwise unchanged: `icloud_fetch` → `ah-ingest` →
  `session_detail` → `vault_push`.
- **Box remains a destination, not a transport.** `box_client.py`,
  `box_auth.py` and `vault_push.py` stay, because the Claude Vault lives on
  Box regardless. One Box login is still required, on the Mac only; the phone
  needs none.

## Archive

The Box transport is not deleted, only removed from `main`. It is preserved as
built at commit `23f38fc`, reachable via branch `archive/box-transport` and
tag `box-transport-v1`. To inspect it:

    git show box-transport-v1:ios/App/App/BoxClient.swift
    git show box-transport-v1:tools/box_fetch.py

If Box (or any API-addressable transport) is revisited, the outbox design in
ADR-003's *Durability (producer)* section is the part worth re-reading — it is
the correct answer to "the publish step can fail" and would apply unchanged.

## Consequences

- **The transport works today** rather than after a setup ritual: the phone
  needs no credentials, no login, and no network permission beyond iCloud.
- **The Jul 15–31 gap is recoverable.** The installed build's anchors only
  advanced through `0007`, so the next sync from a build with unchanged
  `AnchorStore` semantics emits everything since. This holds only if the app
  is *upgraded in place* — a delete-and-reinstall drops the anchor store with
  the app container and forces a full `ah-build` re-base (ADR-002).
- **iCloud's opacity is contained, not solved.** `icloud_fetch` cannot force a
  download; a file that will not materialise is skipped and retried next
  cycle. It does return non-zero on a hard failure (missing source folder,
  failed copy) and `sync_cycle` propagates that instead of printing
  "nothing new", so a dead transport no longer reads as a healthy cycle.
- **Sidecar ordering is weaker than under Box.** ADR-003's drain uploaded all
  sidecars before any delta, so the transport folder never held a delta
  without its files. iCloud gives no propagation-order guarantee, and
  `icloud_fetch` can only order files already visible on the Mac. If a delta
  lands before its route GPX, `ah-ingest` prints `! route file missing,
  skipped` yet still records the delta in `applied_deltas`, so that `routes`
  row is lost until a full `ah-build`. Blast radius is small — `routes` only
  feeds `report.py`, and `session_detail` reads GPX by uuid and re-renders 14
  days each cycle — but it is silent. This is a restoration of the ADR-002
  behaviour, not a new regression. Deferring a delta whose non-null
  `route_file` is absent would close it; not done here.
- **Staleness is still silent.** Nothing yet warns when the newest applied
  delta is days old — the actual failure mode of the last two weeks. A
  freshness check in `sync_cycle` is the obvious follow-up and is deliberately
  left out of this ADR.
- Two ADR-003 artefacts stay live and keep their rationale: the durable NAS
  inbox, and the rolling per-discipline Vault files.
