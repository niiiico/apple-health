import Foundation
import UIKit

/// Writes delta JSON + sidecars into the app's iCloud Drive folder
/// (ADR-002; restored by ADR-004 after the Box experiment).
///
/// iCloud gives durability for free: the write into the ubiquity container
/// **is** the durable write — the sync daemon uploads it afterwards with no
/// help from us, and the file survives app termination and reboots. Anchors
/// may therefore advance as soon as `write` returns, with no outbox and no
/// network in the path.
///
/// Ordering guarantee for the consumer: every sidecar is written before the
/// delta JSON that references it, so a delta is never visible before its
/// files. (iCloud does not guarantee *propagation* order, so `ah-ingest`
/// still tolerates a delta whose sidecars have not landed yet — see
/// docs/delta-contract.md.)
struct DeltaWriter {

    enum WriterError: LocalizedError {
        case iCloudUnavailable
        var errorDescription: String? {
            "iCloud Drive unavailable — check that iCloud is signed in and enabled for HealthSync."
        }
    }

    /// Must match `com.apple.developer.ubiquity-container-identifiers` in
    /// App.entitlements and the `NSUbiquitousContainers` key in Info.plist.
    static let containerID = "iCloud.net.dev2.healthsync"

    /// `<ubiquity container>/Documents/HealthSync/`, created on first use.
    /// The first call blocks while the container is provisioned, so call it
    /// off the main thread.
    static func folderURL() throws -> URL {
        guard let container = FileManager.default
            .url(forUbiquityContainerIdentifier: containerID) else {
            throw WriterError.iCloudUnavailable
        }
        let dir = container.appendingPathComponent("Documents/HealthSync", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    /// Write one sync's files: sidecars first, JSON last, all atomic.
    func write(delta: Delta, sidecars: [(name: String, content: String)], seq: Int) throws {
        let dir = try Self.folderURL()
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

    /// Write a single sidecar with no accompanying delta JSON. Only valid for
    /// files that are not ingested into the DB (HR-series CSVs — see the
    /// backfill exception in docs/delta-contract.md).
    ///
    /// Takes the folder as a parameter so a caller writing many sidecars pays
    /// the blocking `folderURL()` lookup once rather than per file.
    func writeSidecar(name: String, content: String, in dir: URL) throws {
        try content.data(using: .utf8)!
            .write(to: dir.appendingPathComponent(name), options: .atomic)
    }

    /// Names already present in the folder. A file this device has not
    /// materialised locally appears as a `.<name>.icloud` placeholder — it is
    /// still present, so the name is normalised before comparison.
    func existingFileNames() throws -> Set<String> {
        let names = try FileManager.default.contentsOfDirectory(atPath: Self.folderURL().path)
        return Set(names.map(Self.resolvePlaceholder))
    }

    /// `.route-X.gpx.icloud` → `route-X.gpx`; any other name unchanged.
    static func resolvePlaceholder(_ name: String) -> String {
        guard name.hasPrefix("."), name.hasSuffix(".icloud") else { return name }
        return String(name.dropFirst().dropLast(".icloud".count))
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
    /// Marketing version *and* build, e.g. "1.0 (44)".
    ///
    /// The build number is the one that matters and was missing:
    /// CFBundleShortVersionString has read "1.0" since the first release, so a
    /// delta could not say which build wrote it and neither could the screen.
    /// Establishing that a sync had picked up new fields meant inspecting the
    /// JSON for them — workable once, useless as a habit.
    static var appVersion: String {
        let info = Bundle.main.infoDictionary
        let short = info?["CFBundleShortVersionString"] as? String ?? "0"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return "\(short) (\(build))"
    }
}
