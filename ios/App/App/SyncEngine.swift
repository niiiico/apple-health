import HealthKit

/// Orchestrates one incremental sync: runs anchored queries for workouts,
/// sparse records and dense quantities, builds a `Delta`, exports route GPX,
/// writes everything to the shared folder, then advances anchors.
///
/// Anchors advance **only after** `DeltaWriter` reports a durable write, so any
/// failure simply re-queries the same window next time.
final class SyncEngine {
    private let store = HKHealthStore()
    private let anchors = AnchorStore()
    private let writer = DeltaWriter()

    enum SyncError: Error { case healthDataUnavailable }

    /// First local day this app owns. Days before it are already in `health.db`
    /// from the full export (taken 2026-06-29) and must never be re-emitted:
    /// the `daily_metrics` merge is additive, so re-sent history double-counts
    /// (see docs/delta-contract.md). This also keeps the first anchor-less sync
    /// to weeks of samples instead of the entire HealthKit history. Bump this
    /// only after re-basing the DB with a newer full export (and reinstalling
    /// the app so anchors reset) — the export's own taken-day stays partial in
    /// the DB and the first delta's full-day buckets must replace it (see
    /// ios/README.md, "Re-basing").
    static let bootstrapCutoff: Date = {
        var c = DateComponents()
        c.year = 2026; c.month = 6; c.day = 29
        return Calendar.current.date(from: c)!
    }()

    /// Request read authorisation for every observed type.
    func requestAuthorization() async throws {
        guard HKHealthStore.isHealthDataAvailable() else { throw SyncError.healthDataUnavailable }
        try await store.requestAuthorization(toShare: [], read: HealthTypes.readTypes)
    }

    /// Run a sync. Returns a short human summary for the UI. A no-op (nothing
    /// new) writes no file and advances no anchor.
    @discardableResult
    func sync() async throws -> String {
        let seq = anchors.nextSeq
        var delta = Delta(
            generated_at: DateFormats.iso(Date()),
            device: DeviceInfo.model,
            app_version: DeviceInfo.appVersion,
            anchor_seq: seq
        )

        // Track anchors to commit only on success, keyed by query.
        var pendingAnchors: [(key: String, anchor: HKQueryAnchor)] = []
        var sidecars: [(name: String, content: String)] = []

        // --- Workouts (+ route GPX & per-workout HR series) ---
        let wq = try await runAnchored(type: HKObjectType.workoutType(), key: "workouts")
        pendingAnchors.append((key: "workouts", anchor: wq.anchor))
        delta.workouts.deleted = wq.deleted
        let exporter = RouteExporter(store: store)
        for case let w as HKWorkout in wq.added {
            var routeFile: String? = nil
            if let gpx = try? await exporter.gpx(for: w) {
                let name = "route-\(w.uuid.uuidString).gpx"
                sidecars.append((name: name, content: gpx))
                routeFile = name
            }
            var hrFile: String? = nil
            if let csv = try? await hrSeriesCSV(for: w), !csv.isEmpty {
                let name = "hr-\(w.uuid.uuidString).csv"
                sidecars.append((name: name, content: csv))
                hrFile = name
            }
            var swimFile: String? = nil
            if let csv = try? await swimLengthsCSV(for: w), !csv.isEmpty {
                let name = "swim-\(w.uuid.uuidString).csv"
                sidecars.append((name: name, content: csv))
                swimFile = name
            }
            delta.workouts.added.append(Self.makeWorkout(
                w, routeFile: routeFile, hrFile: hrFile, swimFile: swimFile))
        }

        // --- Sparse records ---
        for q in HealthTypes.sparse {
            let r = try await runAnchored(type: q.type, key: "sparse." + q.name)
            pendingAnchors.append((key: "sparse." + q.name, anchor: r.anchor))
            for case let s as HKQuantitySample in r.added {
                delta.records.added.append(.init(
                    type: q.name,
                    start: DateFormats.appleDate(s.startDate),
                    value: s.quantity.doubleValue(for: q.unit),
                    unit: q.unit.unitString,
                    source: s.sourceRevision.source.name
                ))
            }
        }

        // --- Dense quantities → per-(day,type) partial buckets ---
        for q in HealthTypes.dense {
            let r = try await runAnchored(type: q.type, key: "dense." + q.name)
            pendingAnchors.append((key: "dense." + q.name, anchor: r.anchor))
            var buckets: [String: Delta.DailyBucket] = [:]  // keyed by day
            for case let s as HKQuantitySample in r.added {
                let day = DateFormats.day(s.startDate)
                let value = s.quantity.doubleValue(for: q.unit)
                if buckets[day] != nil {
                    buckets[day]!.add(value)
                } else {
                    buckets[day] = .init(day: day, type: q.name, unit: q.unit.unitString,
                                         count: 1, sum: value, min: value, max: value)
                }
            }
            delta.daily_metrics.added.append(contentsOf: buckets.values)
        }

        if delta.isEmpty {
            // Still advance anchors — we have consumed this window, just nothing to ship.
            for p in pendingAnchors { anchors.setAnchor(p.anchor, for: p.key) }
            anchors.commitSeq(seq)
            return "Up to date — nothing new."
        }

        // Write to iCloud Drive (sidecars first, JSON last, atomic). The local
        // write is the durable write — iCloud uploads it for us — so it is safe
        // to advance anchors as soon as it returns. A throw here leaves the
        // anchors untouched and the same window is simply re-queried next time.
        try writer.write(delta: delta, sidecars: sidecars, seq: seq)
        for p in pendingAnchors { anchors.setAnchor(p.anchor, for: p.key) }
        anchors.commitSeq(seq)

        return "Synced \(delta.workouts.added.count) workouts, "
             + "\(delta.daily_metrics.added.count) metric-days, "
             + "\(delta.records.added.count) records."
    }

