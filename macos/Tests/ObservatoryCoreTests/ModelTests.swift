import XCTest
@testable import ObservatoryApp
@MainActor final class ModelTests: XCTestCase {
    func defaults() -> UserDefaults {
        let name = "observatory-tests-" + UUID().uuidString
        let defaults = UserDefaults(suiteName: name)!
        addTeardownBlock { defaults.removePersistentDomain(forName: name) }
        return defaults
    }
    func testLateWorkspaceResponseCannotReplaceNewState() async throws {
        var waiting: CheckedContinuation<[String: Any], Error>?
        var calls = 0
        let first = expectation(description: "old request started")
        let model = Model(defaults: defaults()) { action, _ in
            XCTAssertEqual(action, "status"); calls += 1
            if calls == 1 {
                return try await withCheckedThrowingContinuation { waiting = $0; first.fulfill() }
            }
            return ["engine_version": "new", "agent_enabled": true, "provider_configured": true, "conversations": []]
        }
        let old = Task { await model.refresh() }
        await fulfillment(of: [first], timeout: 2)
        model.workspace = "/new-workspace"
        await model.saveSettings()
        waiting?.resume(returning: ["engine_version": "old", "agent_enabled": false, "conversations": []])
        await old.value
        XCTAssertEqual(model.version, "new"); XCTAssertTrue(model.ready); XCTAssertNil(model.error)
    }
    func testRefusedAskPreservesDraftAndEndsSpinner() async {
        let model = Model(defaults: defaults()) { _, _ in throw NSError(domain: "fixture", code: 1) }
        model.ready = true; model.question = "Question remains"
        await model.send()
        XCTAssertEqual(model.question, "Question remains"); XCTAssertFalse(model.busy); XCTAssertNotNil(model.error)
    }
    func testWorkspaceChangeClearsOldDraftAndProjectScope() async {
        let model = Model(defaults: defaults()) { _, _ in ["engine_version": "new", "conversations": []] }
        model.scope = "project:old"; model.question = "Old private draft"; model.workspace = "/new-workspace"
        await model.saveSettings()
        XCTAssertEqual(model.scope, ""); XCTAssertEqual(model.question, ""); XCTAssertTrue(model.projects.isEmpty)
    }
    func testDashboardRejectsNonLoopbackDestination() async {
        let model = Model(defaults: defaults()) { _, _ in ["url": "https://example.invalid/dashboard/index.html"] }
        let url = await model.dashboardURL()
        XCTAssertNil(url); XCTAssertNotNil(model.error)
    }
    func testNewConversationCannotDetachRunningRequest() {
        let model = Model(defaults: defaults())
        model.selected = "chat-existing"; model.busy = true
        model.newConversation()
        XCTAssertEqual(model.selected, "chat-existing")
    }
}
