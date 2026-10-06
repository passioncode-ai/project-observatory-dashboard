import SwiftUI
import AppKit
import ObservatoryCore

@MainActor final class ApplicationDelegate: NSObject, NSApplicationDelegate {
    /// SwiftUI's own "open this window", remembered from the app's commands — which
    /// exist with no window open — because AppKit's reopen and a launch restored with
    /// no window do NOT recreate a SwiftUI scene window by themselves (measured with
    /// the WindowGroup this was: zero windows after closing the dashboard and clicking
    /// the Dock icon; the single `Window` scene relies on the same call).
    static var openWindow: OpenWindowAction?
    /// The staged-update watch; one per app, started at launch.
    let updates = Updates()
    /// `--background` (the organization's background launch, and the idle restart's
    /// reopen): start and do the startup work, but show no window and take no focus
    /// until the person opens the app.
    static let launchedInBackground = CommandLine.arguments.contains("--background")

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Belt and braces beside the bridge's per-descriptor F_SETNOSIGPIPE: a pipe
        // to a child that already exited must fail a write, never end the app.
        signal(SIGPIPE, SIG_IGN)
        NSApp.setActivationPolicy(.regular)
        // One dark PassionCode product: menus, alerts, sheets and the Settings window
        // follow the dashboard's dark pages whatever the system appearance is.
        NSApp.appearance = NSAppearance(named: .darkAqua)
        updates.start()
        if Self.launchedInBackground {
            // SwiftUI opens the first scene's window by itself; put it away unseen. A Dock
            // click (applicationShouldHandleReopen) brings it back as usual.
            for delay in [0.0, 0.5] {
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                    NSApp.windows.filter { DashboardReopen.isDashboard($0.identifier?.rawValue) }.forEach { $0.orderOut(nil) }
                }
            }
            return
        }
        // A launch from Finder, the Dock or `open` brings the window forward: the
        // macOS 14 cooperative call, which the system honours for a user-started app.
        NSApp.activate()
        // A launch must always end with the dashboard on screen — also when the
        // system restored only the Assistant window.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { Self.showDashboard() }
    }
    /// Clicking the Dock icon brings the dashboard back whenever it is not on screen,
    /// whatever else is open. AppKit's `hasVisibleWindows` counts the Assistant too, so
    /// with only the Assistant open a Dock click used to bring nothing back (A47); the
    /// decision is the dashboard's own window now, not "any window". Returns false:
    /// the dashboard is handled here, and AppKit's default (un-minimising some other
    /// window when none is visible) must not add a window beside it.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        Self.showDashboard()
        return false
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    /// A quit is a safe point (LC-16): a staged update is swapped in once the app is gone.
    func applicationWillTerminate(_ notification: Notification) { updates.installOnQuit() }

    static func showDashboard() {
        switch DashboardReopen.decide(NSApp.windows.map(DashboardReopen.Seen.init)) {
        case .nothing: break
        case .restore:
            if let w = NSApp.windows.first(where: { DashboardReopen.isDashboard($0.identifier?.rawValue) && $0.isMiniaturized }) {
                w.deminiaturize(nil)
            }
        case .open: openWindow?(id: WindowID.dashboard)
        }
        NSApp.activate()
    }
}

/// What a Dock click or a launch must do to put the dashboard on screen, decided from
/// the app's windows alone so it can be tested without AppKit's event loop.
enum DashboardReopen: Equatable {
    /// The dashboard is already on screen: other windows (the Assistant) stay as they are.
    case nothing
    /// The dashboard is minimised into the Dock: un-minimise it.
    case restore
    /// No dashboard window: SwiftUI's openWindow creates it — or, for the single
    /// `Window` scene, brings the existing one forward, so a window this test cannot
    /// recognise costs a bring-forward, never a second dashboard.
    case open

    struct Seen: Equatable {
        var identifier: String?
        var visible: Bool
        var miniaturized: Bool
    }

    /// SwiftUI names a `Window(id:)` scene's NSWindow by its id; a WindowGroup's by
    /// `<id>-AppWindow-<n>`. Both spellings are the dashboard.
    static func isDashboard(_ identifier: String?) -> Bool {
        guard let id = identifier else { return false }
        return id == WindowID.dashboard || id.hasPrefix(WindowID.dashboard + "-")
    }

