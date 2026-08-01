import Foundation

/// Codable mirror of the delta JSON contract (schema v1).
/// See ../../../docs/delta-contract.md — keep field names in sync.
struct Delta: Codable {
    var schema = 1
    var generated_at: String
    var device: String
    var app_version: String
    var anchor_seq: Int
    var workouts = WorkoutSection()
    var records = RecordSection()
    var daily_metrics = DailySection()

    /// Set only on a backfill delta (schema 2), which re-ships a closed date
    /// range queried from scratch rather than samples since an anchor. Its
    /// `daily_metrics` buckets cover whole days and *replace* stored rows, so
    /// a range the DB already holds can be re-shipped without double-counting.
    /// Nil on a normal delta — `JSONEncoder` then omits the key entirely, so
    /// incremental files stay byte-identical to schema 1.
    var backfill: BackfillRange? = nil

    struct BackfillRange: Codable {
        let from: String   // YYYY-MM-DD, inclusive
        let to: String     // YYYY-MM-DD, inclusive, must be before today
    }

    /// True when the delta carries nothing worth publishing.
    var isEmpty: Bool {
        workouts.added.isEmpty && workouts.deleted.isEmpty &&
        records.added.isEmpty && records.deleted.isEmpty &&
        daily_metrics.added.isEmpty
    }

    struct WorkoutSection: Codable {
        var added: [Workout] = []
        var deleted: [String] = []
    }
    struct RecordSection: Codable {
        var added: [Record] = []
        var deleted: [String] = []
    }
    struct DailySection: Codable {
        var added: [DailyBucket] = []
    }

    struct Workout: Codable {
        let uuid: String
        let activity: String
        let start: String
        let end: String
        let duration_min: Double?
        let distance_km: Double?
        let energy_kcal: Double?
        let avg_hr: Double?
        let max_hr: Double?
        let source: String?
        let indoor: Int?
        let route_file: String?
        let hr_file: String?
    }

    struct Record: Codable {
        let type: String
        let start: String
        let value: Double
        let unit: String
        let source: String?
    }

    /// A *partial* per-(day,type) aggregate covering only this delta's new
    /// samples. `ah-ingest` adds it onto the stored row. No `avg` — derived
    /// downstream as `sum / count`.
    struct DailyBucket: Codable {
        let day: String
        let type: String
        let unit: String
        var count: Int
        var sum: Double
        var min: Double
        var max: Double

        mutating func add(_ value: Double) {
            count += 1
            sum += value
            if value < min { min = value }
            if value > max { max = value }
        }
    }
}
