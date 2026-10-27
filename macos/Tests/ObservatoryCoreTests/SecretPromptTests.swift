import XCTest
@testable import ObservatoryApp
import ObservatoryCore
import Security

/// A phrase store in memory, standing for the keychain group the signed app holds (or a build that has none).
final class MemoryPhraseStore: PhraseStore {
    var phrase: String?
    var unavailable = false
    init(_ phrase: String? = nil, unavailable: Bool = false) { self.phrase = phrase; self.unavailable = unavailable }
    func read() throws -> String? { if unavailable { throw PhraseStoreError.unavailable }; return phrase }
    func write(_ phrase: String) throws { if unavailable { throw PhraseStoreError.unavailable }; self.phrase = phrase }
}

/// The secret prompt's window model (OBS-260, OSS-84): the request read as the engine's door answers it, the only
/// inputs the window sends, the phrase that every genuine prompt shows, and no window without a place for it.
@MainActor final class SecretPromptTests: XCTestCase {
    private let rid = "sreq_" + String(repeating: "a", count: 24)
    /// A fixture value composed rather than written out, so no key-shaped literal sits in the source.
    private let value = ["fixture", "value", String(repeating: "q", count: 12)].joined(separator: "-")

    private func request(replaces: Bool = false) -> [String: Any] {
        ["requestId": rid, "agent": "alpha", "project": "alpha-web", "slot": ["env": "prod", "name": "STRIPE_KEY"],
         "provider": "stripe", "reason": "deploy the webhook handler", "replaces": replaces,
         "lastChange": replaces ? "2026-10-01T09:00:00Z" as Any : NSNull(), "deadline": "2026-10-27T10:10:00Z"]
    }

    func testTheRequestIsReadAsTheDoorAnswersIt() throws {
        let list = try XCTUnwrap(SecretRequest.list(answer: ["requests": [request(replaces: true), ["requestId": "x"]]]))
        XCTAssertEqual(list.count, 1, "a request this app cannot read is left out, never shown half")
        let r = list[0]
        XCTAssertEqual([r.agent, r.project, r.env, r.name, r.reason], ["alpha", "alpha-web", "prod", "STRIPE_KEY", "deploy the webhook handler"])
        XCTAssertEqual(r.provider, "stripe")
        XCTAssertTrue(r.replaces)
        XCTAssertEqual(r.lastChange, "2026-10-01T09:00:00Z")
        XCTAssertNil(SecretRequest.list(answer: ["error": "client-unverified"]))
    }

    func testTheWindowSendsOnlyItsOwnInputs() {
        XCTAssertEqual(SecretRequest.input(op: "list"), ["op": "list"])
        XCTAssertEqual(SecretRequest.input(op: "decline", requestId: rid), ["op": "decline", "requestId": rid])
        XCTAssertEqual(SecretRequest.input(op: "store", requestId: rid, value: value), ["op": "store", "requestId": rid, "value": value])
        XCTAssertEqual(SecretRequest.input(op: "unavailable", requestId: rid, reason: "phrase-store-unavailable"),
                       ["op": "unavailable", "requestId": rid, "reason": "phrase-store-unavailable"])
        XCTAssertNil(SecretRequest.input(op: "store", requestId: rid, value: "  "), "an empty key is never sent")
        XCTAssertNil(SecretRequest.input(op: "store", requestId: rid, value: "first\nsecond"), "one line only")
        XCTAssertNil(SecretRequest.input(op: "decline", requestId: rid, value: value), "a value travels only in store")
        XCTAssertNil(SecretRequest.input(op: "unavailable", requestId: rid, reason: "other"))
        XCTAssertEqual(SecretRequest.input(op: "unavailable", requestId: rid, reason: "engine-untrusted")?["reason"], "engine-untrusted")
        XCTAssertNil(SecretRequest.input(op: "list", requestId: rid))
        XCTAssertNil(SecretRequest.input(op: "ask"))
        XCTAssertTrue(Backend.actions.contains("secret"))
    }

    func testThePhraseIsOneShortLine() {
        XCTAssertEqual(Phrase.normalized("  blue heron at noon \n"), "blue heron at noon")
        XCTAssertNil(Phrase.normalized("ab"))
        XCTAssertNil(Phrase.normalized(String(repeating: "x", count: 65)))
        XCTAssertNil(Phrase.normalized("two\nlines"))
    }

    func testNoPlaceForThePhraseMeansNoPromptAndAPhraseIsSetAtFirstUse() {
        XCTAssertEqual(SecretPromptStage.of(MemoryPhraseStore(unavailable: true)), .unavailable)
        XCTAssertEqual(SecretPromptStage.of(MemoryPhraseStore()), .needsPhrase)
        XCTAssertEqual(SecretPromptStage.of(MemoryPhraseStore("blue heron at noon")), .prompt(phrase: "blue heron at noon"))
    }

