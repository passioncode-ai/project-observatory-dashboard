import Foundation

/// What the dashboard view does with a navigation it did not start itself.
public enum NavigationDecision: Equatable, Sendable { case allow, openExternally, deny }

/// The one place a dashboard address is judged. The window shows exactly one
/// workspace's dashboard — its verified loopback server, or its built pages on disk —
/// and nothing else: another site, another local server or a file outside the
/// pages opens in the user's browser or not at all, never inside this window.
public struct DashboardOrigin: Equatable, Sendable {
    public let base: URL
    /// Schemes handed to the system: the web and mail. The pages emit no other kind.
    public static let externalSchemes: Set<String> = ["http", "https", "mailto"]

    /// A verified server: `http://127.0.0.1:<port>/dashboard/index.html`.
    public static func live(_ url: URL) -> DashboardOrigin? {
        guard url.scheme == "http", url.host == "127.0.0.1", url.port != nil, url.user == nil, url.password == nil,
              url.path == "/dashboard/index.html" else { return nil }
        return DashboardOrigin(base: url)
    }
    /// The built pages: `…/docs/dashboard/index.html` inside the workspace.
    public static func files(_ path: String, workspace: String) -> DashboardOrigin? {
        let url = URL(fileURLWithPath: path).standardizedFileURL
        let root = URL(fileURLWithPath: workspace).standardizedFileURL.path + "/"
        guard path.hasPrefix("/"), url.path.hasPrefix(root), url.lastPathComponent == "index.html" else { return nil }
        return DashboardOrigin(base: url)
    }
    public var isLive: Bool { base.scheme == "http" }
    /// The directory a file dashboard may read and navigate within.
    public var directory: URL { base.deletingLastPathComponent() }

    public func decide(_ url: URL) -> NavigationDecision {
        let scheme = url.scheme?.lowercased() ?? ""
        if scheme == "about" { return .allow }                       // about:blank, srcdoc frames
        if isLive, scheme == "http", url.host == base.host, url.port == base.port, url.user == nil { return .allow }
        if !isLive, scheme == "file" {
            let dir = directory.standardizedFileURL.path + "/"
            return url.standardizedFileURL.path.hasPrefix(dir) ? .allow : .deny
        }
        if Self.externalSchemes.contains(scheme) { return .openExternally }
        return .deny
    }
}
