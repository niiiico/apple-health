# HealthSync (iOS)

On-device producer for the incremental-sync path
([ADR-002](../docs/adr-002-incremental-sync.md)). It reads new HealthKit data
since the last anchor and writes [delta files](../docs/delta-contract.md) into
its iCloud Drive folder, where the Mac's `tools/sync_cycle.py` picks them up.

> Transport note: a Box-based transport was built in July 2026 and reverted
> before activation — see [ADR-004](../docs/adr-004-revert-to-icloud-transport.md).
> The app needs **no** Box credentials and no login.

The Xcode project is checked in at `App/App.xcodeproj` with the required
capabilities and Info.plist keys already configured — open it, select your
team, and run on a real iPhone (HealthKit is unavailable in the Simulator).

> **About editor errors:** browsing the Swift files outside Xcode shows red
> diagnostics ("No such module 'UIKit'", cross-file "cannot find X in scope").
> Those are SourceKit using the macOS toolchain on loose files; they resolve
> inside the Xcode target.

## What it does

- `HealthTypes` — the catalogue of observed types and the **fixed unit** each is
  read in (must stay constant; the ingest side adds across deltas).
- `AnchorStore` — persists one `HKQueryAnchor` per type in `UserDefaults`.
- `SyncEngine` — runs `HKAnchoredObjectQuery` per type, folds dense samples into
  per-day buckets, exports routes, builds a `Delta`, writes it, **then** advances
  anchors (so a failed sync just retries the same window).
- `RouteExporter` — `HKWorkoutRouteQuery` → GPX 1.1 matching Apple's
  `workout-routes/*.gpx` so `parse_gpx` reads it unchanged.
- `DeltaWriter` — writes sidecars (route GPX, per-workout HR-series CSV) first,
  then the JSON, atomically, into
  `iCloud.net.dev2.healthsync/Documents/HealthSync/`. The local write **is**
  the durable write (iCloud uploads it for us), so anchors advance as soon as
  it returns — no outbox, no network in the path. The HR series feeds
  `tools/session_detail.py`; it is not ingested into the DB.
- `AppDelegate` — registers a daily `BGAppRefreshTask`; `ContentView` adds a
  manual "Sync now" button and a "Backfill HR series" button (below).

## Backfill HR series

Deltas written by app versions older than 2026-07-11 carried no
`hr-<uuid>.csv`, and anchors have advanced past those workouts, so a normal
sync can never re-emit them. The backfill button repairs this: it re-queries
every workout since `bootstrapCutoff` with a plain (non-anchored) query and
writes only the *missing* HR CSVs — files already in the iCloud folder are
skipped, including ones present only as not-yet-downloaded `.icloud`
placeholders. It writes no delta JSON and touches no anchors,
which is safe precisely because HR CSVs are never ingested into `health.db`
(see the backfill exception in
[delta-contract.md](../docs/delta-contract.md)). Idempotent; run it whenever
`tools/session_detail.py` reports missing series.

## Project configuration (already in the repo)

These live in the project so a fresh checkout builds a working app; listed here
because each one is load-bearing:

- **Bundle id** `net.dev2.healthsync` — must stay the reverse-DNS prefix of
  `AppDelegate.refreshTaskID` (`net.dev2.healthsync.refresh`) or the background
  task silently never runs.
- **`App/App.entitlements`** — HealthKit + iCloud Documents with container
  `iCloud.net.dev2.healthsync`. The delta folder is
  `<container>/Documents/HealthSync/`.
- **`App/Info.plist`** — `NSHealthShareUsageDescription` (without it, HealthKit
  authorization raises an NSException), `BGTaskSchedulerPermittedIdentifiers`
  (without it, `BGTaskScheduler.register` crashes at launch), `UIBackgroundModes`
  (`fetch`), and `NSUbiquitousContainers` (makes the container visible in
  iCloud Drive / Files).
- **`SyncEngine.bootstrapCutoff`** — see below.