    func testTheKeychainItemIsTheDataProtectionKeychainsNeverTheLoginOne() {
        let q = KeychainPhraseStore.query()
        XCTAssertEqual(q[kSecUseDataProtectionKeychain as String] as? Bool, true)
        XCTAssertNil(q[kSecAttrAccessGroup as String], "the app's own default group, named only by its entitlement")
        XCTAssertEqual(KeychainPhraseStore.error(errSecMissingEntitlement), .unavailable)
    }

    func testAWaitingRequestWithoutAPlaceForThePhraseIsAnsweredNoClientAndShowsNoWindow() async {
        var sent: [[String: String]] = []
        let m = Model(defaults: scratchDefaults(), transport: { action, input in
            XCTAssertEqual(action, "secret")
            sent.append(input)
            return input["op"] == "list" ? ["requests": [self.request()]] : ["request": [:]]
        }, phraseStore: MemoryPhraseStore(unavailable: true))
        m.engineCheck = { _ in .systemOwned }
        await m.readSecretRequests()
        XCTAssertEqual(sent, [["op": "list"], ["op": "unavailable", "requestId": rid, "reason": "phrase-store-unavailable"]])
        XCTAssertEqual(m.secretRequests, [])
        XCTAssertEqual(m.secretStage, .unavailable)
    }

    func testSaveSendsTheValueOnlyToTheDoorAndCancelDeclines() async throws {
        var sent: [[String: String]] = []
        let m = Model(defaults: scratchDefaults(), transport: { _, input in
            sent.append(input)
            return input["op"] == "list" ? ["requests": [self.request()]] : ["request": ["outcome": "stored"]]
        }, phraseStore: MemoryPhraseStore("blue heron at noon"))
        m.engineCheck = { _ in .systemOwned }
        await m.readSecretRequests()
        XCTAssertEqual(m.secretStage, .prompt(phrase: "blue heron at noon"))
        let r = try XCTUnwrap(m.secretRequests.first)
        await m.answerSecret(r, value: value)
        XCTAssertEqual(sent.last, ["op": "store", "requestId": rid, "value": value])
        XCTAssertEqual(m.secretRequests, [])
        await m.answerSecret(r, value: nil)
        XCTAssertEqual(sent.last, ["op": "decline", "requestId": rid])
        await m.answerSecret(r, value: "first\nsecond")
        XCTAssertEqual(sent.count, 3, "a key with a line break never reaches the bridge")
    }

    func testARefusalIsWordedAndAClosedRequestLeavesTheWindow() async throws {
        let m = Model(defaults: scratchDefaults(), transport: { _, input in
            if input["op"] == "list" { return ["requests": [self.request()]] }
            throw BridgeError.backend("request-closed")
        }, phraseStore: MemoryPhraseStore("blue heron at noon"))
        m.russian = false
        m.engineCheck = { _ in .systemOwned }
        await m.readSecretRequests()
        await m.answerSecret(try XCTUnwrap(m.secretRequests.first), value: value)
        XCTAssertEqual(m.secretFailure?.code, "request-closed")
        XCTAssertEqual(m.secretMessage(m.secretFailure!), "This request is no longer waiting.")
        XCTAssertEqual(m.secretRequests, [])
        XCTAssertEqual(m.secretMessage(Failure(code: "client-unverified", detail: nil)),
                       "Only Observatory's own signed app can answer a secret request.")
    }

    func testThePhraseIsSavedOnlyWhereTheAppKeepsIt() {
        let store = MemoryPhraseStore()
        let m = Model(defaults: scratchDefaults(), transport: { _, _ in [:] }, phraseStore: store)
        XCTAssertFalse(m.savePhrase("ab"))
        XCTAssertTrue(m.savePhrase("blue heron at noon"))
        XCTAssertEqual(store.phrase, "blue heron at noon")
        XCTAssertEqual(m.secretStage, .prompt(phrase: "blue heron at noon"))
        let none = Model(defaults: scratchDefaults(), transport: { _, _ in [:] }, phraseStore: MemoryPhraseStore(unavailable: true))
        XCTAssertFalse(none.savePhrase("blue heron at noon"))
        XCTAssertEqual(none.secretStage, .unavailable)
    }

    func testAnEngineAnotherProgramCouldChangeGetsNoWindowAndNoValue() async {
        var sent: [[String: String]] = []
        let m = Model(defaults: scratchDefaults(), transport: { _, input in
            sent.append(input)
            return input["op"] == "list" ? ["requests": [self.request()]] : ["request": [:]]
        }, phraseStore: MemoryPhraseStore("blue heron at noon"))
        m.engineCheck = { _ in .untrusted("outside-bundle-user-writable") }
        await m.readSecretRequests()
        XCTAssertEqual(sent, [["op": "list"], ["op": "unavailable", "requestId": rid, "reason": "engine-untrusted"]])
        XCTAssertEqual(m.secretStage, .untrustedEngine)
        XCTAssertEqual(m.secretRequests, [], "no window, so nothing can be typed into it")
    }

