import XCTest
@testable import ObservatoryApp
@testable import ObservatoryCore

/// The operator's lifecycle defaults for every app (2026-10-16): open at login by default,
/// permissions checked at every launch, a closed window keeps the app working, a ready
/// update asks once to restart; and the update record read every 5 minutes (2026-10-17).
final class AppLifecycleTests: XCTestCase {
    // MARK: open at login

    func testTheFirstLaunchTurnsOpenAtLoginOnAndStoresTheChoice() {
        XCTAssertEqual(LoginItemPolicy.decide(stored: nil, status: .notRegistered), .init(action: .register, stored: true))
        XCTAssertEqual(LoginItemPolicy.decide(stored: nil, status: .enabled), .init(action: .keep, stored: true))
    }

    func testAPersonsOffIsNeverOverriddenAndALeftoverRegistrationIsRemoved() {
        for status in [LoginItemStatus.notRegistered, .notFound] {
            XCTAssertEqual(LoginItemPolicy.decide(stored: false, status: status), .init(action: .keep, stored: false))
        }
        XCTAssertEqual(LoginItemPolicy.decide(stored: false, status: .enabled), .init(action: .unregister, stored: false))
        XCTAssertEqual(LoginItemPolicy.decide(stored: false, status: .requiresApproval), .init(action: .unregister, stored: false))
    }

    func testAnUpdateKeepsTheChoiceAndALostRegistrationIsRestored() {
        // An update replaces the bundle; the stored choice survives it, and a launch that
        // finds the registration gone registers again.
        XCTAssertEqual(LoginItemPolicy.decide(stored: true, status: .enabled), .init(action: .keep, stored: true))
        XCTAssertEqual(LoginItemPolicy.decide(stored: true, status: .notRegistered), .init(action: .register, stored: true))
        XCTAssertEqual(LoginItemPolicy.decide(stored: true, status: .requiresApproval), .init(action: .keep, stored: true),
                       "registering again cannot grant an approval only the person gives")
    }