    /// One-off repair pass: write the `hr-<uuid>.csv` sidecar for every workout
    /// since `bootstrapCutoff` that does not already have one in the shared
    /// folder. Uses a plain (non-anchored) workout query, so it reaches
    /// workouts already consumed by past syncs — deltas written before
    /// 2026-07-11 lacked HR series entirely. Safe by construction: HR CSVs are
    /// never ingested into `health.db` (consumed per-uuid by
    /// `tools/session_detail.py`), so re-writing them cannot double-count, and
    /// neither deltas nor anchors are touched.
    func backfillHRSeries() async throws -> String {
        let workouts = try await workoutsSinceCutoff()
        // Skip anything already in the folder, including files that exist only
        // as not-yet-downloaded iCloud placeholders on this device.
        let dir = try DeltaWriter.folderURL()
        let existing = try writer.existingFileNames()
        var written = 0, present = 0, noHR = 0
        for w in workouts {
            let name = "hr-\(w.uuid.uuidString).csv"
            if existing.contains(name) { present += 1; continue }
            guard let csv = try? await hrSeriesCSV(for: w), !csv.isEmpty else {
                noHR += 1
                continue
            }
            try writer.writeSidecar(name: name, content: csv, in: dir)
            written += 1
        }
        return "HR backfill: \(written) written, \(present) already present, \(noHR) without HR."
    }

    // MARK: - Range backfill

    enum BackfillError: LocalizedError {
        case rangeInverted
        case rangeNotClosed
        case beforeCutoff

        var errorDescription: String? {
            switch self {
            case .rangeInverted: "The start date is after the end date."
            case .rangeNotClosed:
                "A backfill must end before today — only complete days can be authoritative."
            case .beforeCutoff:
                "That range predates the full export the database was built from; "
                    + "it is already covered there."
            }
        }
    }

