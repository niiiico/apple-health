import Foundation
import HealthKit

/// Reads exactly what the Vault files need, straight from HealthKit.
///
/// There is no database, no anchor, and no delta (ADR-005). Every render is a
/// fresh query over a bounded window, so nothing accumulates and nothing can
/// drift out of sync with the phone's own data.
struct HealthQueries {
    let store = HKHealthStore()

    /// Only what the renderers consume — a much smaller ask than HealthSync's
    /// full catalogue, because we no longer mirror the whole store.
    static var readTypes: Set<HKObjectType> {
        [
            HKObjectType.workoutType(),
            HKQuantityType.quantityType(forIdentifier: .heartRate)!,
            HKQuantityType.quantityType(forIdentifier: .restingHeartRate)!,
            HKQuantityType.quantityType(forIdentifier: .heartRateVariabilitySDNN)!,
        ]
    }

    func requestAuthorization() async throws {
        guard HKHealthStore.isHealthDataAvailable() else {
            throw VaultError.message("HealthKit unavailable on this device.")
        }
        try await store.requestAuthorization(toShare: [], read: Self.readTypes)
    }

    // MARK: - Workouts

    /// Workouts overlapping `[from, to)`, oldest first.
    func workouts(from: Date, to: Date) async throws -> [Session] {
        let predicate = HKQuery.predicateForSamples(withStart: from, end: to, options: .strictStartDate)
        return try await runWorkoutQuery(predicate: predicate, limit: HKObjectQueryNoLimit, ascending: true)
    }

    /// The `limit` most recent workouts of one activity, newest first.
    func recent(activity: HKWorkoutActivityType, limit: Int) async throws -> [Session] {
        let predicate = HKQuery.predicateForWorkouts(with: activity)
        return try await runWorkoutQuery(predicate: predicate, limit: limit, ascending: false)
    }

    private func runWorkoutQuery(predicate: NSPredicate, limit: Int,
                                 ascending: Bool) async throws -> [Session] {
        try await withCheckedThrowingContinuation { cont in
            let sort = NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: ascending)
            let q = HKSampleQuery(sampleType: .workoutType(), predicate: predicate,
                                  limit: limit, sortDescriptors: [sort]) { _, samples, error in
                if let error { return cont.resume(throwing: error) }
                let workouts = (samples as? [HKWorkout]) ?? []
                cont.resume(returning: workouts.map(Self.session(from:)))
            }
            store.execute(q)
        }
    }

    private static func session(from w: HKWorkout) -> Session {
        // statistics(for:) is the modern replacement for the deprecated
        // totalDistance/totalEnergyBurned, and is what the full export's
        // WorkoutStatistics rows come from — so these match the Mac's numbers.
        let distance = w.statistics(for: HKQuantityType(.distanceWalkingRunning))?.sumQuantity()
            ?? w.statistics(for: HKQuantityType(.distanceCycling))?.sumQuantity()
            ?? w.statistics(for: HKQuantityType(.distanceSwimming))?.sumQuantity()
        let energy = w.statistics(for: HKQuantityType(.activeEnergyBurned))?.sumQuantity()
        let hr = w.statistics(for: HKQuantityType(.heartRate))
        let bpm = HKUnit.count().unitDivided(by: .minute())

        return Session(
            uuid: w.uuid,
            activity: activityName(w.workoutActivityType),
            start: w.startDate,
            durationMin: w.duration / 60,
            distanceKm: distance?.doubleValue(for: .meterUnit(with: .kilo)),
            energyKcal: energy?.doubleValue(for: .kilocalorie()),
            avgHR: hr?.averageQuantity()?.doubleValue(for: bpm),
            maxHR: hr?.maximumQuantity()?.doubleValue(for: bpm)
        )
    }

    /// Normalised activity name, matching what the full export produces.
    ///
    /// `swimBikeRun` is listed here deliberately: HealthSync's map omits it, so
    /// the delta path files triathlons as `Activity<rawValue>` while the full
    /// export calls them `SwimBikeRun`. The weekly brief keys "always
    /// significant" off that name, so the omission would silently drop races.
    static func activityName(_ t: HKWorkoutActivityType) -> String {
        switch t {
        case .running: return "Running"
        case .cycling: return "Cycling"
        case .swimming: return "Swimming"
        case .swimBikeRun: return "SwimBikeRun"
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
        case .downhillSkiing: return "DownhillSkiing"
        case .snowboarding: return "Snowboarding"
        case .paddleSports: return "PaddleSports"
        case .underwaterDiving: return "UnderwaterDiving"
        case .surfingSports: return "SurfingSports"
        default: return "Activity\(t.rawValue)"
        }
    }

    // MARK: - Series

    /// Per-sample heart rate across one workout, for zones and drift.
    ///
    /// This is the series HealthSync had to ship as an `hr-<uuid>.csv` sidecar
    /// and backfill by hand when older deltas lacked it. On-device it is just a
    /// query — there is nothing to miss and nothing to repair.
    func heartRateSeries(for uuid: UUID, start: Date, end: Date) async throws -> [(Date, Double)] {
        let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: .strictStartDate)
        let bpm = HKUnit.count().unitDivided(by: .minute())
        return try await withCheckedThrowingContinuation { cont in
            let sort = NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: true)
            let q = HKSampleQuery(sampleType: HKQuantityType(.heartRate), predicate: predicate,
                                  limit: HKObjectQueryNoLimit, sortDescriptors: [sort]) { _, samples, error in
                if let error { return cont.resume(throwing: error) }
                let vals = (samples as? [HKQuantitySample] ?? [])
                    .map { ($0.startDate, $0.quantity.doubleValue(for: bpm)) }
                cont.resume(returning: vals)
            }
            store.execute(q)
        }
    }

    /// Mean of a sparse quantity type over `[from, to)`, or nil when no samples.
    func average(_ id: HKQuantityTypeIdentifier, unit: HKUnit,
                 from: Date, to: Date) async throws -> Double? {
        let predicate = HKQuery.predicateForSamples(withStart: from, end: to, options: .strictStartDate)
        return try await withCheckedThrowingContinuation { cont in
            let q = HKStatisticsQuery(quantityType: HKQuantityType(id), quantitySamplePredicate: predicate,
                                      options: .discreteAverage) { _, stats, error in
                if let error {
                    // No samples in the window is reported as an error by
                    // HKStatisticsQuery; it is a normal state, not a failure.
                    let ns = error as NSError
                    if ns.domain == HKError.errorDomain, ns.code == HKError.errorNoData.rawValue {
                        return cont.resume(returning: nil)
                    }
                    return cont.resume(throwing: error)
                }
                cont.resume(returning: stats?.averageQuantity()?.doubleValue(for: unit))
            }
            store.execute(q)
        }
    }
}

enum VaultError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let m) = self { return m }; return nil }
}
