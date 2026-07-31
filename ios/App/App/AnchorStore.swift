import Foundation
import HealthKit

/// Persists one `HKQueryAnchor` per HealthKit query key so each sync only sees
/// samples added/deleted since the previous run.
///
/// Anchors are advanced **only after** the delta file is durably written, so a
/// crash mid-sync re-queries the same window rather than losing data.
final class AnchorStore {
    private let defaults: UserDefaults
    private let prefix = "anchor."
    private let seqKey = "anchor.seq"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    /// Monotonic sync counter, embedded in delta filenames as `<seq>`.
    var nextSeq: Int {
        let n = defaults.integer(forKey: seqKey) + 1
        return n
    }

    func commitSeq(_ seq: Int) {
        defaults.set(seq, forKey: seqKey)
    }

    func anchor(for key: String) -> HKQueryAnchor? {
        guard let data = defaults.data(forKey: prefix + key) else { return nil }
        return try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: data)
    }

    func setAnchor(_ anchor: HKQueryAnchor, for key: String) {
        guard let data = try? NSKeyedArchiver.archivedData(
            withRootObject: anchor, requiringSecureCoding: true) else { return }
        defaults.set(data, forKey: prefix + key)
    }
}
