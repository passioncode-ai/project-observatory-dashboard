import SwiftUI
import AppKit
import ObservatoryCore

/// Activating a staged update (LC-16): the engine's hourly pass has verified the new app
/// and waits for this one to quit. This object finds that out — at launch and every
/// 15 minutes — shows it on the Dock icon, and activates it at a safe point: the person's
/// *Restart to update*, the person's quit, or 30 minutes with no window on screen and no
/// input. A running session is never stopped for it: the assistant's job is detached and
/// resumes after the restart.
@MainActor final class Updates: ObservableObject {
    /// Reading the local record costs nothing; every 6 h made the prompt appear hours after
    /// the update was ready (the operator, 2026-10-08). The engine stages hourly.
    static let checkEvery: TimeInterval = 15 * 60
    /// The Dock badge while an update is staged: seen without opening a window.
    static let badgeLabel = "↑"
    @Published private(set) var staged: StagedUpdate? {
        didSet { badge(staged == nil ? nil : Self.badgeLabel) }
    }
    /// Why the last *Restart to update* could not start, as a code; nil after a success.
    @Published private(set) var refusal: String?

    /// The running app's version; an unreadable one (`swift run`) is never behind.
    let running: String
    let log: AppLog
    private let defaults: UserDefaults
    private let bundlePath: String
    private let spawn: ([String], [String: String]) throws -> pid_t
    private let terminate: @MainActor () -> Void
    private let badge: @MainActor (String?) -> Void
    /// Set by the windows: a question, or a dashboard build or start, is running.
    var workInFlight: () -> Bool = { false }
    /// A helper has been started; a quit that follows must not start a second one.
    private(set) var helperStarted = false
    private var schedule: Task<Void, Never>?, idleTimer: Timer?, inputMonitor: Any?
    private var lastSeen = Date(), lastInput = Date()

    init(defaults: UserDefaults = .standard, bundle: Bundle = .main, log: AppLog = .standard(),
         running: String? = nil, bundlePath: String? = nil,
         spawn: @escaping ([String], [String: String]) throws -> pid_t = RestartHelper.spawnDetached,
         terminate: @escaping @MainActor () -> Void = { NSApp.terminate(nil) },
         badge: @escaping @MainActor (String?) -> Void = { NSApp.dockTile.badgeLabel = $0 }) {
        self.defaults = defaults; self.log = log; self.spawn = spawn; self.terminate = terminate; self.badge = badge
        self.running = running ?? bundle.infoDictionary?["CFBundleShortVersionString"] as? String ?? ""
        self.bundlePath = bundlePath ?? bundle.bundlePath
    }

    private var configured: (executable: String, workspace: String) { Model.configured(defaults) }

    /// Reads the workspace's maintenance record now.
    @discardableResult func check() -> StagedUpdate? {
        staged = UpdateState.read(workspace: configured.workspace, running: running)
        return staged
    }

    /// The first check now, then every `checkEvery` while the app runs; the idle watch every minute.
    func start() {
        check()
        schedule?.cancel()
        schedule = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(Self.checkEvery))
                guard let self, !Task.isCancelled else { return }
                self.check()
            }
        }
        inputMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .leftMouseDown, .rightMouseDown, .otherMouseDown, .scrollWheel]) { [weak self] event in
            self?.lastInput = Date(); return event
        }
        idleTimer = Timer.scheduledTimer(withTimeInterval: IdleRestart.every, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.idleTick() }
        }
    }

    private func idleTick() {
        let onScreen = NSApp.windows.contains { $0.isVisible && $0.styleMask.contains(.titled) }
        if onScreen || NSApp.isActive { lastSeen = Date() }
        let seen = IdleRestart.Observation(staged: staged != nil, windowOnScreen: onScreen, active: NSApp.isActive,
                                           workInFlight: workInFlight(), lastSeen: lastSeen, lastInput: lastInput)
        guard IdleRestart.shouldRestart(seen, now: Date()) else { return }
        // The record may have moved on (another pass installed it): read it again first.
        guard check() != nil else { return }
        restart(trigger: "idle", mode: .background)
    }

    /// The person's *Restart to update*.
    func restartToUpdate() {
        guard check() != nil else {
            refusal = "no-update"
            log.record("update_restart", "refused", ["trigger": "person", "reason": "no-update"])
            return
        }
        restart(trigger: "person", mode: .restart)
    }

    /// The person quit with an update staged: swap it in after the app has gone, open nothing.
    func installOnQuit() {
        guard !helperStarted, let staged = check() else { return }
        _ = startHelper(.quit, version: staged.version)
    }

    private func restart(trigger: String, mode: RestartHelper.Mode) {
        guard !helperStarted, let staged else { return }
        log.record("update_restart", "requested", ["trigger": trigger, "version": staged.version])
        if let why = startHelper(mode, version: staged.version) {
            refusal = why
            log.record("update_restart", "refused", ["trigger": trigger, "reason": why, "version": staged.version])
            return
        }
        refusal = nil
        terminate()
    }

    /// Starts the helper; returns why it could not, as a code.
    func startHelper(_ mode: RestartHelper.Mode, version: String) -> String? {
        let plan = RestartHelper.Plan(pid: getpid(), engine: configured.executable, workspace: configured.workspace,
                                      bundle: bundlePath, mode: mode, log: log.file.path, version: version)
        if let why = plan.refusal { return why }
        try? FileManager.default.createDirectory(at: log.file.deletingLastPathComponent(), withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        var env = ProcessInfo.processInfo.environment
        env["OBSERVATORY_HOME"] = configured.workspace
        do { _ = try spawn(plan.argv, env) } catch { return "spawn-failed" }
        helperStarted = true
        return nil
    }
}

/// The app menu's *Restart to update*, shown only while an update is staged.
struct UpdateCommands: Commands {
    @ObservedObject var model: Model
    @ObservedObject var updates: Updates
    var body: some Commands {
        CommandGroup(after: .appInfo) {
            if let staged = updates.staged {
                Button(model.t("Restart to update")) { updates.restartToUpdate() }
                    .help(model.t("Version {version} is ready. Restart to update; it also installs when you quit.", ["version": staged.version]))
            }
        }
    }
}

/// The dashboard's toolbar button for the same act: unobtrusive, never a modal.
struct UpdateToolbarButton: View {
    @EnvironmentObject var m: Model
    @EnvironmentObject var updates: Updates
    var body: some View {
        if let staged = updates.staged {
            Button { updates.restartToUpdate() } label: {
                Label(m.t("Restart to update"), systemImage: "arrow.triangle.2.circlepath").labelStyle(.titleAndIcon)
            }
            .help(updates.refusal == nil
                  ? m.t("Version {version} is ready. Restart to update; it also installs when you quit.", ["version": staged.version])
                  : m.t("The update could not start. It installs when you quit Project Observatory."))
        }
    }
}
