import SwiftUI

struct ContentView: View {
    @State private var status = "Ready."
    @State private var busy = false
    @State private var boxConnected = BoxClient.shared.isAuthorized
    @State private var preview: [String: String] = [:]

    private let refresh = VaultRefresh()

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Button("Grant Health access") { run(grantHealth) }
                    Button(boxConnected ? "Reconnect Box" : "Connect Box") { run(connectBox) }
                } header: {
                    Text("Setup")
                } footer: {
                    Text(BoxConfig.useStaging
                         ? "Staging mode — writes to “\(BoxConfig.stagingFolderName)”, not the live Vault."
                         : "Live mode — writes directly to the Vault.")
                }

                Section("Actions") {
                    Button("Preview (render only)") { run(previewOnly) }
                    Button("Refresh Vault") { run(push) }
                        .disabled(!boxConnected)
                }

                Section("Status") {
                    Text(status).font(.footnote).monospaced()
                }

                if !preview.isEmpty {
                    Section("Rendered") {
                        ForEach(preview.keys.sorted(), id: \.self) { name in
                            NavigationLink(name) {
                                ScrollView {
                                    Text(preview[name] ?? "")
                                        .font(.system(.caption, design: .monospaced))
                                        .textSelection(.enabled)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                        .padding()
                                }
                                .navigationTitle(name)
                            }
                        }
                    }
                }
            }
            .navigationTitle("VaultBrief")
            .overlay { if busy { ProgressView().controlSize(.large) } }
        }
    }

    // MARK: - Actions

    private func grantHealth() async throws {
        try await refresh.health.requestAuthorization()
        status = "Health access granted."
    }

    private func connectBox() async throws {
        try await BoxClient.shared.authorize()
        boxConnected = BoxClient.shared.isAuthorized
        status = "Box connected."
    }

    /// Render without uploading — the safe way to check output, and how the
    /// side-by-side comparison against the Mac's Vault files is done.
    private func previewOnly() async throws {
        preview = try await refresh.render()
        status = "Rendered \(preview.count) file(s); nothing uploaded."
    }

    private func push() async throws {
        let result = try await refresh.run()
        preview = result.rendered
        let pushed = result.pushed.isEmpty ? "nothing" : result.pushed.joined(separator: ", ")
        status = "Pushed: \(pushed).\nUnchanged: \(result.unchanged.count) file(s)."
    }

    private func run(_ action: @escaping () async throws -> Void) {
        busy = true
        Task {
            do { try await action() }
            catch { status = "Failed — \(error.localizedDescription)" }
            busy = false
        }
    }
}
