# ADR-007 — The phone posts to the cluster; the Mac leaves the path

- Status: **accepted**
- Date: 2026-08-26
- Completes the topology [ADR-006](adr-006-sinks-are-plugins.md) corollary (a)
  implies. Retires the transport chosen in
  [ADR-004](adr-004-revert-to-icloud-transport.md), and with it the delta
  *file* semantics of [ADR-002](adr-002-incremental-sync.md).

## Context

ADR-005 named the right target — remove the Mac — and reached for it by moving
rendering onto the phone. ADR-006 replaced that with a sounder frame: sources
and derivations are the stable layer, where data lands is a plugin, and ingest
runs as a pod. What ADR-006 did not move is the one thing the phone→Mac hop
still owns: **deltas arrive in iCloud Drive, and only a Mac can read an iCloud
Drive.**

So the pipeline today is phone → iCloud → **Mac** → Postgres on ras12. The store
moved off the Mac; the transport did not. `ah-sync` still runs on a laptop,
by hand — the launchd agent was confirmed never installed — and every incident
in this project's record sits in that hop, never in HealthKit:

- deltas `0008`–`0009` lost during the Box detour, never applied;
- delta `0007` stuck 17 days as an unmaterialised iCloud placeholder;
- delta `0012` sat in iCloud a full day because nothing invoked `ah-fetch`;
- the Jul 15–31 window, 17 days stale with no signal that anything had stopped.

Three routes out were examined before choosing.

**iCloud from Linux has no supported form.** rclone's `iclouddrive` backend is
reverse-engineered web-service access: it takes the Apple ID password itself
(app-specific passwords are rejected) and mints a trust token valid **30 days**,
after which a human completes an interactive 2FA. A transport that requires a
person every 30 days is a scheduled outage, in a project whose entire failure
record is unattended staleness. It is additionally unclear whether a container
belonging to an app that never went through App Store review appears in the web
drive tree rclone reads at all — the known caveat for third-party containers,
and untested here.

**CloudKit Web Services is official, Linux-friendly, and cannot help.** It
addresses CloudKit record zones, not the ubiquity Documents container, so the
delta files are not reachable through it without rewriting the producer. And a
server-to-server key reaches only the **public** database; the private one
requires an interactive user web-auth token — the same 30-day human, wearing a
different hat. Health data in a public database is not a trade worth costing.

**`/Volumes/nicolas-data` is a local APFS volume** (`/dev/disk3s7`), not the NAS.
The "durable NAS inbox" ADR-003 introduced and ADR-004 deliberately kept has
always been a disk attached to the Mac. Anything that still reads the inbox is
still bound to the Mac — which includes the route GPX every session render
needs.

That last finding settles it. There is no configuration of the current design in
which the Mac is optional, and no way to teach Linux to speak iCloud that does
not reintroduce a human on a timer.

## Decision

**The phone posts delta bundles over the home VPN to an ingest endpoint on k3s,
authenticated by a client certificate terminated at HAProxy. Postgres is the
only store. iCloud Drive, the inbox and the Mac leave the path entirely.**

```
iPhone (HealthSync)                          k3s (ras11/19/24/27)
┌──────────────────────────────┐            ┌────────────────────────────────┐
│ HealthKit → anchored query   │            │ HAProxy — TLS + verify required│
│   ↓                          │  VPN       │   ↓  X-Client-DN               │
│ bundle → outbox (on disk)    │ ──mTLS───▶ │ ah-serve  /v1/deltas           │
│   ↓  anchors advance HERE    │            │   ↓  merge (ah-pgsync path)    │
│ drain: POST /v1/deltas       │            │ Postgres @ ras12 — sole store  │
│   ↑ status surfaced in the UI│ ◀──ack ────│   observed_through echoed back │
└──────────────────────────────┘            └────────────────────────────────┘
```

### a. Transport: one bundle, one request

A sync run POSTs a single multipart request carrying the delta JSON and every
sidecar it references. The delta *contract* — schema, merge semantics, sparse
allowlist, day bucketing — is unchanged; only its carrier is.

This closes a hole ADR-004 accepted knowingly. Under a shared folder, iCloud
gives no propagation-order guarantee, so a delta could become visible before its
route GPX; `ah-ingest` then warned `! route file missing, skipped`, recorded the
delta as applied anyway, and that `routes` row was lost until a full `ah-build`.
One atomic bundle per sync makes that state unrepresentable rather than merely
tolerated.

### b. Durability: outbox on device, anchors advance on the write

ADR-004's strongest argument against a network transport was that iCloud's
durability is free — the local write into the container *is* the durable write,
so anchors may advance the moment it returns. A network publish can fail, so the
property has to be bought back: the bundle is written to a local outbox, anchors
advance **on that write, not on the POST**, and a separate drain uploads.

