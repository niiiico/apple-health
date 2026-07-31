import Foundation
import UIKit

/// Stages delta JSON + sidecars in a local **outbox**, then drains it to the
/// Box transport folder (ADR-003).
///
/// The outbox is the durability gate: a file that reaches it WILL eventually
/// reach Box (drain retries on every sync), so anchors may advance as soon as
/// the local write succeeds — never re-emitting a window means never
/// double-counting. Ordering guarantee for the consumer: within a drain,
/// every sidecar is uploaded before any delta JSON, and deltas go oldest
/// first, so a delta is never visible before the files it references.
struct DeltaWriter {

    /// Local staging folder (Documents/HealthSyncOutbox — survives restarts,
    /// visible in the Files app for debugging).
    static func outboxURL() throws -> URL {
        let docs = try FileManager.default.url(for: .documentDirectory, in: .userDomainMask,
                                               appropriateFor: nil, create: true)
        let dir = docs.appendingPathComponent("HealthSyncOutbox", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    /// Stage one sync's files: sidecars first, JSON last, all atomic.
    func write(delta: Delta, sidecars: [(name: String, content: String)], seq: Int) throws {
        let dir = try Self.outboxURL()
        for file in sidecars {
            try file.content.data(using: .utf8)!
                .write(to: dir.appendingPathComponent(file.name), options: .atomic)
        }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(delta)
        let name = "delta-\(DateFormats.fileStamp(Date()))-\(String(format: "%04d", seq)).json"
        try data.write(to: dir.appendingPathComponent(name), options: .atomic)
    }

    /// Stage a single sidecar with no accompanying delta JSON. Only valid for
    /// files that are not ingested into the DB (HR-series CSVs — see the
    /// backfill note in docs/delta-contract.md).
    func writeSidecar(name: String, content: String) throws {
        let url = try Self.outboxURL().appendingPathComponent(name)
        try content.data(using: .utf8)!.write(to: url, options: .atomic)
    }

    /// Names currently staged (pending upload).
    func pendingNames() throws -> [String] {
        try FileManager.default.contentsOfDirectory(atPath: Self.outboxURL().path)
            .filter { !$0.hasPrefix(".") }
    }

    /// Upload everything in the outbox — sidecars first, then delta JSONs in
    /// ascending (chronological) filename order — deleting each local file
    /// once its upload succeeds. Throws on the first failure; whatever
    /// remains stays queued for the next drain. Returns the uploaded count.
    @discardableResult
    func drainOutbox(using box: BoxClient) async throws -> Int {
        let dir = try Self.outboxURL()
        let names = try pendingNames()
        let sidecars = names.filter { !$0.hasPrefix("delta-") }.sorted()
        let deltas = names.filter { $0.hasPrefix("delta-") }.sorted()
        var uploaded = 0
        for name in sidecars + deltas {
            let url = dir.appendingPathComponent(name)
            try await box.upload(name: name, content: try Data(contentsOf: url))
            try FileManager.default.removeItem(at: url)
            uploaded += 1
        }
        return uploaded
    }
}

/// Date string helpers shared across the app. Centralised so the wire formats
/// stay consistent with the delta contract.
enum DateFormats {
    /// "2026-06-25 07:12:33 +0200" — matches Apple Health export date strings.
    static func appleDate(_ d: Date) -> String { appleFormatter.string(from: d) }
    /// "2026-06-25" — local calendar day, matches `parse_export._day`.
    static func day(_ d: Date) -> String { dayFormatter.string(from: d) }
    /// ISO-8601 UTC, e.g. "2026-06-26T03:00:00Z".
    static func iso(_ d: Date) -> String { isoFormatter.string(from: d) }
    /// "20260626T030000Z" — compact UTC stamp for delta filenames.
    static func fileStamp(_ d: Date) -> String { stampFormatter.string(from: d) }

    private static let appleFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd HH:mm:ss Z"
        return f
    }()
    private static let dayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
    private static let isoFormatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()
    private static let stampFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyyMMdd'T'HHmmss'Z'"
        return f
    }()
}

/// Device / app identifiers, informational fields in the delta.
enum DeviceInfo {
    static var model: String { UIDevice.current.model }
    static var appVersion: String {
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0"
    }
}
