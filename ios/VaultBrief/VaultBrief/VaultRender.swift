import Foundation

/// One workout, flattened out of HealthKit.
///
/// Deliberately free of `HKWorkout` so every renderer below is a pure function
/// over value types — they can be exercised on the host without a device, which
/// is what makes the staging-vs-live diff (ADR-005) a real check.
struct Session {
    let uuid: UUID
    let activity: String        // normalised, HK prefix stripped ("Running", …)
    let start: Date
    let durationMin: Double
    let distanceKm: Double?
    let energyKcal: Double?
    let avgHR: Double?
    let maxHR: Double?
}

/// Rolling per-discipline Vault files, keyed by activity.
let disciplines: [(activity: String, file: String, label: String)] = [
    ("Swimming", "sport-natation-sessions.md", "natation"),
    ("Running",  "sport-course-sessions.md",   "course"),
    ("Cycling",  "sport-velo-sessions.md",     "vélo"),
]

let sessionsPerFile = 5

/// A workout is "significant" if multisport, or past a per-activity distance
/// floor (km). Mirrors `vault_sport_week`.
///
/// An array, not a dictionary: the floors are printed into the brief's section
/// heading, and Python emits them in declaration order. Sorting the keys instead
/// would render a different heading for identical data and make the
/// staging-vs-live diff noisy.
let significantFloorKm: [(activity: String, km: Double)] = [
    ("Running", 10), ("Swimming", 2), ("Cycling", 30),
]

private let floorByActivity = Dictionary(uniqueKeysWithValues:
    significantFloorKm.map { ($0.activity, $0.km) })
let alwaysSignificant: Set<String> = ["SwimBikeRun"]
let significantLookbackWeeks = 6

// MARK: - Formatting primitives
//
// These exist to match the Python renderers character for character. Swift's
// default number formatting differs from Python's (`%g`-style trimming, locale
// separators), so every number below goes through an explicit format.

private func f(_ v: Double, _ places: Int) -> String {
    String(format: "%.\(places)f", v)
}

/// m:ss, as `session_detail._mmss`.
func mmss(_ seconds: Double) -> String {
    String(format: "%d:%02d", Int(seconds / 60), Int(seconds.truncatingRemainder(dividingBy: 60)))
}

private let isoDay: DateFormatter = {
    let d = DateFormatter()
    d.dateFormat = "yyyy-MM-dd"
    // The Vault reads as local training days; a UTC formatter would file an
    // evening workout under the next day.
    d.locale = Locale(identifier: "en_US_POSIX")
    return d
}()

func day(_ date: Date) -> String { isoDay.string(from: date) }

// MARK: - Discipline files

/// The `## <date> — 12.34 km / 45 min / 5:30/km / FC 141/164 / 910 kcal` line
/// plus its zone/drift detail.
private func sessionEntry(_ s: Session, series: [(Date, Double)]?) -> [String] {
    var parts: [String] = []
    if let km = s.distanceKm { parts.append("\(f(km, 2)) km") }
    parts.append("\(f(s.durationMin, 0)) min")
    if s.activity == "Running", let km = s.distanceKm, km > 0 {
        parts.append(mmss(s.durationMin * 60 / km) + "/km")
    }
    if let avg = s.avgHR, let mx = s.maxHR { parts.append("FC \(f(avg, 0))/\(f(mx, 0))") }
    if let kcal = s.energyKcal { parts.append("\(f(kcal, 0)) kcal") }

    var lines = ["## \(day(s.start)) — " + parts.joined(separator: " / ")]
    if let series, let sum = HRSummary(series: series) {
        let zones = Zones.all
            .map { "\($0.short) \(f(sum.zonePercent[$0.label] ?? 0, 0)) %" }
            .joined(separator: " · ")
        lines.append("- Zones (\(sum.n) éch.) : \(zones).")
        if sum.thirds.count == 3 {
            let means = sum.thirds.map { f($0.1, 0) }.joined(separator: " → ")
            let peak = sum.thirds.map(\.2).max()!
            lines.append("- Dérive par tiers : moy \(means) ; max \(f(peak, 0)).")
        }
    } else {
        lines.append("- Séries FC indisponibles — avg/max seulement.")
    }
    return lines + [""]
}

