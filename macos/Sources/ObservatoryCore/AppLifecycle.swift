import Foundation

/// The app's lifecycle defaults (operator rule, 2026-10-16, for every product that ships an
/// app): it opens at login unless the person turned that off, it checks at every launch
/// that what it needs is granted and says what is missing, it keeps working with its
/// window closed, and a ready update asks once to restart.
///
/// Everything here is a pure decision over plain values, so the tests drive it without
/// AppKit, ServiceManagement or a login session; `ObservatoryApp` applies the decisions.

/// What `SMAppService.mainApp.status` says, as the decisions need it.
public enum LoginItemStatus: String, Equatable, Sendable {
    /// Registered and allowed: the app opens at login.
    case enabled
    /// Not registered (never, or unregistered).
    case notRegistered
    /// Registered, but the person must allow it in System Settings › General › Login Items
    /// (or switched it off there).
    case requiresApproval
    /// The system cannot find the app's service (a build outside a bundle, `swift run`).
    case notFound
}

/// Open at login: on by default, the person's choice kept across launches and updates.
public enum LoginItemPolicy {
    /// The UserDefaults key holding the person's choice. Absent: the app never launched
    /// with this policy yet, which is the moment the default (on) is applied.
    public static let key = "openAtLogin"

    public enum Action: String, Equatable, Sendable { case register, unregister, keep }

    public struct Decision: Equatable, Sendable {
        public var action: Action
        /// The choice to store; it is always stored after a decision, so the default is
        /// applied exactly once.
        public var stored: Bool
        public init(action: Action, stored: Bool) { self.action = action; self.stored = stored }
    }

    /// At every launch, and when the Settings toggle changes.
    ///
    /// - no choice yet: the first launch, so the default applies: register, store on;
    /// - choice off: never registered, and a registration left over is removed;
    /// - choice on: registered again if the system lost it (a bundle replaced by an
    ///   update keeps its registration, but a moved one does not); one waiting for the
    ///   person's approval is kept as it is: registering again cannot grant it.
    public static func decide(stored: Bool?, status: LoginItemStatus) -> Decision {
        switch (stored ?? true, status) {
        case (true, .notRegistered), (true, .notFound): return Decision(action: .register, stored: true)
        case (true, _): return Decision(action: .keep, stored: true)
        case (false, .enabled), (false, .requiresApproval): return Decision(action: .unregister, stored: false)
        case (false, _): return Decision(action: .keep, stored: false)
        }
    }
}

/// A permission the app needs and does not have, shown at launch with how to grant it.
public struct MissingPermission: Equatable, Sendable {
    /// A code for the log and the tests.
    public let code: String
    /// The English text (a key of the Russian dictionary) naming what is missing.
    public let title: String
    /// How to grant it, and how to stop needing it.
    public let detail: String
    public init(code: String, title: String, detail: String) { self.code = code; self.title = title; self.detail = detail }
}

/// Checked at every launch: no permission the app needs is allowed to be missing silently.
///
/// Today the app needs no privacy (TCC) permission: it reads its own workspace and talks to
/// its engine on 127.0.0.1. The one grant it depends on is the login item's approval. A new
/// requirement is added here, with its test, so the launch check names it too.
public enum AppPermissions {
    public static let loginItemCode = "login-item-not-allowed"
    public static let loginItemTitle = "Project Observatory cannot open at login"
    public static let loginItemDetail = "Allow it in System Settings › General › Login Items, or turn off “Open at login” in Project Observatory Settings."

    /// - `wanted`: the person's choice (open at login on);
    /// - `status`: the login item's status after the launch's decision was applied;
    /// - `registerFailed`: the registration this launch attempted threw.
    public static func missing(wanted: Bool, status: LoginItemStatus, registerFailed: Bool) -> [MissingPermission] {
        guard wanted else { return [] }
        if registerFailed || status == .requiresApproval {
            return [MissingPermission(code: loginItemCode, title: loginItemTitle, detail: loginItemDetail)]
        }
        return []
    }
}

/// «Restart to finish updating»: asked once per staged version, when the person can see it.
public enum UpdatePrompt {
    /// The UserDefaults key holding the last version the prompt was shown for.
    public static let key = "updatePromptedVersion"

    /// Show the prompt now? Only for a staged version not asked about yet, and only while a
    /// window of the app is on screen: a prompt nobody sees is not shown (the idle restart
    /// handles an app nobody uses). «Later» is remembered by storing the version, so the
    /// same update never asks twice; the menu item and the toolbar button stay.
    public static func shouldPrompt(staged: String?, prompted: String?, windowOnScreen: Bool) -> Bool {
        guard let staged, AppVersion.parse(staged) != nil, windowOnScreen else { return false }
        return staged != prompted
    }
}

/// Closing the last window hides it: the app keeps running in the Dock, keeps reading its
/// update record and keeps its idle restart; it quits only through its own Quit (⌘Q) or a
/// system shutdown or logout.
public enum WindowClosePolicy {
    public static let terminateAfterLastWindowClosed = false
}
