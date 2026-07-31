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
            delta.workouts.added.append(Self.makeWorkout(w, routeFile: routeFile, hrFile: hrFile))
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

    private static func makeWorkout(_ w: HKWorkout, routeFile: String?, hrFile: String?) -> Delta.Workout {
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
            hr_file: hrFile
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