/// Rolling session file for one discipline, most recent first.
func renderDiscipline(label: String, sessions: [Session],
                      series: [UUID: [(Date, Double)]], today: Date) -> String {
    var lines = [
        "---",
        "tags: [sport, \(label), sessions, fc]",
        "volatility: high",
        "last_updated: \(day(today))",
        "---",
        "# \(label.prefix(1).uppercased() + label.dropFirst()) — \(sessionsPerFile) dernières séances (rolling, auto)",
        "",
        "Généré par VaultBrief (iOS) depuis HealthKit ; écrasé à chaque refresh. "
            + "Zones (bpm) : \(Zones.header).",
        "",
    ]
    for s in sessions.prefix(sessionsPerFile) {
        lines += sessionEntry(s, series: series[s.uuid])
    }
    return lines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines) + "\n"
}

// MARK: - Weekly brief

private func fmtWorkout(_ s: Session) -> String {
    var parts: [String] = []
    if let km = s.distanceKm { parts.append("\(f(km, 1)) km") }
    parts.append("\(f(s.durationMin, 0)) min")
    if s.activity == "Running", let km = s.distanceKm, km > 0 {
        let perKm = s.durationMin / km
        parts.append("\(Int(perKm)):\(String(format: "%02d", Int((perKm.truncatingRemainder(dividingBy: 1) * 60).rounded())))/km")
    }
    if let avg = s.avgHR { parts.append("avg HR \(f(avg, 0))") }
    return "- \(day(s.start)) **\(s.activity)** — \(parts.joined(separator: ", "))"
}

/// Week-to-date brief with prior-4-week context. `week`, `prior` and `recent`
/// are the caller's already-scoped windows (see `HealthQueries.brief`).
func renderWeekBrief(week: [Session], prior: [Session], recent: [Session],
                     restingHR: (Double?, Double?), hrv: (Double?, Double?),
                     monday: Date, today: Date) -> String {
    func totals(_ rows: [Session]) -> [String: (Int, Double)] {
        var out: [String: (Int, Double)] = [:]
        for r in rows {
            let (n, km) = out[r.activity] ?? (0, 0)
            out[r.activity] = (n + 1, km + (r.distanceKm ?? 0))
        }
        return out
    }
    let weekT = totals(week), priorT = totals(prior)

    let cal = Calendar(identifier: .iso8601)
    let yearForWeek = cal.component(.yearForWeekOfYear, from: monday)
    let weekNo = cal.component(.weekOfYear, from: monday)

    var lines = [
        "---",
        "tags: [sport, training, weekly]",
        "volatility: high",
        "last_updated: \(day(today))",
        "---",
        "# Training week \(yearForWeek)-W\(String(format: "%02d", weekNo))"
            + " (from \(day(monday)), through \(day(today)))",
        "",
        "## Totals (week to date vs prior 4-wk weekly avg)",
    ]
    for act in Set(weekT.keys).union(priorT.keys).sorted() {
        let (n, km) = weekT[act] ?? (0, 0)
        let (pn, pkm) = priorT[act] ?? (0, 0)
        lines.append("- \(act): \(n) session(s), \(f(km, 1)) km "
                     + "(avg \(f(Double(pn) / 4, 1))/wk, \(f(pkm / 4, 1)) km/wk)")
    }

    lines += ["", "## Sessions this week"]
    lines += week.isEmpty ? ["- none yet"] : week.map(fmtWorkout)

    lines += ["", "## Wellness"]
    if let w = restingHR.0, let p = restingHR.1 {
        lines.append("- Resting HR: \(f(w, 0)) bpm this week (4-wk avg \(f(p, 0)))")
    }
    if let w = hrv.0, let p = hrv.1 {
        lines.append("- HRV (SDNN): \(f(w, 0)) ms this week (4-wk avg \(f(p, 0)))")
    }

    let floors = significantFloorKm
        .map { "\($0.activity) ≥ \(f($0.km, 0)) km" }
        .joined(separator: " OR ")
    let sig = recent.filter {
        alwaysSignificant.contains($0.activity)
            || ($0.distanceKm ?? 0) >= (floorByActivity[$0.activity] ?? .infinity)
    }
    lines += ["", "## Significant workouts, prior \(significantLookbackWeeks) weeks (multisport OR \(floors))"]
    lines += sig.isEmpty ? ["- none"] : sig.map(fmtWorkout)

    lines += ["", "_Snapshot generated on-device from HealthKit; full data stays on the phone._", ""]
    return lines.joined(separator: "\n")
}
