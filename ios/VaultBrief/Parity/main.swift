// Host harness: renders the Vault files from a JSON fixture using the *same*
// Zones.swift / VaultRender.swift the iOS app compiles, so the output can be
// diffed against the Python renderers byte for byte.
import Foundation

struct Fixture: Decodable {
    struct S: Decodable {
        let uuid: String, activity: String
        let start: Double
        let durationMin: Double
        let distanceKm: Double?, energyKcal: Double?, avgHR: Double?, maxHR: Double?
    }
    struct D: Decodable { let label: String; let sessions: [S] }
    let today: String, monday: String
    let disciplines: [String: D]
    let series: [String: [[Double]]]
    let week: [S], prior: [S], recent: [S]
    let restingHR: [Double?], hrv: [Double?]
}

func toSession(_ s: Fixture.S) -> Session {
    Session(uuid: UUID(uuidString: s.uuid) ?? UUID(),
            activity: s.activity,
            start: Date(timeIntervalSince1970: s.start),
            durationMin: s.durationMin,
            distanceKm: s.distanceKm, energyKcal: s.energyKcal,
            avgHR: s.avgHR, maxHR: s.maxHR)
}

let args = CommandLine.arguments
let fx = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: URL(fileURLWithPath: args[1])))
let outDir = URL(fileURLWithPath: args[2])
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)

let iso = DateFormatter()
iso.dateFormat = "yyyy-MM-dd"
iso.locale = Locale(identifier: "en_US_POSIX")
let today = iso.date(from: fx.today)!
let monday = iso.date(from: fx.monday)!

// Discipline files. Series are keyed by the fixture's uuid string; rebuild the
// map against the Session UUIDs the harness actually minted.
for (activity, file, label) in disciplines {
    guard let d = fx.disciplines[activity] else { continue }
    var sessions: [Session] = []
    var series: [UUID: [(Date, Double)]] = [:]
    for s in d.sessions {
        let session = toSession(s)
        sessions.append(session)
        if let raw = fx.series[s.uuid] {
            series[session.uuid] = raw.map { (Date(timeIntervalSince1970: $0[0]), $0[1]) }
        }
    }
    let md = renderDiscipline(label: label, sessions: sessions, series: series, today: today)
    try md.write(to: outDir.appendingPathComponent(file), atomically: true, encoding: .utf8)
    _ = d.label
}

let brief = renderWeekBrief(
    week: fx.week.map(toSession), prior: fx.prior.map(toSession), recent: fx.recent.map(toSession),
    restingHR: (fx.restingHR[0], fx.restingHR[1]), hrv: (fx.hrv[0], fx.hrv[1]),
    monday: monday, today: today)
try brief.write(to: outDir.appendingPathComponent("sport-week-current.md"),
                atomically: true, encoding: .utf8)
print("rendered \(disciplines.count + 1) files to \(outDir.path)")
