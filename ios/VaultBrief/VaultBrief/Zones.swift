import Foundation

/// HR zones (bpm), from the athlete profile.
///
/// **This is the single definition.** On the Mac side the same model is spelled
/// out twice — `ZONES` in `tools/race_detail.py` and a duplicated SQL `CASE` in
/// `src/apple_health/html_report.py` — which is how their labels drifted (`Z1
/// <134` vs `Z1 <135`, both cutting at 135). That drift is now resolved: the
/// Python side has one definition in `derive/zones.py`, the HTML report
/// generates its SQL from it, and `<135` won because the band includes 134.
/// This file is the deliberate second copy, kept honest by Parity/check.sh.
/// Do not add a third: anything here
/// that needs zones reads `Zones.all`.
enum Zones {
    struct Zone {
        let label: String   // e.g. "Z1 <135" — rendered verbatim into the Vault
        let lo: Double
        let hi: Double

        /// Leading token ("Z1"), used in the compact per-session zone line.
        var short: String { String(label.prefix(while: { $0 != " " })) }
    }

    static let all: [Zone] = [
        .init(label: "Z1 <135",    lo: 0,   hi: 134),
        .init(label: "Z2 135-159", lo: 135, hi: 159),
        .init(label: "Z3 160-169", lo: 160, hi: 169),
        .init(label: "Z4 170-177", lo: 170, hi: 177),
        .init(label: "Z5 >=178",   lo: 178, hi: 999),
    ]

    /// Header line listing every band, e.g. "Z1 <135 · Z2 135-159 · …".
    static var header: String { all.map(\.label).joined(separator: " · ") }

    /// Zone containing `hr`. Falls back to Z1, matching `derive.zones.zone_of`:
    /// the bands leave no gap, so this only ever fires on a negative reading.
    static func of(_ hr: Double) -> Zone {
        all.first { hr >= $0.lo && hr <= $0.hi } ?? all[0]
    }
}

/// Distribution + drift over one workout's heart-rate series.
///
/// Mirrors `race_detail.summarize` / `race_detail.thirds` so the phone-rendered
/// Vault files can be diffed against the Mac-rendered ones during the ADR-005
/// side-by-side period.
struct HRSummary {
    let n: Int
    let avg: Double
    let min: Double
    let max: Double
    /// Percent of samples per zone, keyed by `Zone.label`.
    let zonePercent: [String: Double]
    /// (label, mean, max) per third, chronological. Empty when < 120 samples —
    /// drift over a handful of readings is noise, not a trend.
    let thirds: [(String, Double, Double)]

    init?(series: [(Date, Double)]) {
        guard !series.isEmpty else { return nil }
        let sorted = series.sorted { $0.0 < $1.0 }
        let hrs = sorted.map(\.1)
        n = hrs.count
        avg = hrs.reduce(0, +) / Double(n)
        min = hrs.min()!
        max = hrs.max()!

        // `total` rather than `n`: referring to the property inside the closure
        // would capture a partly-initialised `self`.
        let total = hrs.count
        var counts: [String: Int] = [:]
        for h in hrs { counts[Zones.of(h).label, default: 0] += 1 }
        zonePercent = counts.mapValues { 100.0 * Double($0) / Double(total) }

        guard total >= 120 else { thirds = []; return }
        let third = total / 3
        thirds = ["1/3", "2/3", "3/3"].enumerated().map { i, label in
            let chunk = i < 2
                ? Array(hrs[(i * third)..<((i + 1) * third)])
                : Array(hrs[(2 * third)...])
            return (label, chunk.reduce(0, +) / Double(chunk.count), chunk.max()!)
        }
    }
}