    @MainActor func testTheLoginItemGlueAppliesTheDefaultOnceAndLogsIt() throws {
        let d = scratchDefaults()
        let log = AppLog(file: FileManager.default.temporaryDirectory.appendingPathComponent("obs-login-\(UUID().uuidString)/app.log"))
        addTeardownBlock { try? FileManager.default.removeItem(at: log.file.deletingLastPathComponent()) }
        var status = LoginItemStatus.notRegistered, registered = 0
        let item = LoginItem(defaults: d, log: log, status: { status },
                             register: { registered += 1; status = .enabled }, unregister: { status = .notRegistered })
        XCTAssertEqual(item.apply(), [])
        XCTAssertEqual(registered, 1); XCTAssertEqual(d.object(forKey: LoginItemPolicy.key) as? Bool, true)
        XCTAssertTrue(item.enabled)
        XCTAssertEqual(item.apply(), []); XCTAssertEqual(registered, 1, "a second launch registers nothing again")
        item.enabled = false
        XCTAssertEqual(status, .notRegistered); XCTAssertEqual(d.object(forKey: LoginItemPolicy.key) as? Bool, false)
        XCTAssertEqual(item.apply(), []); XCTAssertEqual(registered, 1, "off stays off at the next launch")
        let lines = try String(contentsOf: log.file, encoding: .utf8)
        XCTAssertTrue(lines.contains(#""code":"registered""#)); XCTAssertTrue(lines.contains(#""code":"unregistered""#))
    }

    @MainActor func testARegistrationThatFailsIsShownAndLoggedNeverSilent() throws {
        let d = scratchDefaults()
        let log = AppLog(file: FileManager.default.temporaryDirectory.appendingPathComponent("obs-login-\(UUID().uuidString)/app.log"))
        addTeardownBlock { try? FileManager.default.removeItem(at: log.file.deletingLastPathComponent()) }
        let item = LoginItem(defaults: d, log: log, status: { .notRegistered },
                             register: { throw BridgeError.failed("denied") }, unregister: {})
        XCTAssertEqual(item.apply().map(\.code), [AppPermissions.loginItemCode])
        let lines = try String(contentsOf: log.file, encoding: .utf8)
        XCTAssertTrue(lines.contains(#""code":"failed""#))
        XCTAssertTrue(lines.contains(#""permission":"login-item-not-allowed""#))
    }

    // MARK: permissions at every launch

    func testAMissingLoginItemApprovalIsNamedWithHowToGrantIt() {
        let missing = AppPermissions.missing(wanted: true, status: .requiresApproval, registerFailed: false)
        XCTAssertEqual(missing.map(\.code), [AppPermissions.loginItemCode])
        XCTAssertTrue(missing[0].detail.contains("System Settings"), "how to grant it")
        XCTAssertTrue(missing[0].detail.contains("Open at login"), "and how to stop needing it")
        XCTAssertEqual(AppPermissions.missing(wanted: true, status: .notRegistered, registerFailed: true).count, 1)
    }

    func testNothingIsMissingWhenGrantedOrNotWanted() {
        XCTAssertEqual(AppPermissions.missing(wanted: true, status: .enabled, registerFailed: false), [])
        XCTAssertEqual(AppPermissions.missing(wanted: false, status: .requiresApproval, registerFailed: false), [])
        XCTAssertEqual(AppPermissions.missing(wanted: false, status: .notRegistered, registerFailed: true), [])
    }

    // MARK: closing the window

    func testClosingTheLastWindowNeverQuitsTheApp() {
        XCTAssertFalse(WindowClosePolicy.terminateAfterLastWindowClosed)
        XCTAssertEqual(ApplicationDelegate.terminatesAfterLastWindowClosed, WindowClosePolicy.terminateAfterLastWindowClosed)
    }

    // MARK: the update prompt

    func testAReadyUpdateAsksOncePerVersionAndOnlyWhenSeen() {
        XCTAssertTrue(UpdatePrompt.shouldPrompt(staged: "0.28.0", prompted: nil, windowOnScreen: true))
        XCTAssertFalse(UpdatePrompt.shouldPrompt(staged: "0.28.0", prompted: nil, windowOnScreen: false), "nobody would see it")
        XCTAssertFalse(UpdatePrompt.shouldPrompt(staged: "0.28.0", prompted: "0.28.0", windowOnScreen: true), "Later is remembered")
        XCTAssertTrue(UpdatePrompt.shouldPrompt(staged: "0.29.0", prompted: "0.28.0", windowOnScreen: true), "a newer one asks again")
        XCTAssertFalse(UpdatePrompt.shouldPrompt(staged: nil, prompted: nil, windowOnScreen: true))
        XCTAssertFalse(UpdatePrompt.shouldPrompt(staged: "next", prompted: nil, windowOnScreen: true))
    }

    @MainActor func testTheUpdatesWatchAsksThroughItsPresenterAndRemembersTheVersion() throws {
        let ws = URL(fileURLWithPath: Model.resolved(FileManager.default.temporaryDirectory.appendingPathComponent("obs-prompt-\(UUID().uuidString)").path))
        try FileManager.default.createDirectory(at: ws.appendingPathComponent("store"), withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: ws) }
        try JSONSerialization.data(withJSONObject: ["app": ["result": "waiting-for-quit", "pending": "0.19.0"]])
            .write(to: UpdateState.file(workspace: ws.path))
        let d = scratchDefaults()
        d.set(ws.path, forKey: "workspace"); d.set("/opt/po/bin/project-observatory", forKey: "executable")
        var asked: [String] = [], spawned = 0, quits = 0
        let u = Updates(defaults: d, log: AppLog(file: ws.appendingPathComponent("logs/app.log")), running: "0.18.0",
                        bundlePath: "/srv/apps/Project Observatory.app", spawn: { _, _ in spawned += 1; return 4242 },
                        terminate: { quits += 1 }, badge: { _ in })
        u.windowOnScreen = { false }
        u.prompt = { version, restart in asked.append(version); restart(false) }
        u.check(); u.promptIfDue()
        XCTAssertEqual(asked, [], "no window on screen: not asked yet")
        u.windowOnScreen = { true }
        u.promptIfDue()
        XCTAssertEqual(asked, ["0.19.0"]); XCTAssertEqual(spawned, 0, "Later restarts nothing")
        XCTAssertEqual(d.string(forKey: UpdatePrompt.key), "0.19.0")
        u.check(); u.promptIfDue()
        XCTAssertEqual(asked, ["0.19.0"], "the same version never asks twice")
        XCTAssertNotNil(u.staged, "the menu item and toolbar button stay")
        d.removeObject(forKey: UpdatePrompt.key)
        u.prompt = { version, restart in asked.append(version); restart(true) }
        u.promptIfDue()
        XCTAssertEqual(spawned, 1); XCTAssertEqual(quits, 1, "Restart now restarts to update")
    }

    // MARK: the 5-minute cadence

    func testTheUpdateRecordIsReadEveryFiveMinutes() {
        XCTAssertEqual(Updates.checkEvery, 5 * 60)
    }
}
