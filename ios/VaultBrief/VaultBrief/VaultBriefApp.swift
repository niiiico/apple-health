import SwiftUI

/// VaultBrief — renders the curated training Vault on-device and pushes it to
/// Box (ADR-005).
///
/// Replaces the HealthSync → iCloud → Mac → Box chain with a single hop. No
/// delta files, no anchors, no inbox, no database: HealthKit is queried
/// directly and the only thing that leaves the phone is the markdown itself.
@main
struct VaultBriefApp: App {
    var body: some Scene {
        WindowGroup { ContentView() }
    }
}
