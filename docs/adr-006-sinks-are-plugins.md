# ADR-006 — The sink is not the architecture

**Date:** 2026-08-26
**Status:** Accepted
**Supersedes** the topology of [ADR-002](adr-002-incremental-sync.md) and makes
the disagreement in [ADR-003](adr-003-box-transport-vault-push.md),
[ADR-004](adr-004-revert-to-icloud-transport.md) and
[ADR-005](adr-005-on-device-processing.md) a configuration choice rather than a
rewrite.

## Context

Four consecutive ADRs argued about where the data finally lands — iCloud, then
Box, then iCloud again, then rendered on the phone. Each one rewrote the
pipeline, because in this project the destination *was* the architecture. None
of them changed what the data is or how it is derived, and none of them made the
analysis better.

Meanwhile the actual work — Claude reading the pushed artifacts and writing
training judgment back — has been happening in the Vault the whole time, by
hand. Reading what it wrote there is what settled this decision.

**1. A rendered artifact with no coverage boundary produced a wrong training
conclusion that survived a month.** From `sport-triathlon-kujukuri-log.md`:

> La note antérieure « vélo = 0 » était une erreur de données : le snapshot
> d'alors s'arrêtait au 07-10, avant le brick du 11.

`sport-week-current.md` covered through 07-10. The 48.5 km brick was 07-11.
Cycling was recorded as the primary gap, that assessment propagated into the
Phase 3 priorities, and it was only corrected on 12-08 when a fresh export
landed. The artifact never said what window it covered, so there was nothing to
hedge against. The instant needed to say so — the delta's `generated_at` — was
in every file we ingested and was discarded.

**2. The significance thresholds delete real training.** The same journal
carries a caveat about its own instrument:

> Les séances sous les seuils de capture (nat <2 km, course <10 km, vélo <30 km)
> ne sont parfois connues que par échange, sans données précises.

A floor intended to keep a weekly brief short became a floor on what the record
contains.

**3. The pre-rendered view is narrower than the analysis needs.** The August
swim entries in the journal carry zone *durations*, per-100m pace, recovery HR
and lap splits — `splits 1300/1400/1500 m à 2'17 / 2'12 / 2'15`. None of that is
in any file this project produces; it arrived by hand. The threshold pace target
now in the plan was validated against numbers the pipeline cannot supply.

**4. Opening the network inbound is not neutral.** A remote MCP server would
give Claude live access, at the cost of a public hostname, a tunnel, an
authorisation server and a homelab that must be up at the moment it is least
convenient for it not to be — five minutes before a race, on another continent.

**5. The house already solved this.** `tvledger` states it as a design property
in [its ADR-001](../../tvledger/docs/adr-001-ingest-not-tracker.md): the
expensive work is ingest and matching, and the tracker is a replaceable sink
behind a one-method protocol. Its own sink reversal cost it one module.

## Decision

**Sources and derivations are the architecture. Where the data lands is a
plugin chosen at runtime.**

```
sources ──▶ store ──▶ derive ──▶ sinks
(export, healthsync)  (postgres)  (zones,   (box brief, html report,
                                   cadence,  session files, advisor)
                                   drift)
```

A new destination is a file under `sinks/`, not an ADR.

### Corollaries

**a. Postgres, not SQLite.** Ingest and the interaction layer run as pods on
k3s. SQLite's single-file model makes the data a property of one machine, and
its locking over NFS is not something to rely on. Same reasoning, and the same
conclusion, as tvledger's ADR-006.

**b. Coverage is a recorded fact.** `ingest_runs.observed_through` stores the
instant HealthKit was queried up to. Every query response carries it, and states
explicitly when a requested range extends past it. `max(started_at)` is not a
substitute — it reports the last workout, which during a rest week is not the
same question.

**c. Zone numbers travel with the model that produced them.** HR zones are
defined on the watch, and HealthKit exposes them only for live workout building
(`HKLiveWorkoutZoneUpdate`) — there is no read-back of historical boundaries. So
the bands cannot be discovered; they can only be stated. Every response carries
them, because "Z3 4:52" is meaningless without them.

**One model, not a dated series — revised 2026-08-27.** This originally
specified dated models with an `effective_from`, and `hr_zone_models` was built
for it. That was machinery for a change that has not happened: the boundaries
have not moved, and there is one set of bands for the whole record, in
`derive/zones.py`.

It was also actively harmful. The dated model was looked up and *reported* but
never reached the arithmetic — `summarize()` and `zone_durations()` classify via
the module constant, and take no bands argument. So recording a model would have
printed one set of bands above numbers computed with another: a stated basis
that is not the basis, which is this ADR's own failure mode arriving through the
feature meant to prevent it. The write path is removed rather than left as a
trap, and a test now pins that the reported bands are the ones that classify.

`hr_zone_models` stays in the schema, empty and unread, for the day the
boundaries actually change. Populating it means first making `derive.zones`
classify by it. Until then the honest cost is stated plainly: if the watch's
bands change, every figure becomes wrong for sessions before the change, with
nothing to signal it.

**d. Store primitives, derive summaries.** HR samples, laps and route points go
into the database; zone distributions, drift and cadence are computed at query
time and never stored. A stored percentage is only valid for the zone model that
produced it, and the raw series must survive a model change. This is ADR-001's
"the DB is a disposable projection" applied one level down.

**e. Analysis runs inside; only conclusions leave.** A job on k3s queries
Postgres locally and drives a Claude tool-use loop whose tools execute *within*
the network. The only new flow is outbound HTTPS. This gives the interaction
pattern of a remote MCP server with the opposite network direction — no inbound
exposure, no tunnel, no authorisation server. An MCP sink remains available
later as one more plugin, on evidence, not as a precondition.

