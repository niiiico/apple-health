import SwiftUI

/// Minimal UI: authorise once, then a manual "Sync now" button plus the last
/// status line. The real work happens unattended via the background task.
struct ContentView: View {
    private let engine = SyncEngine()
    @State private var status = "Ready."
    @State private var busy = false
    @AppStorage("anchor.seq") private var lastSeq = 0

    var body: some View {
        VStack(spacing: 24) {
            Image(systemName: "heart.text.square")
                .font(.system(size: 64))
                .foregroundStyle(.pink)
            Text("HealthSync").font(.largeTitle.bold())

            Text(status)
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)
                .padding(.horizontal)

            Button {
                Task { await runSync() }
            } label: {
                Label(busy ? "Syncing…" : "Sync now", systemImage: "arrow.triangle.2.circlepath")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(busy)
            .padding(.horizontal, 40)

            Button {
                Task { await runBackfill() }
            } label: {
                Label("Backfill HR series", systemImage: "waveform.path.ecg")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered)
            .disabled(busy)
            .padding(.horizontal, 40)

            if lastSeq > 0 {
                Text("Last sync #\(lastSeq)").font(.footnote).foregroundStyle(.tertiary)
            }
        }
        .padding()
        .task {
            do { try await engine.requestAuthorization(); status = "Authorised. Tap to sync." }
            catch { status = "Authorisation failed: \(error.localizedDescription)" }
        }
    }

    private func runSync() async {
        busy = true
        defer { busy = false }
        do { status = try await engine.sync() }
        catch { status = "Sync failed: \(error.localizedDescription)" }
    }

    private func runBackfill() async {
        busy = true
        defer { busy = false }
        do { status = try await engine.backfillHRSeries() }
        catch { status = "Backfill failed: \(error.localizedDescription)" }
    }
}

#Preview {
    ContentView()
}
