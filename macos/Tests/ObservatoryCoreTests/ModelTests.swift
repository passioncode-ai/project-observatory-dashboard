import XCTest
@testable import ObservatoryApp
import ObservatoryCore
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

    func readyStatus(_ extra: [String: Any] = [:]) -> [String: Any] {
        ["protocol": "observatory-assistant/1", "engine_version": "test", "agent_enabled": true, "provider_configured": true,
         "conversations": [["id": "chat-a", "title": "A"]], "projects": [["id": "project:a", "name": "A"]]].merging(extra) { $1 }
    }
    func testBusyFollowsTheStoredRowsNotAStalePoll() async {
        var status = "working"
        let model = Model(defaults: defaults()) { action, _ in
            switch action {
            case "status": return self.readyStatus()
            case "get": return ["turns": [["request_id": "r1", "question": "Q", "status": status, "job_id": "job-1",
                                           "answer": status == "completed" ? ["answer": "A"] : [:]]]]
            default: throw BridgeError.timeout
            }
        }
        await model.refresh()
        await model.load("chat-a")
        XCTAssertTrue(model.busy); XCTAssertEqual(model.job, "job-1")
        status = "completed"                                // finished while the poll was failing
        await model.load("chat-a")
        XCTAssertFalse(model.busy); XCTAssertNil(model.job)
        XCTAssertEqual(model.turns.first?.answer, "A")
    }
    func testRetryAfterTimeoutReusesTheRequestIdUntilTheDraftChanges() async {
        var ids: [String] = []
        let model = Model(defaults: defaults()) { action, input in
            if action == "status" { return self.readyStatus() }
            if action == "ask" { ids.append(input["request_id"] ?? ""); throw BridgeError.timeout }
            return [:]
        }
        await model.refresh(); model.question = "Same draft"
        await model.send(); await model.send()
        XCTAssertEqual(ids.count, 2); XCTAssertEqual(ids[0], ids[1])
        model.question = "Edited draft"
        await model.send()
        XCTAssertNotEqual(ids[2], ids[0])
    }
    func testAScopeThatLeftTheRegistryIsCleared() async {
        let model = Model(defaults: defaults()) { _, _ in self.readyStatus(["conversations": []]) }
        model.scope = "project:gone"
        await model.refresh()
        XCTAssertEqual(model.scope, "")
    }
    func testLanguageChangeKeepsTheSessionAndRewordsTheError() async {
        let model = Model(defaults: defaults()) { action, _ in
            if action == "status" { return self.readyStatus(["provider_configured": false, "provider_status": "absent", "conversations": []]) }
            return [:]
        }
        model.russian = false
        await model.refresh()
        model.question = "Draft stays"; model.scope = "project:a"
        let english = model.error
        model.russian = true
        await model.saveSettings()
        XCTAssertEqual(model.question, "Draft stays"); XCTAssertEqual(model.scope, "project:a")
        XCTAssertNotEqual(model.error, english); XCTAssertTrue(model.error?.contains("Провайдер") == true)
    }
    func testOldEngineAndEveryLimitHaveTheirOwnWords() {
        let model = Model(defaults: defaults()); model.russian = false
        let generic = model.message("something-new")
        for code in ["backend-incompatible", "unknown-workspace", "conversation-full", "history-full", "request-history-full",
                     "credential-shaped-input", "unknown-project", "invalid-question", "disk-full", "unknown-job",
                     "invalid-evidence", "conversation-busy", "backend-failed", "provider-unconfigured"] {
            XCTAssertFalse(model.message(code).hasPrefix(String(generic.prefix(20))), code)
        }
        XCTAssertTrue(model.message("backend-incompatible").contains("full update"))
    }
    func testQuestionLimitCountsCodePointsLikeTheEngine() {
        let model = Model(defaults: defaults())
        model.question = String(repeating: "👩‍👩‍👧", count: 1000)     // 1000 graphemes, 5000 code points
        XCTAssertFalse(model.questionTooLong)
        model.question = String(repeating: "👩‍👩‍👧", count: 1300)     // 6500 code points
        XCTAssertTrue(model.questionTooLong)
    }
    func testMarkdownLinksAreRemoved() {
        let out = rendered("**Bold** and [a link](https://example.invalid)\n- item")
        XCTAssertTrue(out.runs.allSatisfy { $0.link == nil })
        XCTAssertTrue(String(out.characters).contains("• item"))
        XCTAssertEqual(money(0.000279944), "$0.0003")
    }
}

