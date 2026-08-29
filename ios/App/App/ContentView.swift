import SwiftUI

/// Minimal UI: authorise once, then a manual "Sync now" button plus the last
/// status line. The real work happens unattended via the background task.
struct ContentView: View {
    private let engine = SyncEngine()
    @State private var status = "Ready."
    @State private var busy = false
    @State private var showRangeBackfill = false
    @AppStorage("anchor.seq") private var lastSeq = 0

    var body: some View {
        VStack(spacing: 24) {
            Image(systemName: "heart.text.square")
                .font(.system(size: 64))
                .foregroundStyle(.pink)
            Text("HealthSync").font(.largeTitle.bold())
            // The build, on screen. Without it there is no way to tell an
            // install apart from the one before it except by syncing and
            // reading the JSON for fields that should be there.
            Text(DeviceInfo.appVersion)
                .font(.footnote.monospaced())
                .foregroundStyle(.secondary)

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

            // Swim lengths were never exported, so every past pool session on
            // this device has splits waiting in HealthKit that the record has
            // never seen. Worth running once.
            Button {
                Task { await runSwimBackfill() }
            } label: {
                Label("Backfill swim lengths", systemImage: "figure.pool.swim")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered)
            .disabled(busy)
            .padding(.horizontal, 40)

            Button {
                showRangeBackfill = true
            } label: {
                Label("Backfill a date range…", systemImage: "calendar.badge.plus")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered)
            .disabled(busy)
            .padding(.horizontal, 40)

            if lastSeq > 0 {
                Text("Last sync #\(lastSeq)").font(.footnote).foregroundStyle(.tertiary)
            }
        }
        .sheet(isPresented: $showRangeBackfill) {
            RangeBackfillSheet { from, to in
                showRangeBackfill = false
                Task { await runRangeBackfill(from: from, to: to) }
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

    private func runSwimBackfill() async {
        busy = true
        defer { busy = false }
        do { status = try await engine.backfillSwimLengths() }
        catch { status = "Swim backfill failed: \(error.localizedDescription)" }
    }

    private func runRangeBackfill(from: Date, to: Date) async {
        busy = true
        defer { busy = false }
        do { status = try await engine.backfill(from: from, to: to) }
        catch { status = "Backfill failed: \(error.localizedDescription)" }
    }
}

/// Date-range picker for a repair backfill. Defaults to the week before
/// yesterday, and cannot select today or later — only complete days can be
/// re-shipped authoritatively (see `SyncEngine.backfill`).
private struct RangeBackfillSheet: View {
    let onRun: (Date, Date) -> Void
    @Environment(\.dismiss) private var dismiss

    private static let yesterday = Calendar.current.date(
        byAdding: .day, value: -1, to: Calendar.current.startOfDay(for: Date()))!

    @State private var from = Calendar.current.date(byAdding: .day, value: -7, to: yesterday)!
    @State private var to = yesterday

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    DatePicker("From", selection: $from,
                               in: ...Self.yesterday, displayedComponents: .date)
                    DatePicker("To", selection: $to,
                               in: from...Self.yesterday, displayedComponents: .date)
                } footer: {
                    Text("Re-reads these days from Health and ships them again. "
                         + "Days already in the database are replaced, not added, "
                         + "so nothing is double-counted. Anchors are untouched.")
                }
                Section {
                    Button("Back-fill range") { onRun(from, to) }
                }
            }
            .navigationTitle("Backfill range")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }
}

#Preview {
    ContentView()
}
