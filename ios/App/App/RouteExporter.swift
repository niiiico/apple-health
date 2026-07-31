import HealthKit
import CoreLocation

/// Streams a workout's `HKWorkoutRoute` into a GPX 1.1 string matching the
/// shape of Apple's `workout-routes/*.gpx`, so the existing `parse_gpx`
/// summariser reads it unchanged.
struct RouteExporter {
    let store: HKHealthStore

    /// Returns GPX text for the workout's route, or nil if it has none.
    func gpx(for workout: HKWorkout) async throws -> String? {
        guard let route = try await firstRoute(for: workout) else { return nil }
        let locations = try await locations(for: route)
        guard !locations.isEmpty else { return nil }
        return Self.render(locations)
    }

    /// The route series object attached to a workout (workouts have at most one).
    private func firstRoute(for workout: HKWorkout) async throws -> HKWorkoutRoute? {
        try await withCheckedThrowingContinuation { cont in
            let predicate = HKQuery.predicateForObjects(from: workout)
            let q = HKAnchoredObjectQuery(
                type: HKSeriesType.workoutRoute(),
                predicate: predicate,
                anchor: nil,
                limit: HKObjectQueryNoLimit
            ) { _, samples, _, _, error in
                if let error { cont.resume(throwing: error); return }
                cont.resume(returning: samples?.first as? HKWorkoutRoute)
            }
            store.execute(q)
        }
    }

    /// Drains all CLLocations from a route series (delivered in batches).
    private func locations(for route: HKWorkoutRoute) async throws -> [CLLocation] {
        try await withCheckedThrowingContinuation { cont in
            var all: [CLLocation] = []
            let q = HKWorkoutRouteQuery(route: route) { _, batch, done, error in
                if let error { cont.resume(throwing: error); return }
                if let batch { all.append(contentsOf: batch) }
                if done { cont.resume(returning: all) }
            }
            store.execute(q)
        }
    }

    private static let isoFormatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    private static func render(_ locations: [CLLocation]) -> String {
        var gpx = """
        <?xml version="1.0" encoding="UTF-8"?>
        <gpx version="1.1" creator="HealthSync" xmlns="http://www.topografix.com/GPX/1/1">
        <trk><trkseg>

        """
        for loc in locations {
            let lat = loc.coordinate.latitude
            let lon = loc.coordinate.longitude
            let ele = loc.altitude
            let time = isoFormatter.string(from: loc.timestamp)
            gpx += "<trkpt lat=\"\(lat)\" lon=\"\(lon)\"><ele>\(ele)</ele><time>\(time)</time></trkpt>\n"
        }
        gpx += "</trkseg></trk></gpx>\n"
        return gpx
    }
}
