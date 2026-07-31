import SwiftUI
import BackgroundTasks

@main
struct HealthSyncApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

/// Registers and schedules the daily background sync.
final class AppDelegate: NSObject, UIApplicationDelegate {
    static let refreshTaskID = "net.dev2.healthsync.refresh"

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: Self.refreshTaskID, using: nil) { task in
            self.handleRefresh(task as! BGAppRefreshTask)
        }
        scheduleRefresh()
        return true
    }

    /// Ask the system to wake us roughly daily for a sync.
    func scheduleRefresh() {
        let request = BGAppRefreshTaskRequest(identifier: Self.refreshTaskID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 12 * 60 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    private func handleRefresh(_ task: BGAppRefreshTask) {
        scheduleRefresh()  // chain the next one
        let work = Task {
            do { _ = try await SyncEngine().sync(); task.setTaskCompleted(success: true) }
            catch { task.setTaskCompleted(success: false) }
        }
        task.expirationHandler = { work.cancel() }
    }
}
