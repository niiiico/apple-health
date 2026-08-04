# VaultBrief

Renders the curated training Vault **on the phone** and pushes it to Box.
See [ADR-005](../../docs/adr-005-on-device-processing.md).

This replaces the HealthSync → iCloud → Mac → Box chain with a single hop.
There are no delta files, no anchors, no inbox and no database: HealthKit is
queried directly, and the only thing that leaves the phone is the markdown.

```
HealthKit ──▶ render markdown ──▶ Box
   (on device, no intermediate representation)
```

## Sources

| File | Role |
|---|---|
| `VaultBrief/Zones.swift` | HR zone model + per-workout distribution/drift. **The single definition** — the Mac still spells it out twice. |
| `VaultBrief/VaultRender.swift` | The four Vault files, as pure functions over value types. No HealthKit import: this is what makes the parity check possible. |
| `VaultBrief/HealthQueries.swift` | Everything that touches HealthKit. Bounded windows only. |
| `VaultBrief/VaultRefresh.swift` | Orchestration: query → render → upload what changed. |
| `VaultBrief/BoxClient.swift` | OAuth + upload, recovered from `box-transport-v1` and retargeted from deltas to markdown. |
| `Parity/` | Host-side check that the Swift renderers match the Python ones. |

## Before it runs

1. **Box credentials.** Create a Box Platform "Custom App" (OAuth 2.0 user
   authentication), grant *Read and write all files and folders*, add
   `vaultbrief://box-auth` as a redirect URI, and fill in `BoxConfig.clientID`
   / `clientSecret`. These are still `REPLACE_WITH_…` placeholders — the same
   gap that left ADR-003 built but never activated for two weeks, so treat an
   unconfigured build as non-functional rather than idle.
2. **Xcode project.** The sources are complete and typecheck against the iOS
   SDK, but there is no `.xcodeproj` yet — create an iOS App target named
   `VaultBrief`, add `VaultBrief/*.swift`, set the Info.plist and entitlements,
   and enable the HealthKit capability.

## Staging

`BoxConfig.useStaging` is `true`, so uploads go to a `Vault-next/` folder at the
Box root rather than the live Vault. Both this app and `tools/vault_push.py`
render the *same four file names*; pointing both at the Vault would have them
overwrite each other on every run.

Flip `useStaging` to `false` at cutover, once the Mac pipeline is retired.

## Parity check

```bash
ios/VaultBrief/Parity/check.sh [YYYY-MM-DD]
```

Renders all four files with the Python pipeline and with the app's own Swift
renderers, then diffs. Passes today against the live `health.db`; only the two
provenance lines differ, and the check filters exactly those. Any other diff is
a real divergence.
