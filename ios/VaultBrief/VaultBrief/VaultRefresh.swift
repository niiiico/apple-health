import Foundation
import HealthKit

/// Builds the four curated Vault files and pushes the ones that changed.
///
/// The whole ADR-005 pipeline: query HealthKit → render markdown → upload.
/// Idempotent by construction — each run renders the current window from
/// scratch and overwrites, so running it twice is a no-op and running it late
/// simply produces a fresher file. There is nothing to replay and nothing to
/// double-count.
struct VaultRefresh {
    let health = HealthQueries()
    let box = BoxClient.shared

    struct Result {
        var rendered: [String: String] = [:]
        var pushed: [String] = []
        var unchanged: [String] = []
    }

    /// Render every Vault file for the week containing `today`.
    func render(today: Date = Date(), calendar: Calendar = .autoupdatingCurrent) async throws -> [String: String] {
        var cal = calendar
        cal.firstWeekday = 2                       // Monday, as `date.weekday()` in Python
        let startOfToday = cal.startOfDay(for: today)
        let monday = cal.dateInterval(of: .weekOfYear, for: startOfToday)!.start
        let tomorrow = cal.date(byAdding: .day, value: 1, to: startOfToday)!
        let fourWeeksBefore = cal.date(byAdding: .weekOfYear, value: -4, to: monday)!
        let lookback = cal.date(byAdding: .weekOfYear, value: -significantLookbackWeeks, to: startOfToday)!

        var files: [String: String] = [:]

        // Rolling per-discipline files.
        for d in disciplines {
            guard let hkType = Self.hkActivity(d.activity) else { continue }
            let sessions = try await health.recent(activity: hkType, limit: sessionsPerFile)
            var series: [UUID: [(Date, Double)]] = [:]
            for s in sessions {
                let end = s.start.addingTimeInterval(s.durationMin * 60)
                let vals = try await health.heartRateSeries(for: s.uuid, start: s.start, end: end)
                if !vals.isEmpty { series[s.uuid] = vals }
            }
            files[d.file] = renderDiscipline(label: d.label, sessions: sessions,
                                             series: series, today: startOfToday)
        }

        // Weekly brief.
        let week = try await health.workouts(from: monday, to: tomorrow)
        let prior = try await health.workouts(from: fourWeeksBefore, to: monday)
        let recent = try await health.workouts(from: lookback, to: monday).reversed()
        let bpm = HKUnit.count().unitDivided(by: .minute())
        let ms = HKUnit.secondUnit(with: .milli)
        let rhr = (try await health.average(.restingHeartRate, unit: bpm, from: monday, to: tomorrow),
                   try await health.average(.restingHeartRate, unit: bpm, from: fourWeeksBefore, to: monday))
        let hrv = (try await health.average(.heartRateVariabilitySDNN, unit: ms, from: monday, to: tomorrow),
                   try await health.average(.heartRateVariabilitySDNN, unit: ms, from: fourWeeksBefore, to: monday))

        files["sport-week-current.md"] = renderWeekBrief(
            week: week, prior: prior, recent: Array(recent),
            restingHR: rhr, hrv: hrv, monday: monday, today: startOfToday)

        return files
    }

    /// Render, then upload only what differs from what Box already holds.
    ///
    /// Comparing before uploading keeps the Vault's version history meaningful
    /// and, during the ADR-005 side-by-side period, means a no-op run leaves no
    /// trace to confuse the staging-vs-live diff.
    func run(today: Date = Date()) async throws -> Result {
        var result = Result()
        result.rendered = try await render(today: today)
        let folder = try await box.stagingFolderID()
        let existing = try await box.list(folder: folder)

        for (name, content) in result.rendered.sorted(by: { $0.key < $1.key }) {
            var current: String?
            if let item = existing[name] { current = try await box.download(item) }
            if current == content {
                result.unchanged.append(name)
                continue
            }
            try await box.upload(folder: folder, name: name, content: content, existing: existing[name])
            result.pushed.append(name)
        }
        return result
    }

    private static func hkActivity(_ name: String) -> HKWorkoutActivityType? {
        switch name {
        case "Swimming": return .swimming
        case "Running": return .running
        case "Cycling": return .cycling
        default: return nil
        }
    }
}
