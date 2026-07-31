import AuthenticationServices
import Foundation
import Security

/// Box Platform app credentials. Create a "Custom App" (OAuth 2.0 user
/// authentication) in the Box developer console, add the redirect URI below,
/// grant "Read and write all files and folders", then paste the values here.
/// Personal single-user app: the secret only guards the user's own account.
enum BoxConfig {
    static let clientID = "REPLACE_WITH_CLIENT_ID"
    static let clientSecret = "REPLACE_WITH_CLIENT_SECRET"
    static let redirectURI = "healthsync://box-auth"
    /// Transport folder at the Box root (NOT the Vault — raw data stays out).
    static let folderName = "HealthSync"
}

/// Minimal Box API client: OAuth login, rotating refresh token in the
/// Keychain, uploads with 409 → new-version fallback, folder listing.
///
/// Box refresh tokens are single-use — every refresh returns a new one that
/// MUST be persisted before the old one is discarded, and the chain dies
/// after 60 days unused. Any sync inside that window keeps it alive.
final class BoxClient: NSObject {
    static let shared = BoxClient()

    enum BoxError: LocalizedError {
        case notAuthorized
        case api(String)
        var errorDescription: String? {
            switch self {
            case .notAuthorized: return "Box not connected — tap “Connect Box”."
            case .api(let m): return "Box: \(m)"
            }
        }
    }

    private let keychainAccount = "net.dev2.healthsync.box-refresh-token"
    private var accessToken: String?
    private var cachedFolderID: String? {
        get { UserDefaults.standard.string(forKey: "box.folderID") }
        set { UserDefaults.standard.set(newValue, forKey: "box.folderID") }
    }

    var isAuthorized: Bool { loadRefreshToken() != nil }

    // MARK: - OAuth

    /// Interactive login via ASWebAuthenticationSession; stores the refresh
    /// token. Call once (and again only if the chain ever dies).
    @MainActor
    func authorize() async throws {
        let state = UUID().uuidString
        var comps = URLComponents(string: "https://account.box.com/api/oauth2/authorize")!
        comps.queryItems = [
            .init(name: "client_id", value: BoxConfig.clientID),
            .init(name: "response_type", value: "code"),
            .init(name: "redirect_uri", value: BoxConfig.redirectURI),
            .init(name: "state", value: state),
        ]
        let scheme = URL(string: BoxConfig.redirectURI)!.scheme!
        let callback: URL = try await withCheckedThrowingContinuation { cont in
            let session = ASWebAuthenticationSession(
                url: comps.url!, callbackURLScheme: scheme
            ) { url, error in
                if let url { cont.resume(returning: url) }
                else { cont.resume(throwing: error ?? BoxError.api("login cancelled")) }
            }
            session.presentationContextProvider = self
            session.start()
        }
        let items = URLComponents(url: callback, resolvingAgainstBaseURL: false)?.queryItems
        guard items?.first(where: { $0.name == "state" })?.value == state,
              let code = items?.first(where: { $0.name == "code" })?.value else {
            throw BoxError.api("bad OAuth callback")
        }
        try await exchangeToken(params: ["grant_type": "authorization_code", "code": code,
                                         "redirect_uri": BoxConfig.redirectURI])
    }

    private func refresh() async throws {
        guard let token = loadRefreshToken() else { throw BoxError.notAuthorized }
        try await exchangeToken(params: ["grant_type": "refresh_token", "refresh_token": token])
    }

    private func exchangeToken(params: [String: String]) async throws {
        var req = URLRequest(url: URL(string: "https://api.box.com/oauth2/token")!)
        req.httpMethod = "POST"
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        let all = params.merging(["client_id": BoxConfig.clientID,
                                  "client_secret": BoxConfig.clientSecret]) { a, _ in a }
        req.httpBody = all.map { "\($0)=\($1.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? $1)" }
            .joined(separator: "&").data(using: .utf8)
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode == 200,
              let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let access = json["access_token"] as? String,
              let newRefresh = json["refresh_token"] as? String else {
            throw BoxError.api("token exchange failed (\((resp as? HTTPURLResponse)?.statusCode ?? 0)) — reconnect Box if this persists")
        }
        // Rotation: persist the new refresh token before anything else.
        try storeRefreshToken(newRefresh)
        accessToken = access
    }

    // MARK: - Requests

    private func send(_ makeRequest: (String) -> URLRequest) async throws -> (Data, HTTPURLResponse) {
        if accessToken == nil { try await refresh() }
        for attempt in 0...1 {
            let (data, resp) = try await URLSession.shared.data(for: makeRequest(accessToken!))
            let http = resp as! HTTPURLResponse
            if http.statusCode == 401 && attempt == 0 { try await refresh(); continue }
            return (data, http)
        }
        fatalError("unreachable")
    }