**f. Box is a delivery channel, not a store.** It receives rendered briefs so
they are readable from a phone with nothing of ours in the path. It holds no
health record. The plan and log move into `documents` and Box gets copies, so
the channel stays one-directional.

**g. An internal interaction layer supplies what sensors cannot.** At
`health.int.dev2.com`, on k3s: session and period notes — *piscine indisponible*,
*pas la force pour le vélo*, *benchmark non concluant* — and the zone-model
timeline. Without it the advisor re-derives from numbers alone and reproduces
exactly the confident-wrong pattern this ADR exists to stop. `tvledger`'s review
queue is the same organ.

**h. No thresholds in the query layer.** Brevity is the renderer's concern.

## Sequencing the cutover

The readers and the writer move independently, and in that order they are safe;
reversed, they are not.

1. **Reader seam** *(done)* — `sources/hr_series.py` provides samples from
   either the inbox sidecars or `hr_samples`, and both renderers take a provider
   instead of parsing a CSV. Verified byte-identical on real data across three
   Vault files and twenty-one session files.
2. **Writer cutover** *(done)* — `ah-pgsync` applies pending deltas to
   Postgres with the same merge semantics as `sources.healthsync`, keyed on
   `ingest_runs.ref` for per-file idempotency, and `ah-sync` runs it before
   anything renders. Verified by replaying all sixteen deltas into an empty
   database: 17,269 HR samples across 28 sessions and identical
   count/sum/min/max on spot-checked days, matching what `ah-migrate` produced
   from SQLite by a completely different path.
3. **Readers prefer Postgres** *(done)* — `default_source()` returns
   Postgres-with-inbox-behind-it when a DSN is configured. The fallback is
   load-bearing rather than defensive: a workout `ah-pgsync` has not reached yet
   still renders from its sidecar instead of losing its zones, so the answer is
   never worse than the inbox-only behaviour it replaces.

4. **The interaction layer is deployed** *(done)* — `ah-web` runs on k3s at
   <https://health.int.dev2.net>, behind an oauth2-proxy sidecar and Authelia,
   with a certificate from the internal CA. It is the only piece of this that
   an iPhone can reach, which is the whole point of (g): the facts no sensor
   produces have to be enterable from wherever you are when you remember them.

**`--inbox` does not disappear.** Route GPX still lives there, and only the
heart-rate half moved. Claiming the parameter had gone would be the same kind of
overstatement as "the zone model is defined once" was before the review caught
it. Retiring it needs route points in the store, which nothing needs yet.

### The database connection is mutually authenticated

`sslmode=verify-full` with a client certificate: we verify the server — chain
and hostname, so it proves which server answered — and the server verifies us.
`require` and `verify-ca` encrypt without proving the former; do not downgrade to
them to make something connect.

An earlier draft of this section said the connection was stuck on `require`
until a client certificate could be issued. That was wrong, and worth keeping
because the error is easy to repeat: `verify-full` describes how *this* end
verifies the *server* and needs only the root CA, which was already mounted for
oauth2-proxy. A client certificate is the other direction. Conflating them made
a change that needed nothing look like it needed a passphrase and a window.

Two things this turned up that were not obvious:

- **The certificate is held by the pod *and* the Mac**, because Postgres matches
  the CN against the role name — a second certificate for `apple_health` would
  carry the same CN and be the same identity. So the `pg_hba` rule requiring a
  certificate applies to the Mac as well, and the Mac is what runs ingest.
  Applying it while the Mac still connected on `sslmode=require` would have
  stopped `ah-pgsync` and left Postgres quietly behind SQLite — corollary (b)'s
  failure exactly, arriving through the door marked "security hardening".
- **It expires 2028-01-09 and does not auto-renew.** Client certificates have no
  ACME path. This is a real maintenance obligation, and the honest cost of the
  decision rather than a footnote.

## Consequences

- **The 002–005 argument ends without any of them being wrong.** Each was a
  correct answer to "which pipe", asked at a time when the pipe was load-bearing.
- **The phone keeps a job, and a smaller one.** Only an iOS app can read
  HealthKit — verified: `HKHealthStore.isHealthDataAvailable()` is `false` on
  macOS, where the framework ships for Mac Catalyst. The exporter reads
  HealthKit and posts to the ingest endpoint. No OAuth, no rendering, no delta
  contract to keep in sync.
- **We give up live arbitrary questions from the phone while away from home.**
  The brief answers what it anticipated; anything else waits for the LAN or the
  next run. This is the price of (e) and it is accepted deliberately.
- **The zone model is a thing that must be maintained by hand.** Nothing can
  detect a change to the watch's bands, so a change that is not noticed silently
  mis-classifies every session before it. There is no mitigation in the code —
  only the fact that the bands are printed on every surface, so a figure that
  stops matching the watch is at least visible.
- **`sources/healthsync.py` and the phone exporter owe laps.** The tool contract
  specifies them because the coaching already depends on them; until the source
  provides them, `has_laps` is `false` and honest.
- **The interaction layer has no authentication code, and must not grow any.**
  The oauth2-proxy sidecar is the only thing its Service publishes, so the login
  is mandatory by topology rather than by discipline. Two details carry that:
  `ah-web` binds loopback, and the Service's `targetPort` is the sidecar.
  Changing either — including "temporarily", to debug something — publishes a
  write API that takes no credential to the whole cluster.
- **Reversible.** The raw export on the NAS is untouched and `ah-build`
  reconstructs from it. The dated cold archive remains the recovery path.