This is ADR-003's design, and it is recovered rather than re-derived — ADR-004
explicitly preserved it for this case ("if Box, or any API-addressable
transport, is revisited, the outbox design is the part worth re-reading, and
would apply unchanged"):

    git show box-transport-v1:ios/App/App/BoxClient.swift

The OAuth half is discarded; the outbox, the drain ordering and the anchor
invariant are what carry over. Uploads use a default `URLSession` inside the
background task rather than a background-configuration session: client-
certificate challenges on background sessions have sharp edges, the payload is
kilobytes, and the outbox already makes a failed attempt free.

### c. Auth: mTLS, terminated at HAProxy

Ingress goes through **HAProxy**, as the rest of the estate's apps do — not the
cluster's default Traefik. HAProxy binds with `ca-file` set to the internal CA
and `verify required`, so an unauthenticated request never reaches the service,
and forwards the verified subject as `X-Client-DN`. No certificate handling
reaches the Python.

The CA is the one already minted for the OTA install flow (`tools/ota/ca.cnf`)
and already installed and fully trusted on the device, so the trust path is
proven rather than new. The client certificate is long-lived deliberately: a
renewal ritual is exactly the failure mode this ADR exists to delete. It is not,
however, allowed to be silent — the service logs and alarms when `notAfter` is
within 30 days.

### d. Reachability: VPN on the phone

The endpoint stays internal (`health.int.dev2.net` — ADR-006 wrote
`health.int.dev2.com`, which appears to be a slip; the rest of the estate is
`.net`). The only new flow is the phone's own tunnel, so ADR-006 corollary (e)
survives in the form that matters: nothing of ours is exposed publicly.

This choice is not load-bearing. If the tunnel turns out not to survive iOS
background execution, the outbox degrades precisely into "queues while away,
drains on the next contact from home" — later, never lost.

### e. Storage: Postgres, and route points finally land in it

The migration is complete and Postgres is the store, so the inbox does not get
to survive as a second one. The heart-rate series already moved. Route GPX did
not: `routes` records a summary and `n_points`, never the track, while session
km splits read the GPX file directly. As long as that is true, a renderer needs
a filesystem the Mac owns.

So **migration 7 adds `route_points`**; the ingest service stores the track on
arrival, and the 1,352 historical GPX files are bulk-loaded once — from the
Mac's disk, while the Mac is still in a position to read it. This is the step
ADR-006 deferred with "retiring `--inbox` needs route points in the store, which
nothing needs yet". Removing the Mac is what needs it.

The raw `export.xml` and the dated cold archive move to ds2. They are not part
of the ingest path; they are ADR-001's recovery path, and `ah-build` must stay
runnable from a pod.

### f. The phone shows what is going on

iCloud made state invisible in a way that flattered it: a write into the
container looked like success, and whether anything downstream ever consumed it
was unknowable from the device. A network transport has real state, and the app
must show it — the phone is the one screen the operator actually looks at.

- **Freshness** — a traffic light on the server's last acknowledged
  `observed_through`, echoed back in the POST response: green within 24 h,
  amber to 48 h, red beyond.
- **Outbox depth** — bundles written and not yet accepted. Zero is the healthy
  resting state; a number that does not fall is the signal the current design
  is structurally incapable of producing.
- **Last exchange** — when the last POST ran and how it ended, with the failure
  reason kept verbatim rather than flattened to "Sync failed".
- **Reachability** — whether the endpoint answered at all, which separates
  "tunnel down" from "server rejected me".

This is deliberately more than a status line. Every failure in this project's
history was legible in principle and observed by nobody.

### g. Staleness is alarmed server-side as well

A CronJob compares `ingest_runs.observed_through` against now and alerts through
the Prometheus/Grafana already running on the cluster. ADR-004 listed this as
knowingly unresolved; it has since cost three separate incidents. It ships
**first**, ahead of everything else here, because the migration window is when a
silent break is most likely.

## Sequencing

1. **Staleness alarm** (g). Independent of the rest, useful immediately, and
   cover for the migration itself.
2. **`route_points` + bulk load** of the historical GPX — while the Mac is still
   there to read the disk that holds it.
3. **Cold storage to ds2** — raw export and dated archive, so `ah-build` stays
   runnable without the Mac.
4. **Ingest service + HAProxy mTLS**, with the phone posting *in addition to*
   writing iCloud. Both paths run concurrently; `ingest_runs.ref` dedups on the
   delta name, so a week of parallel running costs nothing and proves the new
   path against the old one.
5. **Cutover and delete.** The phone stops writing the container; `sources/
   icloud.py`, `tools/launchd/`, the ubiquity entitlement, the
   `NSUbiquitousContainers` key and `--inbox` all go.

## Consequences

- **The Mac is out of the data path**, and no Apple ID credential, rclone
  remote, or 30-day re-auth enters the cluster to replace it.
- **The sidecar-ordering hole closes structurally** rather than being documented
  and tolerated (a).
- **Unreliability moves; it does not vanish.** `BGAppRefreshTask` is best-effort
  — a phone that is rarely opened may not run one for days, where a healthy Mac
  on launchd would have run every 30 minutes. The mitigation is (f) and (g)
  together: the new failure is *loud* where every previous one was silent. This
  trades automatic cadence for observability, knowingly.
- **A new inbound listener exists**, on the internal network, behind mTLS,
  reachable only over the tunnel. ADR-006 corollary (e) said "outbound-only
  egress, no inbound exposure"; this narrows it to *no public inbound exposure*.
  Worth stating plainly rather than claiming nothing changed.
- **The delta contract survives; its file semantics do not.** Immutability,
  ascending-filename ordering and "sidecars written first" were properties of a
  shared folder. Ordering and grouping become properties of one request;
  `ingest_runs.ref` keeps the delta name as the idempotency key, so replay
  safety is unchanged.
- **`--inbox` finally retires** — not by decree, but because the last thing
  reading it (route track points) now has a home in the store.
- **Reversible.** The iCloud path is removed by deletion only and recoverable
  from git; the raw export is untouched and `ah-build` reconstructs the dataset
  from it, exactly as ADR-001 intends.