    private func json(_ data: Data) -> [String: Any] {
        (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    // MARK: - Folders / files

    /// Id of the transport folder at the Box root, created if missing.
    func folderID() async throws -> String {
        if let id = cachedFolderID { return id }
        for item in try await listItems(inFolder: "0") where
            item.type == "folder" && item.name == BoxConfig.folderName {
            cachedFolderID = item.id
            return item.id
        }
        let req0: (String) -> URLRequest = { token in
            var r = URLRequest(url: URL(string: "https://api.box.com/2.0/folders")!)
            r.httpMethod = "POST"
            r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = try? JSONSerialization.data(withJSONObject:
                ["name": BoxConfig.folderName, "parent": ["id": "0"]])
            return r
        }
        let (data, resp) = try await send(req0)
        guard resp.statusCode == 201, let id = json(data)["id"] as? String else {
            throw BoxError.api("cannot create folder (\(resp.statusCode))")
        }
        cachedFolderID = id
        return id
    }

    struct Item { let id: String; let name: String; let type: String }

    /// All items in a folder (paginates past 1000).
    func listItems(inFolder folderID: String) async throws -> [Item] {
        var items: [Item] = []
        var offset = 0
        while true {
            let url = URL(string:
                "https://api.box.com/2.0/folders/\(folderID)/items?limit=1000&offset=\(offset)&fields=id,name,type")!
            let (data, resp) = try await send { token in
                var r = URLRequest(url: url)
                r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                return r
            }
            guard resp.statusCode == 200 else { throw BoxError.api("list failed (\(resp.statusCode))") }
            let body = json(data)
            let entries = (body["entries"] as? [[String: Any]]) ?? []
            items += entries.compactMap {
                guard let id = $0["id"] as? String, let name = $0["name"] as? String,
                      let type = $0["type"] as? String else { return nil }
                return Item(id: id, name: name, type: type)
            }
            offset += entries.count
            let total = (body["total_count"] as? Int) ?? offset
            if offset >= total || entries.isEmpty { return items }
        }
    }

    /// File names already in the transport folder (for backfill skip-existing).
    func existingFileNames() async throws -> Set<String> {
        Set(try await listItems(inFolder: folderID()).filter { $0.type == "file" }.map(\.name))
    }

    /// Upload `name` into the transport folder; a 409 name conflict becomes a
    /// new-version upload of the existing file.
    func upload(name: String, content: Data) async throws {
        let folder = try await folderID()
        let (data, resp) = try await send { token in
            Self.multipartRequest(
                url: URL(string: "https://upload.box.com/api/2.0/files/content")!,
                token: token, name: name, content: content,
                attributes: ["name": name, "parent": ["id": folder]])
        }
        if resp.statusCode == 409 {
            let conflictID = ((json(data)["context_info"] as? [String: Any])?["conflicts"]
                              as? [String: Any])?["id"] as? String
            guard let conflictID else { throw BoxError.api("409 without conflict id") }
            let (_, vresp) = try await send { token in
                Self.multipartRequest(
                    url: URL(string: "https://upload.box.com/api/2.0/files/\(conflictID)/content")!,
                    token: token, name: name, content: content, attributes: nil)
            }
            guard vresp.statusCode == 201 else { throw BoxError.api("version upload failed (\(vresp.statusCode))") }
            return
        }
        guard resp.statusCode == 201 else { throw BoxError.api("upload of \(name) failed (\(resp.statusCode))") }
    }

    private static func multipartRequest(url: URL, token: String, name: String,
                                         content: Data, attributes: [String: Any]?) -> URLRequest {
        let boundary = "healthsync-\(UUID().uuidString)"
        var body = Data()
        func part(_ s: String) { body.append(s.data(using: .utf8)!) }
        if let attributes,
           let attrs = try? JSONSerialization.data(withJSONObject: attributes) {
            part("--\(boundary)\r\nContent-Disposition: form-data; name=\"attributes\"\r\n\r\n")
            body.append(attrs)
            part("\r\n")
        }
        part("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(name)\"\r\n")
        part("Content-Type: application/octet-stream\r\n\r\n")
        body.append(content)
        part("\r\n--\(boundary)--\r\n")

        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        return req
    }

    // MARK: - Keychain

    private func loadRefreshToken() -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: keychainAccount,
            kSecReturnData as String: true,
        ]
        var out: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private func storeRefreshToken(_ token: String) throws {
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: keychainAccount,
        ]
        let attrs: [String: Any] = [kSecValueData as String: token.data(using: .utf8)!]
        let status = SecItemUpdate(base as CFDictionary, attrs as CFDictionary)
        if status == errSecItemNotFound {
            let add = base.merging(attrs) { a, _ in a }
            guard SecItemAdd(add as CFDictionary, nil) == errSecSuccess else {
                throw BoxError.api("keychain write failed")
            }
        } else if status != errSecSuccess {
            throw BoxError.api("keychain update failed (\(status))")
        }
    }
}

extension BoxClient: ASWebAuthenticationPresentationContextProviding {
    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        ASPresentationAnchor()
    }
}