    /// Re-ship a closed date range, whatever the anchors have already consumed.
    ///
    /// This is the repair path for a window lost from the DB — e.g. anchors
    /// advanced over days whose samples were never shipped, which the empty
    /// delta path can do if HealthKit returns nothing because authorisation was
    /// reset. Unlike `sync()` it queries by date instead of by anchor, and
    /// **touches no anchor**, so the incremental stream is unaffected and a
    /// backfill can be re-run as often as needed.
    ///
    /// Safe against duplication by construction: workouts and records dedupe on
    /// the consumer by uuid / natural key, and the daily buckets here cover
    /// whole days, so `ah-ingest` replaces rather than adds them (schema 2 —
    /// see docs/delta-contract.md).
    @discardableResult
    func backfill(from: Date, to: Date) async throws -> String {
        let cal = Calendar.current
        let start = cal.startOfDay(for: from)
        let end = cal.date(byAdding: .day, value: 1, to: cal.startOfDay(for: to))!
        guard start < end else { throw BackfillError.rangeInverted }
        guard end <= cal.startOfDay(for: Date()) else { throw BackfillError.rangeNotClosed }
        guard start >= cal.startOfDay(for: Self.bootstrapCutoff) else {
            throw BackfillError.beforeCutoff
        }

        let seq = anchors.nextSeq
        var delta = Delta(
            generated_at: DateFormats.iso(Date()),
            device: DeviceInfo.model,
            app_version: DeviceInfo.appVersion,
            anchor_seq: seq,
            backfill: .init(from: DateFormats.day(start),
                            to: DateFormats.day(cal.date(byAdding: .day, value: -1, to: end)!))
        )
        delta.schema = 2
        var sidecars: [(name: String, content: String)] = []

        let range = HKQuery.predicateForSamples(
            withStart: start, end: end, options: .strictStartDate)

        // --- Workouts (+ sidecars, same shape as an incremental delta) ---
        let exporter = RouteExporter(store: store)
        for case let w as HKWorkout in try await runRange(
            type: HKObjectType.workoutType(), predicate: range) {
            var routeFile: String? = nil
            if let gpx = try? await exporter.gpx(for: w) {
                let name = "route-\(w.uuid.uuidString).gpx"
                sidecars.append((name: name, content: gpx))
                routeFile = name
            }
            var hrFile: String? = nil
            if let csv = try? await hrSeriesCSV(for: w), !csv.isEmpty {
                let name = "hr-\(w.uuid.uuidString).csv"
                sidecars.append((name: name, content: csv))
                hrFile = name
            }
            var swimFile: String? = nil
            if let csv = try? await swimLengthsCSV(for: w), !csv.isEmpty {
                let name = "swim-\(w.uuid.uuidString).csv"
                sidecars.append((name: name, content: csv))
                swimFile = name
            }
            delta.workouts.added.append(Self.makeWorkout(
                w, routeFile: routeFile, hrFile: hrFile, swimFile: swimFile))
        }

        // --- Sparse records ---
        for q in HealthTypes.sparse {
            for case let s as HKQuantitySample in try await runRange(
                type: q.type, predicate: range) {
                delta.records.added.append(.init(
                    type: q.name,
                    start: DateFormats.appleDate(s.startDate),
                    value: s.quantity.doubleValue(for: q.unit),
                    unit: q.unit.unitString,
                    source: s.sourceRevision.source.name
                ))
            }
        }

        // --- Dense quantities → whole-day (authoritative) buckets ---
        for q in HealthTypes.dense {
            var buckets: [String: Delta.DailyBucket] = [:]
            for case let s as HKQuantitySample in try await runRange(
                type: q.type, predicate: range) {
                let day = DateFormats.day(s.startDate)
                let value = s.quantity.doubleValue(for: q.unit)
                if buckets[day] != nil {
                    buckets[day]!.add(value)
                } else {
                    buckets[day] = .init(day: day, type: q.name, unit: q.unit.unitString,
                                         count: 1, sum: value, min: value, max: value)
                }
            }
            delta.daily_metrics.added.append(contentsOf: buckets.values)
        }

        if delta.isEmpty {
            // Nothing to replace: write no file and burn no sequence number, so
            // the range can simply be retried.
            return "Nothing found in that range."
        }

        try writer.write(delta: delta, sidecars: sidecars, seq: seq)
        anchors.commitSeq(seq)   // sequence only — anchors deliberately untouched

        return "Backfilled \(delta.workouts.added.count) workouts, "
             + "\(delta.daily_metrics.added.count) metric-days, "
             + "\(delta.records.added.count) records."
    }