## Signing (one-time)

Select your team under Signing & Capabilities. With automatic signing, Xcode
registers the App ID with the HealthKit capability and creates the iCloud
container on first build (needs a paid developer account for iCloud; a free
Apple ID also limits sideloads to 7 days).

## Shipping a build to the phone

Bumping `CURRENT_PROJECT_VERSION` changes nothing on the phone by itself. The
OTA server serves the newest `.ipa` under `distrib/`, which is gitignored and
starts out absent — so a fix can be committed, correct and entirely unshipped.
That is not hypothetical: build 45 fixed the humidity scaling on 2026-08-29 and
was still not installed a week later, because nothing between the commit and
the phone ever ran.

```bash
zsh scripts/build_ipa.sh      # archive + ad-hoc export into distrib/
uv run python tools/ota/ota_server.py
```

Then open `https://<mac-ip>:8443/` in Safari on the iPhone and install.

**Confirm the build actually arrived**, rather than assuming the install took:
every delta carries `app_version` as `"1.0 (46)"`, so

```bash
python -c "import json,sys;print(json.load(open(sys.argv[1]))['app_version'])" \
    "$(ls -t /Volumes/nicolas-data/HealthData/healthsync-inbox/delta-*.json | head -1)"
```

A bare `"1.0"` means a build older than 45 — the build number was itself one of
the things 45 added, so its absence is the signal.

## Bootstrap cutoff — why the first sync is small

`health.db` is built by `ah-build` from a full export; deltas are merged on top
**additively**, so the app must never re-emit history the export already
covers. `SyncEngine.bootstrapCutoff` (currently **2026-06-29**, the day the
export the DB was re-based on was taken) excludes everything older from every
query. This also keeps the anchor-less first sync to weeks of samples instead
of the entire HealthKit history (which would risk an out-of-memory kill).

## Re-basing on a newer full export

1. Rebuild the DB with `ah-build`; set `bootstrapCutoff` to the day the export
   was taken; delete or archive already-applied delta files; reinstall the app
   so anchors reset.
2. **Boundary day:** the export's taken-day is *partial* in the rebuilt DB,
   while the first delta carries *full-day* buckets for it. Before the first
   ingest, delete that day's `daily_metrics` rows for the types the delta
   carries so the complete buckets replace them (done by hand for the
   2026-06-29 bootstrap; see `tmp/filter_bootstrap_delta.py` from that run).
3. The first `ah-ingest` into a freshly built DB needs `--force` (the guard
   cannot know the deltas only carry post-cutoff data — with this app's
   cutoff, they do). The guard lifts after the first applied delta.

## Where the files land

The app writes to `iCloud.net.dev2.healthsync/Documents/HealthSync/`, visible
on the Mac at
`~/Library/Mobile Documents/iCloud~net~dev2~healthsync/Documents/HealthSync/`.

On the Mac, `tools/icloud_fetch.py` mirrors new files into the durable inbox
`/Volumes/nicolas-data/HealthData/healthsync-inbox/`, which is what
`ah-ingest --inbox`, `session_detail` and `vault_push` read;
`tools/sync_cycle.py` chains all of it (launchd plist in `tools/launchd/`).
The inbox is a separate copy on purpose: iCloud evicts cold local files, and
the HR-series CSVs must stay readable long after their delta was ingested.

## Notes & limitations

- **Deletions** of dense samples / sparse records are not subtracted downstream
  (HealthKit only gives a UUID on delete) — see the contract. Workout deletes
  apply exactly. A periodic full `ah-build` reconciles.
- **Unit stability is load-bearing.** Never change a type's unit in
  `HealthTypes` on an existing install without a full rebuild, or sums drift.
- **Background timing is best-effort.** iOS decides when `BGAppRefreshTask`
  actually runs (needs the app backgrounded, often charging). The manual button
  is the reliable path; the background task is opportunistic top-up.
