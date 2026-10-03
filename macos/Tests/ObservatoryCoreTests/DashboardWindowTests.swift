import XCTest
import SwiftUI
import AppKit
@testable import ObservatoryApp
import ObservatoryCore

/// The dashboard window against a real web view: what it shows after the workspace
/// changes. The controller (and its one web view) outlives the window, so a window
/// that opens AFTER the change must still load the new workspace's pages.
@MainActor final class DashboardWindowTests: XCTestCase {
    private var windows: [NSWindow] = []
    override func tearDown() async throws {
        for w in windows { w.contentView = nil; w.close() }
        windows = []
    }

    /// Two workspaces with built pages, under their real paths (the app saves real paths).
    private func workspaces() throws -> (String, String) {
        let base = Model.resolved(FileManager.default.temporaryDirectory.appendingPathComponent("obs-dash-" + UUID().uuidString).path)
        addTeardownBlock { try? FileManager.default.removeItem(atPath: base) }
        var made: [String] = []
        for name in ["ws-a", "ws-b"] {
            let pages = base + "/" + name + "/docs/dashboard"
            try FileManager.default.createDirectory(atPath: pages, withIntermediateDirectories: true)
            try "<!doctype html><title>\(name)</title><p>\(name)</p>".write(toFile: pages + "/index.html", atomically: true, encoding: .utf8)
            made.append(Model.resolved(base + "/" + name))
        }
        return (made[0], made[1])
    }

    private func model(_ workspace: String, current: @escaping () -> String) -> Model {
        let name = "observatory-dash-tests-" + UUID().uuidString
        let defaults = UserDefaults(suiteName: name)!
        addTeardownBlock { defaults.removePersistentDomain(forName: name) }
        defaults.set(workspace, forKey: "workspace"); defaults.set("/usr/bin/false", forKey: "executable")
        return Model(defaults: defaults) { action, _ in
            switch action {
            case "dashboard": return ["server": "absent", "files": current() + "/docs/dashboard/index.html"]
            default: return ["engine_version": "test", "agent_enabled": false, "conversations": []]
            }
        }
    }

    /// Opens a dashboard window the way the app does: a new `DashboardView` over the
    /// app's one model and one controller.
    private func open(_ model: Model, _ web: WebController) -> NSWindow {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 620), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        w.isReleasedWhenClosed = false
        w.contentView = NSHostingView(rootView: DashboardView().environmentObject(model).environmentObject(web))
        windows.append(w)
        return w
    }
    private func close(_ w: NSWindow) { w.contentView = nil; w.close() }

    /// Waits until the page on screen is `workspace`'s, or fails naming what is shown.
    private func waitForPage(of workspace: String, _ web: WebController, file: StaticString = #filePath, line: UInt = #line) async {
        let want = URL(fileURLWithPath: workspace + "/docs/dashboard/index.html").standardizedFileURL.path
        for _ in 0..<100 {
            if web.view.url?.standardizedFileURL.path == want, !web.view.isLoading { return }
            try? await Task.sleep(for: .milliseconds(50))
        }
        XCTFail("the window shows \(web.view.url?.path ?? "nothing"), not \(want)", file: file, line: line)
    }

    func testAWindowOpenDuringTheSwitchShowsTheNewWorkspace() async throws {
        let (a, b) = try workspaces()
        var current = a
        let m = model(a) { current }, web = WebController()
        _ = open(m, web)
        await waitForPage(of: a, web)
        m.workspace = b; current = b
        await m.saveSettings()
        await waitForPage(of: b, web)
    }

    /// R2-APP-5: the dashboard window was closed, the workspace changed in Settings,
    /// and the window reopened (⌘1, the Dock). The mode was already B's, so nothing
    /// changed for the new view to react to, and the kept web view went on showing A.
    func testAWindowReopenedAfterTheSwitchShowsTheNewWorkspace() async throws {
        let (a, b) = try workspaces()
        var current = a
        let m = model(a) { current }, web = WebController()
        let first = open(m, web)
        await waitForPage(of: a, web)
        close(first)
        m.workspace = b; current = b
        await m.saveSettings()
        _ = open(m, web)
        await waitForPage(of: b, web)
    }
}