    func testTheSecretDoorRunsWithAScrubbedEnvironment() {
        let inherited = ["PATH": "/tmp/evil:/usr/bin", "PYTHONPATH": "/tmp/evil", "HOME": "/srv/example-ws/home",
                         "DYLD_INSERT_LIBRARIES": "/tmp/x.dylib", "PYTHONSTARTUP": "/tmp/s.py"]
        let env = Backend.environment(for: "secret", inherited: inherited)
        XCTAssertNil(env["PYTHONPATH"]); XCTAssertNil(env["DYLD_INSERT_LIBRARIES"]); XCTAssertNil(env["PYTHONSTARTUP"])
        XCTAssertEqual(env["PYTHONNOUSERSITE"], "1")
        XCTAssertEqual(env["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
        XCTAssertEqual(env["HOME"], "/srv/example-ws/home")
        XCTAssertEqual(Backend.environment(for: "status", inherited: inherited), inherited, "other actions keep theirs")
    }

    /// A file system in memory: path -> (uid, mode, directory), children by folder, a first line per file.
    private func fs(_ entries: [String: (UInt32, UInt16, Bool)], lines: [String: String] = [:]) -> EngineTrust.FileSystem {
        let byDir = Dictionary(grouping: entries.keys.filter { $0 != "/" }) { ($0 as NSString).deletingLastPathComponent }
            .mapValues { $0.map { ($0 as NSString).lastPathComponent } }
        return EngineTrust.FileSystem(
            realpath: { entries[$0] != nil ? $0 : nil },
            lstat: { p in entries[p].map { EngineTrust.Entry(uid: $0.0, mode: $0.1, directory: $0.2) } },
            children: { byDir[$0] ?? (entries[$0]?.2 == true ? [] : nil) },
            firstLine: { lines[$0] })
    }

    func testOnlyAnEngineInsideTheAppOrSealedBySystemIsTrusted() {
        let root: (UInt32, UInt16, Bool) = (0, 0o755, true)
        let sealedFile: (UInt32, UInt16, Bool) = (0, 0o755, false)
        let app = fs(["/": root, "/Applications": root, "/Applications/O.app": root, "/Applications/O.app/Contents": root,
                      "/Applications/O.app/Contents/engine": sealedFile])
        XCTAssertEqual(EngineTrust.verdict(executable: "/Applications/O.app/Contents/engine", bundle: "/Applications/O.app", fs: app), .bundled)
        var system: [String: (UInt32, UInt16, Bool)] = ["/": root, "/opt": root, "/opt/obs": root, "/opt/obs/bin": root,
                                                        "/opt/obs/bin/project-observatory": sealedFile, "/opt/obs/bin/python": sealedFile,
                                                        "/opt/obs/lib": root, "/opt/obs/lib/engine.py": (0, 0o644, false)]
        let script = ["/opt/obs/bin/project-observatory": "#!/opt/obs/bin/python"]
        XCTAssertEqual(EngineTrust.verdict(executable: "/opt/obs/bin/project-observatory", bundle: "/Applications/O.app", fs: fs(system, lines: script)), .systemOwned)
        // One file the user can write anywhere in the tree, and it is not vouched for.
        system["/opt/obs/lib/engine.py"] = (501, 0o644, false)
        XCTAssertFalse(EngineTrust.verdict(executable: "/opt/obs/bin/project-observatory", bundle: "/Applications/O.app", fs: fs(system, lines: script)).trusted)
        system["/opt/obs/lib/engine.py"] = (0, 0o666, false)
        XCTAssertFalse(EngineTrust.verdict(executable: "/opt/obs/bin/project-observatory", bundle: "/Applications/O.app", fs: fs(system, lines: script)).trusted)
        // A per-user install, and a script whose interpreter comes from PATH.
        let user = fs(["/": root, "/srv": root, "/srv/example-ws": root, "/srv/example-ws/home": (501, 0o755, true), "/srv/example-ws/home/venv": (501, 0o755, true),
                       "/srv/example-ws/home/venv/bin": (501, 0o755, true), "/srv/example-ws/home/venv/bin/project-observatory": (501, 0o755, false)])
        XCTAssertEqual(EngineTrust.verdict(executable: "/srv/example-ws/home/venv/bin/project-observatory", bundle: "/Applications/O.app", fs: user),
                       .untrusted("outside-bundle-user-writable"))
        system["/opt/obs/lib/engine.py"] = (0, 0o644, false)
        XCTAssertEqual(EngineTrust.verdict(executable: "/opt/obs/bin/project-observatory", bundle: "/Applications/O.app",
                                           fs: fs(system, lines: ["/opt/obs/bin/project-observatory": "#!/usr/bin/env python3"])),
                       .untrusted("interpreter-unresolved"))
        XCTAssertEqual(EngineTrust.verdict(executable: "/nowhere", bundle: "/Applications/O.app", fs: app), .untrusted("program-unresolved"))
    }
}