    static func decide(_ windows: [Seen]) -> DashboardReopen {
        let dashboards = windows.filter { isDashboard($0.identifier) }
        if dashboards.contains(where: { $0.visible }) { return .nothing }
        if dashboards.contains(where: { $0.miniaturized }) { return .restore }
        return .open
    }
}

extension DashboardReopen.Seen {
    init(_ window: NSWindow) {
        self.init(identifier: window.identifier?.rawValue, visible: window.isVisible, miniaturized: window.isMiniaturized)
    }
}

@main struct ObservatoryApp: App {
    @NSApplicationDelegateAdaptor(ApplicationDelegate.self) var appDelegate
    @StateObject private var model = Model()
    @StateObject private var web = WebController()

    var body: some Scene {
        // The DASHBOARD is the app: it opens first, at launch and on reopen. ONE
        // window — a `Window`, not a WindowGroup, whose openWindow(id:) added a
        // window per ⌘1 / Overview / Dashboard click, all but one blank (they
        // share one web view). SwiftUI lists it in the Window menu as Dashboard, ⌘1.
        Window(model.t("Dashboard"), id: WindowID.dashboard) {
            // The pages are dark by design (PassionCode tokens, `color-scheme: dark`):
            // the window's chrome matches them instead of framing them in light grey.
            DashboardView().environmentObject(model).environmentObject(web).environmentObject(appDelegate.updates)
        }
        .defaultSize(width: 1320, height: 860)
        .keyboardShortcut("1")
        .commands {
            AppCommands(model: model, web: web)
            UpdateCommands(model: model, updates: appDelegate.updates)
        }

        // The assistant is one window away, never in front of the dashboard.
        // SwiftUI lists this window in the Window menu itself; the shortcut lives on
        // the scene, so the menu does not carry a second "Assistant" item.
        Window(model.t("Assistant"), id: WindowID.assistant) {
            AssistantView().environmentObject(model).frame(minWidth: 900, minHeight: 620)
        }
        .defaultSize(width: 1080, height: 760)
        .keyboardShortcut("a", modifiers: [.command, .shift])

        Settings { SettingsView().environmentObject(model) }
    }
}

struct AppCommands: Commands {
    private func remember(_ action: OpenWindowAction) { ApplicationDelegate.openWindow = action }
    @ObservedObject var model: Model
    @ObservedObject var web: WebController
    @Environment(\.openWindow) private var openWindow

    var body: some Commands {
        let _ = remember(openWindow)
        // One dashboard window: no "New Window" over a single web view.
        CommandGroup(replacing: .newItem) {
            Button(model.t("New Conversation")) { openWindow(id: WindowID.assistant); model.newConversation() }
                .keyboardShortcut("n").disabled(model.busy)
            Divider()
            Button(model.t("Delete Conversation…")) { openWindow(id: WindowID.assistant); model.deleting = model.selected }
                .keyboardShortcut(.delete, modifiers: .command).disabled(model.selected == nil || model.busy)
            Divider()
            Button(model.t("Previous Conversation")) { model.selectAdjacent(-1) }
                .keyboardShortcut(.upArrow, modifiers: [.command, .option]).disabled(model.conversations.isEmpty || model.busy)
            Button(model.t("Next Conversation")) { model.selectAdjacent(1) }
                .keyboardShortcut(.downArrow, modifiers: [.command, .option]).disabled(model.conversations.isEmpty || model.busy)
        }
        CommandMenu(model.t("Dashboard")) {
            Button(model.t("Overview")) { openWindow(id: WindowID.dashboard); web.home() }
                .keyboardShortcut("h", modifiers: [.command, .shift])
            Button(model.t("Back")) { web.view.goBack() }.keyboardShortcut("[").disabled(!web.canGoBack)
            Button(model.t("Forward")) { web.view.goForward() }.keyboardShortcut("]").disabled(!web.canGoForward)
            Button(model.t("Reload")) { Task { await model.refreshDashboard(); web.reload() } }.keyboardShortcut("r")
            Divider()
            Button(model.t("Start Server")) { Task { await model.startServer() } }
                .disabled(!model.canStartServer)
            Button(model.t("Rebuild Pages")) { Task { await model.buildDashboard(); web.reload() } }
                .disabled(model.dashboardWorking)
            Button(model.t("Open in Browser")) { if let u = web.currentURL { NSWorkspace.shared.open(u) } }
                .disabled(web.currentURL == nil)
        }
    }
}
