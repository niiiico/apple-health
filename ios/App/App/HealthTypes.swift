import HealthKit

/// The catalogue of HealthKit types HealthSync observes, and the *fixed* unit
/// each quantity type is read in.
///
/// The unit per type MUST stay constant for the life of the install — the
/// `ah-ingest` side adds `sum`/`min`/`max` across deltas, which is only valid
/// when the unit never changes (see ../../../docs/delta-contract.md).
enum HealthTypes {

    /// A quantity type plus the unit it is always read in, and the normalised
    /// name written to the delta (HK identifier prefix already stripped).
    struct Quantity {
        let identifier: HKQuantityTypeIdentifier
        let unit: HKUnit
        let name: String

        var type: HKQuantityType { HKQuantityType.quantityType(forIdentifier: identifier)! }
    }

    /// Dense types — folded on-device into per-(day,type) buckets and merged
    /// additively into `daily_metrics`.
    static let dense: [Quantity] = [
        .init(identifier: .heartRate,                 unit: .count().unitDivided(by: .minute()), name: "HeartRate"),
        .init(identifier: .activeEnergyBurned,        unit: .kilocalorie(),                      name: "ActiveEnergyBurned"),
        .init(identifier: .basalEnergyBurned,         unit: .kilocalorie(),                      name: "BasalEnergyBurned"),
        .init(identifier: .distanceWalkingRunning,    unit: .meterUnit(with: .kilo),             name: "DistanceWalkingRunning"),
        .init(identifier: .distanceCycling,           unit: .meterUnit(with: .kilo),             name: "DistanceCycling"),
        .init(identifier: .stepCount,                 unit: .count(),                            name: "StepCount"),
        .init(identifier: .flightsClimbed,            unit: .count(),                            name: "FlightsClimbed"),
        .init(identifier: .appleExerciseTime,         unit: .minute(),                           name: "AppleExerciseTime"),
        .init(identifier: .runningSpeed,              unit: .meterUnit(with: .kilo).unitDivided(by: .hour()), name: "RunningSpeed"),
        .init(identifier: .walkingSpeed,              unit: .meterUnit(with: .kilo).unitDivided(by: .hour()), name: "WalkingSpeed"),
        .init(identifier: .runningStrideLength,       unit: .meter(),                            name: "RunningStrideLength"),
        .init(identifier: .runningPower,              unit: .watt(),                             name: "RunningPower"),
        .init(identifier: .runningGroundContactTime,  unit: .secondUnit(with: .milli),           name: "RunningGroundContactTime"),
        .init(identifier: .runningVerticalOscillation, unit: .meterUnit(with: .centi),           name: "RunningVerticalOscillation"),
        .init(identifier: .respiratoryRate,           unit: .count().unitDivided(by: .minute()), name: "RespiratoryRate"),
        .init(identifier: .oxygenSaturation,          unit: .percent(),                          name: "OxygenSaturation"),
    ]

    /// Sparse, high-value types — carried as raw rows into `records`.
    /// MUST mirror `parse_export.SPARSE_TYPES` on the consumer side.
    static let sparse: [Quantity] = [
        .init(identifier: .restingHeartRate,          unit: .count().unitDivided(by: .minute()), name: "RestingHeartRate"),
        .init(identifier: .vo2Max,        unit: HKUnit(from: "ml/kg*min"),                       name: "VO2Max"),
        .init(identifier: .bodyMass,                  unit: .gramUnit(with: .kilo),              name: "BodyMass"),
        .init(identifier: .heartRateVariabilitySDNN,  unit: .secondUnit(with: .milli),           name: "HeartRateVariabilitySDNN"),
        .init(identifier: .walkingHeartRateAverage,   unit: .count().unitDivided(by: .minute()), name: "WalkingHeartRateAverage"),
        .init(identifier: .bodyFatPercentage,         unit: .percent(),                          name: "BodyFatPercentage"),
    ]

    /// Everything we request read authorisation for: dense + sparse quantities,
    /// workouts, and workout routes.
    static var readTypes: Set<HKObjectType> {
        var types = Set<HKObjectType>()
        for q in dense { types.insert(q.type) }
        for q in sparse { types.insert(q.type) }
        types.insert(HKObjectType.workoutType())
        types.insert(HKSeriesType.workoutRoute())
        return types
    }
}