    /// Non-anchored query over an explicit predicate.
    private func runRange(type: HKSampleType,
                          predicate: NSPredicate) async throws -> [HKSample] {
        try await withCheckedThrowingContinuation { cont in
            let q = HKSampleQuery(
                sampleType: type,
                predicate: predicate,
                limit: HKObjectQueryNoLimit,
                sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierStartDate,
                                                   ascending: true)]
            ) { _, samples, error in
                if let error { cont.resume(throwing: error); return }
                cont.resume(returning: samples ?? [])
            }
            store.execute(q)
        }
    }

    /// Every workout with `startDate >= bootstrapCutoff`, regardless of anchors.
    private func workoutsSinceCutoff() async throws -> [HKWorkout] {
        try await withCheckedThrowingContinuation { cont in
            let q = HKSampleQuery(
                sampleType: HKObjectType.workoutType(),
                predicate: HKQuery.predicateForSamples(
                    withStart: Self.bootstrapCutoff, end: nil, options: .strictStartDate),
                limit: HKObjectQueryNoLimit,
                sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: true)]
            ) { _, samples, error in
                if let error { cont.resume(throwing: error); return }
                cont.resume(returning: (samples ?? []).compactMap { $0 as? HKWorkout })
            }
            store.execute(q)
        }
    }

    // MARK: - Anchored query helper

    private struct AnchoredResult {
        let added: [HKSample]
        let deleted: [String]
        let anchor: HKQueryAnchor
    }

    /// Wrap `HKAnchoredObjectQuery` as async: returns new samples, deleted
    /// UUIDs, and the advanced anchor for the given persisted key.
    private func runAnchored(type: HKSampleType, key: String) async throws -> AnchoredResult {
        let start = anchors.anchor(for: key)
        return try await withCheckedThrowingContinuation { cont in
            let q = HKAnchoredObjectQuery(
                type: type,
                predicate: HKQuery.predicateForSamples(
                    withStart: Self.bootstrapCutoff, end: nil, options: .strictStartDate),
                anchor: start,
                limit: HKObjectQueryNoLimit
            ) { _, added, deleted, newAnchor, error in
                if let error { cont.resume(throwing: error); return }
                cont.resume(returning: AnchoredResult(
                    added: added ?? [],
                    deleted: (deleted ?? []).map { $0.uuid.uuidString },
                    anchor: newAnchor ?? HKQueryAnchor(fromValue: 0)
                ))
            }
            store.execute(q)
        }
    }

    /// Per-sample heart rate over the workout window, as `time,bpm` CSV
    /// (ISO-8601 UTC). Not part of the DB projection — consumed directly from
    /// the inbox by `tools/session_detail.py` for zone/drift analysis. Returns
    /// "" when the window has no HR samples (e.g. workout without the watch).
    private func hrSeriesCSV(for w: HKWorkout) async throws -> String {
        let unit = HKUnit.count().unitDivided(by: .minute())
        let samples: [HKSample] = try await withCheckedThrowingContinuation { cont in
            let q = HKSampleQuery(
                sampleType: HKQuantityType(.heartRate),
                predicate: HKQuery.predicateForSamples(
                    withStart: w.startDate, end: w.endDate, options: .strictStartDate),
                limit: HKObjectQueryNoLimit,
                sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: true)]
            ) { _, samples, error in
                if let error { cont.resume(throwing: error); return }
                cont.resume(returning: samples ?? [])
            }
            store.execute(q)
        }
        guard !samples.isEmpty else { return "" }
        var csv = "time,bpm\n"
        for case let s as HKQuantitySample in samples {
            csv += "\(DateFormats.iso(s.startDate)),\(Int(s.quantity.doubleValue(for: unit).rounded()))\n"
        }
        return csv
    }

    /// Per-length swim splits over the workout window, as
    /// `start,end,metres` CSV (ISO-8601 UTC).
    ///
    /// HealthKit records one `distanceSwimming` sample per length, each with a
    /// start *and* an end. That is finer than lap events: the swim time for a
    /// length is end − start, and the rest before the next is its start minus
    /// this end. A 200 m is any eight consecutive 25 m lengths, so a benchmark
    /// can be read off the record instead of off the watch by hand.
    ///
    /// Both timestamps are emitted for exactly that reason — the HR sidecar
    /// needs only a start, and copying its shape here would have thrown away
    /// the durations that make this worth having.
    ///
    /// Returns "" for anything that is not a pool swim, which is most workouts.
    private func swimLengthsCSV(for w: HKWorkout) async throws -> String {
        let samples: [HKSample] = try await withCheckedThrowingContinuation { cont in
            let q = HKSampleQuery(
                sampleType: HKQuantityType(.distanceSwimming),
                predicate: HKQuery.predicateForSamples(
                    withStart: w.startDate, end: w.endDate, options: .strictStartDate),
                limit: HKObjectQueryNoLimit,
                sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: true)]
            ) { _, samples, error in
                if let error { cont.resume(throwing: error); return }
                cont.resume(returning: samples ?? [])
            }
            store.execute(q)
        }
        guard !samples.isEmpty else { return "" }
        var csv = "start,end,metres\n"
        for case let s as HKQuantitySample in samples {
            let metres = s.quantity.doubleValue(for: .meter())
            csv += "\(DateFormats.iso(s.startDate)),\(DateFormats.iso(s.endDate)),"
                 + "\(String(format: "%.1f", metres))\n"
        }
        return csv
    }

    /// One-off repair pass for `swim-<uuid>.csv`, the same shape as
    /// `backfillHRSeries`.
    ///
    /// Worth running once: the samples have always been in HealthKit, and only
    /// the export was missing, so every past swim still on this device can be
    /// recovered rather than lost. Safe for the same reason as the HR pass —
    /// sidecars are keyed by uuid and re-writing one cannot double-count.
    func backfillSwimLengths() async throws -> String {
        let workouts = try await workoutsSinceCutoff()
        let dir = try DeltaWriter.folderURL()
        let existing = try writer.existingFileNames()
        var written = 0, present = 0, noLengths = 0
        for w in workouts {
            let name = "swim-\(w.uuid.uuidString).csv"
            if existing.contains(name) { present += 1; continue }
            guard let csv = try? await swimLengthsCSV(for: w), !csv.isEmpty else {
                noLengths += 1
                continue
            }
            try writer.writeSidecar(name: name, content: csv, in: dir)
            written += 1
        }
        return "Swim backfill: \(written) written, \(present) already present, "
             + "\(noLengths) without lengths."
    }

    /// A metadata number in the unit we store, or nil.
    ///
    /// Read through `HKQuantity` rather than off the raw value: HealthKit
    /// writes weather in the device's locale unit, elevation in centimetres and
    /// humidity as a percentage times one hundred, and none of the key names
    /// say so. Asking the quantity for a unit converts; reading `.doubleValue`
    /// and hoping does not.
    private static func metaQuantity(_ w: HKWorkout, _ key: String,
                                     _ unit: HKUnit) -> Double? {
        guard let q = w.metadata?[key] as? HKQuantity, q.is(compatibleWith: unit)
        else { return nil }
        return q.doubleValue(for: unit)
    }

    private static func statsDict(_ all: [HKQuantityType: HKStatistics]) -> [String: [String: Double]] {
        var out: [String: [String: Double]] = [:]
        for (type, stats) in all {
            // The type's own canonical unit, so a consumer never has to guess
            // which one a number is in. Distance and energy are cumulative;
            // rates are averaged.
            let unit: HKUnit
            switch type.identifier {
            case HKQuantityTypeIdentifier.heartRate.rawValue,
                 HKQuantityTypeIdentifier.respiratoryRate.rawValue:
                unit = HKUnit.count().unitDivided(by: .minute())
            case HKQuantityTypeIdentifier.activeEnergyBurned.rawValue,
                 HKQuantityTypeIdentifier.basalEnergyBurned.rawValue:
                unit = .kilocalorie()
            case HKQuantityTypeIdentifier.runningPower.rawValue,
                 HKQuantityTypeIdentifier.cyclingPower.rawValue:
                unit = .watt()
            case HKQuantityTypeIdentifier.cyclingCadence.rawValue:
                unit = HKUnit.count().unitDivided(by: .minute())
            case let id where id.hasPrefix("HKQuantityTypeIdentifierDistance"):
                unit = .meter()
            case HKQuantityTypeIdentifier.runningSpeed.rawValue,
                 HKQuantityTypeIdentifier.cyclingSpeed.rawValue,
                 HKQuantityTypeIdentifier.walkingSpeed.rawValue:
                unit = HKUnit.meterUnit(with: .kilo).unitDivided(by: .hour())
            case HKQuantityTypeIdentifier.runningStrideLength.rawValue,
                 HKQuantityTypeIdentifier.runningVerticalOscillation.rawValue:
                unit = .meter()
            case HKQuantityTypeIdentifier.runningGroundContactTime.rawValue:
                unit = .secondUnit(with: .milli)
            default:
                continue        // unknown unit is worse than no number at all
            }
            var entry: [String: Double] = [:]
            if let v = stats.sumQuantity()?.doubleValue(for: unit) { entry["sum"] = v }
            if let v = stats.averageQuantity()?.doubleValue(for: unit) { entry["avg"] = v }
            if let v = stats.minimumQuantity()?.doubleValue(for: unit) { entry["min"] = v }
            if let v = stats.maximumQuantity()?.doubleValue(for: unit) { entry["max"] = v }
            if !entry.isEmpty {
                entry["_unit_is_canonical"] = 1
                out[type.identifier.replacingOccurrences(
                    of: "HKQuantityTypeIdentifier", with: "")] = entry
            }
        }
        return out
    }

    private static func eventKind(_ t: HKWorkoutEventType) -> String {
        switch t {
        case .pause: return "pause"
        case .resume: return "resume"
        case .lap: return "lap"
        case .marker: return "marker"
        case .motionPaused: return "motionPaused"
        case .motionResumed: return "motionResumed"
        case .segment: return "segment"
        case .pauseOrResumeRequest: return "pauseOrResumeRequest"
        @unknown default: return "unknown"
        }
    }

    private static func makeWorkout(_ w: HKWorkout, routeFile: String?, hrFile: String?,
                                    swimFile: String?) -> Delta.Workout {
        let hr = w.statistics(for: HKQuantityType(.heartRate))
        let hrUnit = HKUnit.count().unitDivided(by: .minute())
        let energy = w.statistics(for: HKQuantityType(.activeEnergyBurned))?
            .sumQuantity()?.doubleValue(for: .kilocalorie())
        let dist = w.totalDistance?.doubleValue(for: .meterUnit(with: .kilo))
        let indoorMeta = w.metadata?[HKMetadataKeyIndoorWorkout] as? Bool

        return Delta.Workout(
            uuid: w.uuid.uuidString,
            activity: Self.activityName(w.workoutActivityType),
            start: DateFormats.appleDate(w.startDate),
            end: DateFormats.appleDate(w.endDate),
            duration_min: w.duration / 60.0,
            distance_km: dist,
            energy_kcal: energy,
            avg_hr: hr?.averageQuantity()?.doubleValue(for: hrUnit),
            max_hr: hr?.maximumQuantity()?.doubleValue(for: hrUnit),
            source: w.sourceRevision.source.name,
            indoor: indoorMeta.map { $0 ? 1 : 0 },
            route_file: routeFile,
            hr_file: hrFile,
            swim_file: swimFile,
            weather_temp_c: metaQuantity(w, HKMetadataKeyWeatherTemperature, .degreeCelsius()),
            // Normalised, not scaled. The first version multiplied by 100 on the
            // assumption that HKUnit.percent() yields a fraction, and shipped
            // "8400 %" — a plausible-looking number in a field nobody checks,
            // which is the exact failure this project exists to stop. The
            // Python side already normalised this way and was right; the Swift
            // guessed and was not.
            weather_humidity_pct: metaQuantity(w, HKMetadataKeyWeatherHumidity, .percent())
                .map { $0 > 100 ? $0 / 100 : $0 },
            elevation_ascended_m: metaQuantity(w, HKMetadataKeyElevationAscended, .meter()),
            elevation_descended_m: metaQuantity(w, HKMetadataKeyElevationDescended, .meter()),
            avg_mets: metaQuantity(w, HKMetadataKeyAverageMETs,
                                   HKUnit.kilocalorie()
                                       .unitDivided(by: HKUnit.gramUnit(with: .kilo)
                                           .unitMultiplied(by: .hour()))),
            pool_length_m: metaQuantity(w, HKMetadataKeyLapLength, .meter()),
            // `HKWorkoutSwimmingLocationType`: unknown 0, pool 1, openWater 2.
            // Read through the enum rather than the integer, because the first
            // version tested `intValue == 1` for *openWater* — exactly
            // backwards — so every pool swim was filed as open water and the
            // one open-water swim as pool. Both are plausible values in a
            // column nothing validates, which is why it stood for a week.
            // `unknown` yields nil instead of falling through to "pool": a
            // default that always answers is what let the inversion hide.
            swim_location: (w.metadata?[HKMetadataKeySwimmingLocationType] as? NSNumber)
                .flatMap { n -> String? in
                    switch HKWorkoutSwimmingLocationType(rawValue: n.intValue) {
                    case .pool:      return "pool"
                    case .openWater: return "openWater"
                    default:         return nil
                    }
                },
            max_speed_kmh: metaQuantity(
                w, HKMetadataKeyMaximumSpeed,
                HKUnit.meterUnit(with: .kilo).unitDivided(by: .hour())),
            segments: w.workoutActivities.isEmpty ? nil :
                w.workoutActivities.enumerated().map { i, a in
                    Delta.Segment(
                        idx: i + 1,
                        activity: Self.activityName(a.workoutConfiguration.activityType),
                        start: DateFormats.appleDate(a.startDate),
                        end: a.endDate.map { DateFormats.appleDate($0) },
                        stats: Self.statsDict(a.allStatistics))
                },
            events: (w.workoutEvents ?? []).isEmpty ? nil :
                (w.workoutEvents ?? []).enumerated().map { i, e in
                    Delta.Event(
                        idx: i + 1,
                        kind: Self.eventKind(e.type),
                        start: DateFormats.appleDate(e.dateInterval.start),
                        end: e.dateInterval.duration > 0
                            ? DateFormats.appleDate(e.dateInterval.end) : nil)
                }
        )
    }

    /// Map an activity type to the same normalised string the full export
    /// produces (HKWorkoutActivityType prefix stripped). Only the activities
    /// that appear in this dataset are named; others fall back to the raw code.
    private static func activityName(_ t: HKWorkoutActivityType) -> String {
        switch t {
        case .running: return "Running"
        case .cycling: return "Cycling"
        case .swimming: return "Swimming"
        case .walking: return "Walking"
        case .hiking: return "Hiking"
        case .traditionalStrengthTraining: return "TraditionalStrengthTraining"
        case .functionalStrengthTraining: return "FunctionalStrengthTraining"
        case .highIntensityIntervalTraining: return "HighIntensityIntervalTraining"
        case .yoga: return "Yoga"
        case .elliptical: return "Elliptical"
        case .rowing: return "Rowing"
        case .crossTraining: return "CrossTraining"
        case .coreTraining: return "CoreTraining"
        case .climbing: return "Climbing"
        default: return "Activity\(t.rawValue)"
        }
    }
}
