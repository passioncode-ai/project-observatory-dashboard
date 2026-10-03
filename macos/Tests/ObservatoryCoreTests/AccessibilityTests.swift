import XCTest
import SwiftUI
import AppKit
@testable import ObservatoryApp
import ObservatoryCore
import ApplicationServices

/// What VoiceOver reads: every window's view is hosted and its accessibility tree
/// walked in-process, the tree VoiceOver itself is given. No display is needed, so
/// this runs where a person's screen is locked or the window is on another Space.
@MainActor final class AccessibilityTests: XCTestCase {
    struct Element { let role: String; let label: String; let path: String }
    private var windows: [NSWindow] = []
    override func tearDown() async throws { for w in windows { w.contentView = nil; w.close() }; windows = [] }

    /// SwiftUI builds its accessibility tree only once an assistive client has
    /// spoken to the process. One attribute request to our own pid is that client;
    /// where the process may not use the Accessibility API (a CI runner without the
    /// permission) the tree stays empty and the test says so instead of passing.
    override func setUp() async throws {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)
        NSApp.finishLaunching()
        let probe = await host(Button("probe") {}, size: NSSize(width: 200, height: 100))
        let reachable = !elements(probe).filter { Self.actionable.contains($0.role) }.isEmpty
        probe.contentView = nil; probe.close(); windows.removeAll()
        if !reachable {
            throw XCTSkip("this process cannot reach SwiftUI's accessibility tree (Accessibility permission: \(AXIsProcessTrusted())); run swift test where it is granted")
        }
    }

    private func host<V: View>(_ view: V, size: NSSize = NSSize(width: 1100, height: 760)) async -> NSWindow {
        let w = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        w.isReleasedWhenClosed = false
        w.contentView = NSHostingView(rootView: view)
        windows.append(w)
        w.setFrameOrigin(NSPoint(x: -20000, y: -20000))    // ordered in, never on a screen
        w.orderFrontRegardless()
        w.contentView?.layoutSubtreeIfNeeded()
        try? await Task.sleep(for: .milliseconds(400))      // the .task blocks run once
        _ = AXUIElementSetAttributeValue(AXUIElementCreateApplication(getpid()), "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
        try? await Task.sleep(for: .milliseconds(100))
        w.contentView?.layoutSubtreeIfNeeded()
        return w
    }

    /// Every element in the tree with its role and the words VoiceOver speaks for it.
    private func elements(_ window: NSWindow) -> [Element] {
        var out: [Element] = []
        func walk(_ node: Any, _ path: String, _ depth: Int) {
            // SwiftUI's nodes answer the NSAccessibility methods without declaring the
            // protocol, so they are asked through dynamic dispatch.
            guard depth < 60, let o = node as? NSObject else { return }
            func ask(_ name: String) -> Any? {
                let sel = NSSelectorFromString(name)
                return o.responds(to: sel) ? o.perform(sel)?.takeUnretainedValue() : nil
            }
            let role = (ask("accessibilityRole") as? String) ?? ""
            let words = [ask("accessibilityLabel") as? String, ask("accessibilityTitle") as? String]
            let label = words.compactMap { $0 }.first { !$0.trimmingCharacters(in: .whitespaces).isEmpty } ?? ""
            let here = path + "/" + role
            if !role.isEmpty { out.append(Element(role: role, label: label, path: here)) }
            for child in (ask("accessibilityChildren") as? [Any]) ?? [] { walk(child, here, depth + 1) }
        }
        if let root = window.contentView { walk(root, "", 0) }
        return out
    }
    private static let actionable: Set<String> = [NSAccessibility.Role.button.rawValue, NSAccessibility.Role.textField.rawValue,
        NSAccessibility.Role.textArea.rawValue, NSAccessibility.Role.checkBox.rawValue, NSAccessibility.Role.radioButton.rawValue,
        NSAccessibility.Role.popUpButton.rawValue, NSAccessibility.Role.menuButton.rawValue, NSAccessibility.Role.link.rawValue]

    /// Each control has words, and no two controls in one window say the same thing
    /// unless they do the same thing (`allowedTwins`).
    private func assertNamed(_ found: [Element], allowedTwins: Set<String> = [], file: StaticString = #filePath, line: UInt = #line) {
        let controls = found.filter { Self.actionable.contains($0.role) }
        if ProcessInfo.processInfo.environment["OBSERVATORY_AX_DUMP"] != nil {
            print("AX:", controls.map { "\($0.role.dropFirst(2))“\($0.label)”" }.joined(separator: " · "))
        }
        XCTAssertFalse(controls.isEmpty, "no control was found: the tree was not read", file: file, line: line)
        for c in controls where c.label.isEmpty { XCTFail("unnamed \(c.role) at \(c.path)", file: file, line: line) }
        let counts = Dictionary(grouping: controls.filter { !$0.label.isEmpty }, by: \.label).filter { $0.value.count > 1 }
        for (label, twins) in counts where !allowedTwins.contains(label) {
            XCTFail("\(twins.count) controls read “\(label)”: \(twins.map(\.role))", file: file, line: line)
        }
    }

    private func model(russian: Bool, answers: @escaping (String) -> [String: Any]) -> Model {
        let name = "observatory-ax-tests-" + UUID().uuidString
        let d = UserDefaults(suiteName: name)!
        addTeardownBlock { d.removePersistentDomain(forName: name) }
        d.set("/srv/example-ws", forKey: "workspace"); d.set("/usr/bin/false", forKey: "executable"); d.set(russian, forKey: "russian")
        return Model(defaults: d) { action, _ in answers(action) }
    }
    private func status(ready: Bool) -> [String: Any] {
        ["engine_version": "0.12.0", "agent_enabled": ready, "provider_configured": ready, "model_configured": ready,
         "conversations": [["id": "c1", "title": "Which projects changed?"], ["id": "c2", "title": "What is unpushed?"]],
         "projects": [["id": "alpha-web", "name": "alpha-web"]]]
    }

    func testSettingsReadsEveryControlByItsOwnName() async {
        for russian in [false, true] {
            let m = model(russian: russian) { _ in self.status(ready: true) }
            let w = await host(SettingsView().environmentObject(m), size: NSSize(width: 600, height: 420))
            let found = elements(w)
            assertNamed(found)
            let labels = Set(found.map(\.label))
            XCTAssertTrue(labels.contains(m.t("Choose", "Выбрать") + ": " + m.t("Workspace", "Папка данных")), "\(labels)")
            XCTAssertTrue(labels.contains(m.t("Choose", "Выбрать") + ": " + m.t("Program", "Программа")), "\(labels)")
        }
    }

    func testTheAssistantReadsEveryControlInBothLanguages() async {
        for (russian, ready) in [(false, true), (true, true), (false, false)] {
            let m = model(russian: russian) { action in
                action == "get" ? ["turns": []] : self.status(ready: ready)
            }
            await m.refresh()
            let w = await host(AssistantView().environmentObject(m))
            // The two Settings links (the footer's gear and the setup banner's button) open one window.
            assertNamed(elements(w), allowedTwins: [m.t("Settings", "Настройки")])
        }
    }

    func testTheDashboardWindowReadsEveryControlInEachMode() async throws {
        let base = Model.resolved(FileManager.default.temporaryDirectory.appendingPathComponent("obs-ax-" + UUID().uuidString).path)
        try FileManager.default.createDirectory(atPath: base + "/docs/dashboard", withIntermediateDirectories: true)
        try "<!doctype html><title>t</title>".write(toFile: base + "/docs/dashboard/index.html", atomically: true, encoding: .utf8)
        addTeardownBlock { try? FileManager.default.removeItem(atPath: base) }
        let answers: [[String: Any]] = [
            ["server": "absent", "files": base + "/docs/dashboard/index.html", "built_at": "2026-10-02T14:03:19Z"],
            ["server": "other-workspace", "files": base + "/docs/dashboard/index.html"],
            ["server": "absent"],
        ]
        for (i, answer) in answers.enumerated() {
            let name = "observatory-ax-dash-" + UUID().uuidString
            let d = UserDefaults(suiteName: name)!
            addTeardownBlock { d.removePersistentDomain(forName: name) }
            d.set(base, forKey: "workspace"); d.set("/usr/bin/false", forKey: "executable")
            let m = Model(defaults: d) { action, _ in action == "dashboard" ? answer : self.status(ready: true) }
            let w = await host(DashboardView().environmentObject(m).environmentObject(WebController()))
            let found = elements(w)
            assertNamed(found)
            if i < 2 { XCTAssertTrue(found.contains { $0.label == m.t("Start server", "Запустить сервер") }, "mode \(i): \(found.map(\.label))") }
            else { XCTAssertTrue(found.contains { $0.label == m.t("Build the dashboard", "Построить дашборд") }, "\(found.map(\.label))") }
        }
    }
}
