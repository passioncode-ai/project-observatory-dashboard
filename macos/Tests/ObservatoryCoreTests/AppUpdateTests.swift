import XCTest
@testable import ObservatoryApp
import ObservatoryCore

/// LC-16 activation: a staged update is found, offered and activated only at a safe point.
final class AppUpdateTests: XCTestCase {
    private func temp(_ name: String = "obs-update") throws -> URL {
        let url = URL(fileURLWithPath: Model.resolved(FileManager.default.temporaryDirectory
            .appendingPathComponent("\(name)-\(UUID().uuidString)").path))
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
    private func record(_ app: [String: Any]) -> Data { try! JSONSerialization.data(withJSONObject: ["app": app, "pass": ["at": "2026-10-06T10:00:00Z"]]) }

    // MARK: version and the maintenance record

    func testOnlyAStrictlyNewerReadableVersionIsOffered() {
        XCTAssertTrue(AppVersion.isNewer("0.19.0", than: "0.18.0"))
        XCTAssertTrue(AppVersion.isNewer("0.18.10", than: "0.18.9"), "numeric, not text, order")
        XCTAssertTrue(AppVersion.isNewer("1.0.0", than: "0.99.99"))
        XCTAssertFalse(AppVersion.isNewer("0.18.0", than: "0.18.0"))
        XCTAssertFalse(AppVersion.isNewer("0.17.3", than: "0.18.0"))
        XCTAssertFalse(AppVersion.isNewer("0.19", than: "0.18.0"))
        XCTAssertFalse(AppVersion.isNewer("0.19.0-rc1", than: "0.18.0"))
        XCTAssertFalse(AppVersion.isNewer("0.19.0", than: ""), "a running version that cannot be read is never behind")
        XCTAssertFalse(AppVersion.isNewer("０.19.0", than: "0.18.0"), "ASCII digits only")
    }

    func testAStagedUpdateIsReadFromTheMaintenanceRecord() {
        let waiting = record(["result": "waiting-for-quit", "version": "0.18.0", "pending": "0.19.0", "target": "0.19.0"])
        XCTAssertEqual(UpdateState.pending(waiting, running: "0.18.0"), StagedUpdate(version: "0.19.0"))
        XCTAssertNil(UpdateState.pending(waiting, running: "0.19.0"), "already running it")
        XCTAssertNil(UpdateState.pending(waiting, running: "0.20.0"), "older than the running app")
        for other in ["updated", "current", "refused", "not-installed", "unsigned-install", "error"] {
            XCTAssertNil(UpdateState.pending(record(["result": other, "pending": "0.19.0"]), running: "0.18.0"), other)
        }
        XCTAssertNil(UpdateState.pending(record(["result": "waiting-for-quit", "pending": 19]), running: "0.18.0"))
        XCTAssertNil(UpdateState.pending(record(["result": "waiting-for-quit"]), running: "0.18.0"))
        XCTAssertNil(UpdateState.pending(Data("{not json".utf8), running: "0.18.0"))
        XCTAssertNil(UpdateState.pending(try! JSONSerialization.data(withJSONObject: ["pass": [:]]), running: "0.18.0"))
        XCTAssertNil(UpdateState.pending(nil, running: "0.18.0"))
    }

    func testTheRecordIsReadFromTheWorkspaceAndNeverThroughALink() throws {
        let ws = try temp()
        XCTAssertNil(UpdateState.read(workspace: ws.path, running: "0.18.0"), "no record yet")
        try FileManager.default.createDirectory(at: ws.appendingPathComponent("store"), withIntermediateDirectories: true)
        try record(["result": "waiting-for-quit", "pending": "0.19.0"]).write(to: UpdateState.file(workspace: ws.path))
        XCTAssertEqual(UpdateState.read(workspace: ws.path, running: "0.18.0")?.version, "0.19.0")
        XCTAssertNil(UpdateState.read(workspace: "relative/ws", running: "0.18.0"))
        let other = try temp()
        try FileManager.default.createDirectory(at: other.appendingPathComponent("store"), withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(at: UpdateState.file(workspace: other.path), withDestinationURL: UpdateState.file(workspace: ws.path))
        XCTAssertNil(UpdateState.read(workspace: other.path, running: "0.18.0"))
    }

    // MARK: idle

    func testIdleRestartNeedsAStagedUpdateNoWindowNoInputAndNoWork() {
        let now = Date()
        let long = now.addingTimeInterval(-IdleRestart.after - 1)
        let idle = IdleRestart.Observation(staged: true, windowOnScreen: false, active: false, workInFlight: false, lastSeen: long, lastInput: long)
        XCTAssertTrue(IdleRestart.shouldRestart(idle, now: now))
        var o = idle; o.staged = false
        XCTAssertFalse(IdleRestart.shouldRestart(o, now: now), "nothing staged")
        o = idle; o.windowOnScreen = true
        XCTAssertFalse(IdleRestart.shouldRestart(o, now: now), "a window is on screen")
        o = idle; o.active = true
        XCTAssertFalse(IdleRestart.shouldRestart(o, now: now), "the app is frontmost")
        o = idle; o.workInFlight = true
        XCTAssertFalse(IdleRestart.shouldRestart(o, now: now), "a question or a build is running")
        o = idle; o.lastSeen = now.addingTimeInterval(-IdleRestart.after + 60)
        XCTAssertFalse(IdleRestart.shouldRestart(o, now: now), "the window closed less than 30 min ago")
        o = idle; o.lastInput = now.addingTimeInterval(-60)
        XCTAssertFalse(IdleRestart.shouldRestart(o, now: now), "input a minute ago")
        o = idle; o.lastSeen = now.addingTimeInterval(-IdleRestart.after); o.lastInput = o.lastSeen
        XCTAssertTrue(IdleRestart.shouldRestart(o, now: now), "exactly 30 min counts")
    }

    // MARK: the helper's plan

    func testThePlanRefusesWhatItCannotDoAndPassesValuesAsArguments() {
        let ok = RestartHelper.Plan(pid: 4242, engine: "/opt/po/bin/project-observatory", workspace: "/srv/example-ws",
                                    bundle: "/Applications/Project Observatory.app", mode: .restart, log: "/srv/logs/app.log", version: "0.19.0")
        XCTAssertNil(ok.refusal)
        XCTAssertEqual(Array(ok.argv.prefix(2)), ["/bin/sh", "-c"])
        XCTAssertEqual(Array(ok.argv.dropFirst(4)), ["4242", "/opt/po/bin/project-observatory", "/srv/example-ws",
                                                     "/Applications/Project Observatory.app", "restart", "/srv/logs/app.log", "/usr/bin/open", "0.19.0"])
        for value in ["/srv/example-ws", "/opt/po", "0.19.0"] { XCTAssertFalse(RestartHelper.script.contains(value)) }
        XCTAssertTrue(RestartHelper.script.contains("full maintain app"), "the one documented command")
        var p = ok; p.bundle = "/build/debug"
        XCTAssertEqual(p.refusal, "not-a-bundle", "a restart needs an app to open again")
        p.mode = .quit
        XCTAssertNil(p.refusal, "a quit opens nothing")
        p = ok; p.engine = "project-observatory"
        XCTAssertEqual(p.refusal, "not-configured")
        p = ok; p.version = "0.19.0; rm -rf /"
        XCTAssertEqual(p.refusal, "no-update")
        p = ok; p.pid = 0
        XCTAssertEqual(p.refusal, "no-process")
    }

    // MARK: the helper itself, with a fake engine and a fake `open`

    private struct Run { let log: [[String: String]]; let opened: [String]; let engineSaw: String; let seconds: TimeInterval }

    /// Runs the real helper script against an engine that answers `answer` with `status`,
    /// after a process that lives `appLives` seconds has exited.
    private func runHelper(mode: RestartHelper.Mode, answer: String, status: Int32 = 0, appLives: Double = 0) throws -> Run {
        let dir = try temp("obs-helper")
        let engine = dir.appendingPathComponent("engine"), opener = dir.appendingPathComponent("open")
        let saw = dir.appendingPathComponent("engine-saw"), opened = dir.appendingPathComponent("opened")
        let log = dir.appendingPathComponent("logs/app.log")
        try FileManager.default.createDirectory(at: log.deletingLastPathComponent(), withIntermediateDirectories: true)
        try """
        #!/bin/sh
        printf '%s|%s' "$OBSERVATORY_HOME" "$*" > '\(saw.path)'
        cat <<'JSON'
        \(answer)
        JSON
        exit \(status)
        """.write(to: engine, atomically: true, encoding: .utf8)
        try "#!/bin/sh\nprintf '%s\\n' \"$*\" >> '\(opened.path)'\n".write(to: opener, atomically: true, encoding: .utf8)
        for f in [engine, opener] { try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: f.path) }

        let app = Process(); app.executableURL = URL(fileURLWithPath: "/bin/sleep"); app.arguments = [String(appLives)]
        try app.run()
        let plan = RestartHelper.Plan(pid: app.processIdentifier, engine: engine.path, workspace: "/srv/example-ws",
                                      bundle: "/srv/apps/Project Observatory.app", mode: mode, log: log.path,
                                      opener: opener.path, version: "0.19.0")
        XCTAssertNil(plan.refusal)
        let helper = Process(); helper.executableURL = URL(fileURLWithPath: plan.argv[0]); helper.arguments = Array(plan.argv.dropFirst())
        let started = Date()
        try helper.run(); helper.waitUntilExit(); app.waitUntilExit()
        let lines = ((try? String(contentsOf: log, encoding: .utf8)) ?? "").split(separator: "\n").map {
            (try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: String]) ?? ["unparsed": String($0)]
        }
        return Run(log: lines, opened: ((try? String(contentsOf: opened, encoding: .utf8)) ?? "").split(separator: "\n").map(String.init),
                   engineSaw: (try? String(contentsOf: saw, encoding: .utf8)) ?? "", seconds: Date().timeIntervalSince(started))
    }

