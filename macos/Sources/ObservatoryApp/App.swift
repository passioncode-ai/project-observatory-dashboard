import SwiftUI
import AppKit

@MainActor final class ApplicationDelegate: NSObject, NSApplicationDelegate {
    /// SwiftUI's own "open this window", remembered from the app's commands — which
    /// exist with no window open — because AppKit's reopen and a launch restored with
    /// no window do NOT recreate a WindowGroup window by themselves (measured: zero
    /// windows after closing the dashboard and clicking the Dock icon).
    static var openWindow: OpenWindowAction?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Belt and braces beside the bridge's per-descriptor F_SETNOSIGPIPE: a pipe
        // to a child that already exited must fail a write, never end the app.
        signal(SIGPIPE, SIG_IGN)
        NSApp.setActivationPolicy(.regular)
        // One dark PassionCode product: menus, alerts, sheets and the Settings window
        // follow the dashboard's dark pages whatever the system appearance is.
        NSApp.appearance = NSAppearance(named: .darkAqua)
        // A launch from Finder, the Dock or `open` brings the window forward: the
        // macOS 14 cooperative call, which the system honours for a user-started app.
        NSApp.activate()
        // A launch must always end with the dashboard on screen.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { Self.showDashboardIfNone() }
    }
    /// Clicking the Dock icon with no window open brings the dashboard back.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { Self.showDashboardIfNone() }
        return true
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    static func showDashboardIfNone() {
        let visible = NSApp.windows.contains { $0.isVisible && $0.canBecomeMain }
        if !visible { openWindow?(id: WindowID.dashboard) }
        NSApp.activate()
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
        Window(model.t("Dashboard", "Дашборд"), id: WindowID.dashboard) {
            // The pages are dark by design (PassionCode tokens, `color-scheme: dark`):
            // the window's chrome matches them instead of framing them in light grey.
            DashboardView().environmentObject(model).environmentObject(web)
        }
        .defaultSize(width: 1320, height: 860)
        .keyboardShortcut("1")
        .commands { AppCommands(model: model, web: web) }

        // The assistant is one window away, never in front of the dashboard.
        // SwiftUI lists this window in the Window menu itself; the shortcut lives on
        // the scene, so the menu does not carry a second "Assistant" item.
        Window(model.t("Assistant", "Ассистент"), id: WindowID.assistant) {
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
            Button(model.t("New Conversation", "Новый диалог")) { openWindow(id: WindowID.assistant); model.newConversation() }
                .keyboardShortcut("n").disabled(model.busy)
            Divider()
            Button(model.t("Delete Conversation…", "Удалить диалог…")) { openWindow(id: WindowID.assistant); model.deleting = model.selected }
                .keyboardShortcut(.delete, modifiers: .command).disabled(model.selected == nil || model.busy)
            Divider()
            Button(model.t("Previous Conversation", "Предыдущий диалог")) { model.selectAdjacent(-1) }
                .keyboardShortcut(.upArrow, modifiers: [.command, .option]).disabled(model.conversations.isEmpty || model.busy)
            Button(model.t("Next Conversation", "Следующий диалог")) { model.selectAdjacent(1) }
                .keyboardShortcut(.downArrow, modifiers: [.command, .option]).disabled(model.conversations.isEmpty || model.busy)
        }
        CommandMenu(model.t("Dashboard", "Дашборд")) {
            Button(model.t("Overview", "Обзор")) { openWindow(id: WindowID.dashboard); web.home() }
                .keyboardShortcut("h", modifiers: [.command, .shift])
            Button(model.t("Back", "Назад")) { web.view.goBack() }.keyboardShortcut("[").disabled(!web.canGoBack)
            Button(model.t("Forward", "Вперёд")) { web.view.goForward() }.keyboardShortcut("]").disabled(!web.canGoForward)
            Button(model.t("Reload", "Обновить")) { Task { await model.refreshDashboard(); web.reload() } }.keyboardShortcut("r")
            Divider()
            Button(model.t("Start Server", "Запустить сервер")) { Task { await model.startServer() } }
                .disabled(model.dashboardWorking)
            Button(model.t("Rebuild Pages", "Перестроить страницы")) { Task { await model.buildDashboard(); web.reload() } }
                .disabled(model.dashboardWorking)
            Button(model.t("Open in Browser", "Открыть в браузере")) { if let u = web.currentURL { NSWorkspace.shared.open(u) } }
                .disabled(web.currentURL == nil)
        }
    }
}
