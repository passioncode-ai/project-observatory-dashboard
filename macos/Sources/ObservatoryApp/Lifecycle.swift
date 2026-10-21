import SwiftUI
import AppKit
import ServiceManagement
import ObservatoryCore

/// Open at login, on by default (operator rule, 2026-10-16), and the launch-time check of
/// what the app needs (AppLifecycle.swift). `apply()` runs at every launch; the Settings
/// toggle writes `enabled`. The system calls are injected, so the tests never register a
/// login item for the person running them.
@MainActor final class LoginItem: ObservableObject {
    /// What is missing after the last `apply()`, shown as a banner in the dashboard window.
    @Published private(set) var missing: [MissingPermission] = []
    private let defaults: UserDefaults
    private let log: AppLog
    private let status: () -> LoginItemStatus
    private let register: () throws -> Void
    private let unregister: () throws -> Void

    init(defaults: UserDefaults = .standard, log: AppLog = .standard(),
         status: @escaping () -> LoginItemStatus = LoginItem.systemStatus,
         register: @escaping () throws -> Void = { try SMAppService.mainApp.register() },
         unregister: @escaping () throws -> Void = { try SMAppService.mainApp.unregister() }) {
        self.defaults = defaults; self.log = log; self.status = status
        self.register = register; self.unregister = unregister
    }

    nonisolated static func systemStatus() -> LoginItemStatus {
        switch SMAppService.mainApp.status {
        case .enabled: return .enabled
        case .requiresApproval: return .requiresApproval
        case .notFound: return .notFound
        default: return .notRegistered
        }
    }

    /// The person's choice; absent until the first launch applies the default (on).
    var enabled: Bool {
        get { defaults.object(forKey: LoginItemPolicy.key) as? Bool ?? true }
        set {
            objectWillChange.send()
            defaults.set(newValue, forKey: LoginItemPolicy.key)
            missing = apply()
        }
    }

    /// Decide, act, store the choice, and say what is missing. Every outcome is logged by
    /// code; nothing degrades silently.
    @discardableResult func apply() -> [MissingPermission] {
        let decision = LoginItemPolicy.decide(stored: defaults.object(forKey: LoginItemPolicy.key) as? Bool, status: status())
        defaults.set(decision.stored, forKey: LoginItemPolicy.key)
        var failed = false
        switch decision.action {
        case .register:
            do { try register(); log.record("login_item", "registered") }
            catch { failed = true; log.record("login_item", "failed", ["action": "register"]) }
        case .unregister:
            do { try unregister(); log.record("login_item", "unregistered") }
            catch { log.record("login_item", "failed", ["action": "unregister"]) }
        case .keep: break
        }
        let now = status()
        if decision.stored && now == .requiresApproval { log.record("login_item", "requires_approval") }
        let found = AppPermissions.missing(wanted: decision.stored, status: now, registerFailed: failed)
        for item in found { log.record("app_permission", "missing", ["permission": item.code]) }
        missing = found
        return found
    }

    /// System Settings › General › Login Items, where the person allows it.
    func openSystemSettings() { SMAppService.openSystemSettingsLoginItems() }
}

/// The dashboard's banner for a permission the app needs and does not have: what is
/// missing, how to grant it, and a button to the place where it is granted.
struct PermissionBanner: View {
    @EnvironmentObject var m: Model
    @EnvironmentObject var loginItem: LoginItem
    var body: some View {
        ForEach(loginItem.missing, id: \.code) { item in
            if item.code == AppPermissions.loginItemCode {
                Banner(tone: .warning, symbol: "lock.shield",
                       title: m.t("Project Observatory cannot open at login"),
                       detail: m.t("Allow it in System Settings › General › Login Items, or turn off “Open at login” in Project Observatory Settings.")) {
                    Button(m.t("Open System Settings")) { loginItem.openSystemSettings() }
                        .buttonStyle(SecondaryButtonStyle())
                }
            }
        }
    }
}

/// «Restart to finish updating»: Restart now, or Later (the menu item and the toolbar
/// button stay). Shown once per version by `Updates.promptIfDue`.
enum UpdateAlert {
    @MainActor static func show(_ m: Model, version: String, answer: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = m.t("Restart to finish updating")
        alert.informativeText = m.t("Version {version} is downloaded and verified. Restart Project Observatory now to finish updating, or later: it also installs when you quit.", ["version": version])
        alert.addButton(withTitle: m.t("Restart now"))
        alert.addButton(withTitle: m.t("Later"))
        if let window = NSApp.keyWindow ?? NSApp.windows.first(where: { $0.isVisible && $0.styleMask.contains(.titled) }) {
            alert.beginSheetModal(for: window) { answer($0 == .alertFirstButtonReturn) }
        } else {
            answer(alert.runModal() == .alertFirstButtonReturn)
        }
    }
}