    func testRestartWaitsForTheAppSwapsAndOpensTheNewOne() throws {
        let r = try runHelper(mode: .restart, answer: #"{"status": "ran", "app": {"result": "updated", "version": "0.19.0"}}"#, appLives: 0.6)
        XCTAssertGreaterThanOrEqual(r.seconds, 0.5, "the swap waits for the app to exit")
        XCTAssertEqual(r.engineSaw, "/srv/example-ws|full maintain app")
        XCTAssertEqual(r.log.map { $0["code"] }, ["started", "installed"])
        XCTAssertEqual(Set(r.log.map { $0["event"] }), ["update_install"])
        XCTAssertTrue(r.log.allSatisfy { $0["version"] == "0.19.0" && $0["trigger"] == "restart" && ($0["at"] ?? "").hasSuffix("Z") }, "\(r.log)")
        XCTAssertEqual(r.opened, ["/srv/apps/Project Observatory.app"])
    }

    func testARefusedSwapReopensTheOldAppAndSaysWhy() throws {
        let r = try runHelper(mode: .restart, answer: #"{"status": "ran", "app": {"result": "refused", "detail": "codesign refused"}}"#)
        XCTAssertEqual(r.log.last?["code"], "failed"); XCTAssertEqual(r.log.last?["reason"], "refused")
        XCTAssertFalse(r.log.contains { $0.values.contains { $0.contains("codesign") } }, "codes only, never the engine's words")
        XCTAssertEqual(r.opened, ["/srv/apps/Project Observatory.app"], "the old app comes back")
    }

    func testAFailedEngineInTheBackgroundReopensWithoutAWindow() throws {
        let r = try runHelper(mode: .background, answer: "Observatory: not a workspace", status: 2)
        XCTAssertEqual(r.log.last?["code"], "failed"); XCTAssertEqual(r.log.last?["reason"], "engine-failed")
        XCTAssertEqual(r.opened, ["-g /srv/apps/Project Observatory.app --args --background"])
    }

    func testAQuitSwapsAndOpensNothing() throws {
        let ok = try runHelper(mode: .quit, answer: #"{"status": "ran", "app": {"result": "updated"}}"#)
        XCTAssertEqual(ok.log.last?["code"], "installed"); XCTAssertEqual(ok.opened, [])
        let failed = try runHelper(mode: .quit, answer: #"{"status": "ran", "app": {"result": "waiting-for-quit"}}"#)
        XCTAssertEqual(failed.log.last?["code"], "failed"); XCTAssertEqual(failed.log.last?["reason"], "waiting-for-quit")
        XCTAssertEqual(failed.opened, [], "the person quit: nothing reopens")
    }

    func testTheHelperRunsInASessionOfItsOwn() throws {
        let dir = try temp("obs-detached")
        let out = dir.appendingPathComponent("ids")
        let pid = try RestartHelper.spawnDetached(["/bin/sh", "-c", #"printf '%s %s' "$$" "$(ps -o pgid= -p $$ | tr -d ' ')" > "$1.tmp" && mv "$1.tmp" "$1""#, "x", out.path], env: [:])
        var status: Int32 = 0
        XCTAssertEqual(waitpid(pid, &status, 0), pid)
        let ids = try String(contentsOf: out, encoding: .utf8).split(separator: " ").map(String.init)
        XCTAssertEqual(ids, [String(pid), String(pid)], "the helper leads its own session and process group")
        XCTAssertNotEqual(getpgrp(), pid)
        XCTAssertThrowsError(try RestartHelper.spawnDetached(["sh"], env: [:]), "a relative program is refused")
    }

    // MARK: the log

    func testTheLogHoldsCodesOnlyInUTC() throws {
        let at = Date(timeIntervalSince1970: 1_790_000_000)
        let line = try XCTUnwrap(AppLog.line(event: "update_restart", code: "requested",
                                             fields: ["trigger": "person", "version": "0.19.0", "path": "/srv/private place", "detail": "a sentence."], at: at))
        let doc = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: String])
        XCTAssertEqual(doc, ["at": "2026-09-21T14:13:20Z", "event": "update_restart", "code": "requested", "trigger": "person", "version": "0.19.0"])
        XCTAssertNil(AppLog.line(event: "update_restart", code: "installed", at: at), "a code outside the shared list")
        XCTAssertNil(AppLog.line(event: "something_else", code: "requested", at: at))
        XCTAssertNotNil(AppLog.line(event: "update_install", code: "timeout", at: at))
    }

    func testTheLogIsPrivateAndKeptUnderItsCap() throws {
        let dir = try temp("obs-log")
        let log = AppLog(file: dir.appendingPathComponent("Logs/Project Observatory/app.log"))
        XCTAssertTrue(log.record("update_restart", "refused", ["reason": "no-update"]))
        let attrs = try FileManager.default.attributesOfItem(atPath: log.file.path)
        XCTAssertEqual((attrs[.posixPermissions] as? NSNumber)?.intValue, 0o600)
        XCTAssertEqual(((try FileManager.default.attributesOfItem(atPath: log.file.deletingLastPathComponent().path))[.posixPermissions] as? NSNumber)?.intValue, 0o700)
        try Data(count: AppLog.cap + 1).write(to: log.file)
        XCTAssertTrue(log.record("update_restart", "requested"))
        XCTAssertEqual(try String(contentsOf: log.file, encoding: .utf8).split(separator: "\n").count, 1)
        XCTAssertTrue(FileManager.default.fileExists(atPath: log.file.path + ".1"))
        XCTAssertEqual(AppLog.standard(home: "/srv/home").file.path, "/srv/home/Library/Logs/Project Observatory/app.log")
    }
}

/// The app's update watch, with the spawn and the quit replaced by recorders.
@MainActor final class UpdatesTests: XCTestCase {
    private var spawned: [[String]] = [], quits = 0
    private func setUp(pending: String?, spawn: (([String], [String: String]) throws -> pid_t)? = nil) throws -> (Updates, AppLog) {
        let ws = URL(fileURLWithPath: Model.resolved(FileManager.default.temporaryDirectory.appendingPathComponent("obs-updates-\(UUID().uuidString)").path))
        try FileManager.default.createDirectory(at: ws.appendingPathComponent("store"), withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: ws) }
        if let pending {
            try JSONSerialization.data(withJSONObject: ["app": ["result": "waiting-for-quit", "pending": pending]])
                .write(to: UpdateState.file(workspace: ws.path))
        }
        let d = scratchDefaults()
        d.set(ws.path, forKey: "workspace"); d.set("/opt/po/bin/project-observatory", forKey: "executable")
        let log = AppLog(file: ws.appendingPathComponent("logs/app.log"))
        spawned = []; quits = 0
        let u = Updates(defaults: d, log: log, running: "0.18.0", bundlePath: "/srv/apps/Project Observatory.app",
                        spawn: spawn ?? { [unowned self] argv, env in
                            XCTAssertEqual(env["OBSERVATORY_HOME"], ws.path)
                            self.spawned.append(argv); return 4242 },
                        terminate: { [unowned self] in self.quits += 1 })
        return (u, log)
    }
    private func events(_ log: AppLog) -> [String] {
        ((try? String(contentsOf: log.file, encoding: .utf8)) ?? "").split(separator: "\n").compactMap {
            guard let d = try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: String] else { return nil }
            return "\(d["event"] ?? "") \(d["code"] ?? "") \(d["reason"] ?? d["trigger"] ?? "")"
        }
    }

    func testRestartToUpdateStartsOneHelperLogsAndQuits() throws {
        let (u, log) = try setUp(pending: "0.19.0")
        XCTAssertEqual(u.check()?.version, "0.19.0"); XCTAssertEqual(u.staged?.version, "0.19.0")
        u.restartToUpdate()
        XCTAssertEqual(spawned.count, 1); XCTAssertEqual(quits, 1)
        XCTAssertEqual(Array(spawned[0].dropFirst(5)), ["/opt/po/bin/project-observatory", spawned[0][6], "/srv/apps/Project Observatory.app",
                                                        "restart", log.file.path, "/usr/bin/open", "0.19.0"])
        XCTAssertEqual(events(log), ["update_restart requested person"])
        u.installOnQuit()
        XCTAssertEqual(spawned.count, 1, "the quit that follows starts no second helper")
    }

    func testNothingStagedIsARefusalNotARestart() throws {
        let (u, log) = try setUp(pending: "0.18.0")
        XCTAssertNil(u.check())
        u.restartToUpdate()
        XCTAssertEqual(spawned.count, 0); XCTAssertEqual(quits, 0)
        XCTAssertEqual(u.refusal, "no-update")
        XCTAssertEqual(events(log), ["update_restart refused no-update"])
        u.installOnQuit()
        XCTAssertEqual(spawned.count, 0)
    }

    func testAHelperThatCannotStartKeepsTheAppRunning() throws {
        let (u, log) = try setUp(pending: "0.19.0", spawn: { _, _ in throw BridgeError.failed("EAGAIN") })
        u.restartToUpdate()
        XCTAssertEqual(quits, 0, "no helper, no quit"); XCTAssertFalse(u.helperStarted)
        XCTAssertEqual(u.refusal, "spawn-failed")
        XCTAssertEqual(events(log), ["update_restart requested person", "update_restart refused spawn-failed"])
    }

    func testQuittingWithAnUpdateStagedSwapsItAfterTheAppIsGone() throws {
        let (u, log) = try setUp(pending: "0.19.0")
        u.installOnQuit()
        XCTAssertEqual(spawned.count, 1); XCTAssertEqual(spawned[0][8], "quit")
        XCTAssertEqual(quits, 0, "the app is already quitting")
        XCTAssertEqual(events(log), [], "a quit is not a restart: the helper logs the install")
    }

    func testABuildWithoutAnAppBundleRefusesToRestart() throws {
        let (_, log) = try setUp(pending: "0.19.0")
        let d = scratchDefaults(); d.set("/srv/example-ws", forKey: "workspace"); d.set("/opt/po/bin/project-observatory", forKey: "executable")
        let dev = Updates(defaults: d, log: log, running: "0.18.0", bundlePath: "/build/debug", spawn: { _, _ in 1 }, terminate: {})
        XCTAssertEqual(dev.startHelper(.restart, version: "0.19.0"), "not-a-bundle")
        XCTAssertNil(dev.startHelper(.quit, version: "0.19.0"))
    }
}
