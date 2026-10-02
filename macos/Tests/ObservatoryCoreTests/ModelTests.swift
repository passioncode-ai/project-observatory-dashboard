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
            if action == "dashboard" { return ["server": "absent"] }   // Save re-checks the dashboard too
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
    func testDashboardPrefersTheVerifiedServerThenTheBuiltPages() {
        let ws = "/srv/example-ws"
        let live = Model.mode(["server": "verified", "url": "http://127.0.0.1:47311/dashboard/index.html",
                               "files": ws + "/docs/dashboard/index.html"], workspace: ws)
        XCTAssertEqual(live.origin?.isLive, true)
        let files = Model.mode(["server": "absent", "files": ws + "/docs/dashboard/index.html",
                                "built_at": "2026-10-02T14:03:19Z", "always_on": true], workspace: ws)
        guard case .files(let o, let at, let alwaysOn, let busy) = files else { return XCTFail("\(files)") }
        XCTAssertFalse(o.isLive); XCTAssertNotNil(at); XCTAssertTrue(alwaysOn); XCTAssertFalse(busy)
        XCTAssertEqual(Model.mode(["server": "absent"], workspace: ws), .notBuilt(portBusy: false))
        XCTAssertEqual(Model.mode(["server": "other-workspace"], workspace: ws), .notBuilt(portBusy: true))
    }
    func testDashboardRefusesAnAddressThatIsNotThisWorkspaces() {
        let ws = "/srv/example-ws"
        // A remote "verified" URL, or pages outside the workspace, are never shown.
        XCTAssertNil(Model.mode(["server": "verified", "url": "https://example.invalid/dashboard/index.html"], workspace: ws).origin)
        XCTAssertNil(Model.mode(["server": "absent", "files": "/srv/example-other/docs/dashboard/index.html"], workspace: ws).origin)
    }
    func testDashboardStartAndBuildReportTheirFailure() async {
        let d = defaults(); d.set("/srv/example-ws", forKey: "workspace")
        let model = Model(defaults: d) { action, _ in
            if action == "serve" { throw BridgeError.backend("dashboard-start-failed") }
            if action == "build" { throw BridgeError.backend("dashboard-build-failed") }
            return ["server": "absent", "files": "/srv/example-ws/docs/dashboard/index.html"]
        }
        await model.refreshDashboard()
        if case .files = model.dashboardMode {} else { XCTFail("\(model.dashboardMode)") }
        await model.startServer()
        XCTAssertEqual(model.dashboardFailure?.code, "dashboard-start-failed")
        XCTAssertFalse(model.dashboardWorking)
        XCTAssertTrue(model.dashboardError?.contains("serverd.out") == true)
        await model.buildDashboard()
        XCTAssertEqual(model.dashboardFailure?.code, "dashboard-build-failed")
        if case .files = model.dashboardMode {} else { XCTFail("a failed build must keep the pages shown") }
    }
    func testAnEngineWithoutTheDashboardActionsIsNamedNotShownAsUnbuilt() async {
        // 0.11.0 answers `dashboard` in its old shape (a url, or an error) and has no
        // serve/build: "no dashboard yet" would be a lie about a workspace that has one.
        let model = Model(defaults: defaults()) { action, _ in
            action == "dashboard" ? ["url": "http://127.0.0.1:47311/dashboard/index.html"] : [:]
        }
        model.russian = false
        await model.refreshDashboard()
        XCTAssertEqual(model.dashboardMode, .unavailable)
        XCTAssertEqual(model.dashboardFailure?.code, "backend-incompatible")
        XCTAssertTrue(model.dashboardError?.contains("0.12") == true)
    }
    func testAppLanguageChangeReachesThePageOnceAndAPageChoiceComesBack() {
        let model = Model(defaults: defaults()); model.russian = false
        let seed = model.localeSeed
        model.russian = true
        XCTAssertEqual(model.localeSeed, seed + 1)                 // the app's change is pushed to the page
        model.adoptPageLocale("en")
        XCTAssertFalse(model.russian); XCTAssertEqual(model.localeSeed, seed + 1)   // adopted, not pushed back
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
    func testDefaultEngineIsFoundWhereTheReadmeInstallsIt() {
        let home = "/srv/example-home"
        let venv = home + "/.local/share/project-observatory-venv/bin/project-observatory"
        // README → Install puts the engine in its own virtual environment.
        XCTAssertEqual(Model.defaultExecutable(home: home) { $0 == venv }, venv)
        // A link on PATH wins over the environment it points into.
        XCTAssertEqual(Model.defaultExecutable(home: home) { $0 == venv || $0 == home + "/.local/bin/project-observatory" },
                       home + "/.local/bin/project-observatory")
        XCTAssertEqual(Model.defaultExecutable(home: home) { $0 == "/opt/homebrew/bin/project-observatory" },
                       "/opt/homebrew/bin/project-observatory")
        // Nothing installed: the documented location, so the message names a real step.
        XCTAssertEqual(Model.defaultExecutable(home: home) { _ in false }, venv)
    }
    func testFirstRunMessagesNameTheInstallAndInitSteps() {
        let model = Model(defaults: defaults())
        let missing = model.message("backend-missing", "/srv/example-home/bin/project-observatory")
        XCTAssertTrue(missing.contains("/srv/example-home/bin/project-observatory"))
        XCTAssertTrue(missing.contains("Install"))
        XCTAssertTrue(model.message("unknown-workspace").contains("project-observatory full init"))
        model.russian = true
        XCTAssertTrue(model.message("backend-missing", "/x").contains("Установите"))
        XCTAssertTrue(model.message("unknown-workspace").contains("project-observatory full init"))
    }
    func testLanguageGivenAsALaunchArgumentIsRead() {
        // `-russian YES` on the command line arrives in the argument domain as a
        // string, not a Bool; it still selects the language.
        let d = defaults(); d.set("YES", forKey: "russian")
        XCTAssertTrue(Model(defaults: d).russian)
        d.set(false, forKey: "russian")
        XCTAssertFalse(Model(defaults: d).russian)
    }
    func testANewWorkspaceWithoutAModelOrBudgetIsNotReadyAndSaysWhy() async {
        // A fresh workspace has no model chain and zero ceilings: "budget reached" was
        // the answer a newcomer got. The status names the missing step instead.
        for (state, code, command) in [("no-model", "model-unconfigured", "full configure model chain"),
                                       ("no-budget", "budget-unset", "full configure budget")] {
            let model = Model(defaults: defaults()) { _, _ in
                ["engine_version": "0.12.0", "agent_enabled": true, "provider_configured": true,
                 "model_configured": false, "model_status": state, "conversations": []]
            }
            await model.refresh()
            XCTAssertFalse(model.ready, state)
            XCTAssertEqual(model.failure?.code, code)
            XCTAssertTrue(model.error?.contains(command) == true, model.error ?? "")
            XCTAssertNotNil(model.sendBlocked)
        }
        // An engine that does not report the field is judged as before.
        let older = Model(defaults: defaults()) { _, _ in
            ["engine_version": "0.12.0", "agent_enabled": true, "provider_configured": true, "conversations": []]
        }
        await older.refresh()
        XCTAssertTrue(older.ready)
    }
    func testEveryErrorTheEngineCanRaiseHasItsOwnWords() throws {
        // The engine's own `AssistantError('…')` codes are the contract: a code with no
        // sentence here reaches the person as "Could not complete the request (code)".
        let src = try String(contentsOf: URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../observatory/engine/agent/assistant.py").standardizedFileURL, encoding: .utf8)
        let rx = try NSRegularExpression(pattern: #"AssistantError\('([a-z-]+)'\)"#)
        let codes = Set(rx.matches(in: src, range: NSRange(src.startIndex..., in: src)).compactMap {
            Range($0.range(at: 1), in: src).map { String(src[$0]) } })
        XCTAssertGreaterThan(codes.count, 20, "the engine's codes were not read")
        let model = Model(defaults: defaults())
        let generic = String(model.message("something-new").prefix(20))
        for code in codes.union(["disk-full", "workspace-unwritable", "model-unconfigured", "budget-unset"]) {
            XCTAssertFalse(model.message(code).hasPrefix(generic), code)
        }
    }
    func testAnUnreadableSourceIsNamedInTheAppsLanguage() {
        // "Не удалось прочитать machine." mixed the engine's file key into Russian text.
        let model = Model(defaults: defaults()); model.russian = true
        for source in ["machine", "projects.json", "findings.json"] {
            let line = model.limitation(["source": source, "code": "unavailable", "reason": "unavailable"])
            XCTAssertFalse(line.contains(source), line)
        }
        model.russian = false
        XCTAssertEqual(model.limitation(["source": "machine", "code": "unavailable"]), "The machine snapshot could not be read.")
    }
}
