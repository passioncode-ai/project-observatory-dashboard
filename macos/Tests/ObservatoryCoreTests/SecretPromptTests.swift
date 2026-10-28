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
        XCTAssertNil(SecretRequest.input(op: "unavailable", requestId: rid, reason: "engine-untrusted"), "the node answers that one, before filing")
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
        m.secretEngine = { SecretPromptTests.bundled }
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
        m.secretEngine = { SecretPromptTests.bundled }
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
        m.secretEngine = { SecretPromptTests.bundled }
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

    nonisolated static let bundled = "/Applications/O.app/" + SecretEngine.relativePath

    func testWithoutABundledEngineOrAValidSignatureNothingIsReadOrSent() async {
        var sent: [[String: String]] = []
        let m = Model(defaults: scratchDefaults(), transport: { _, input in
            sent.append(input)
            return ["requests": [self.request()]]
        }, phraseStore: MemoryPhraseStore("blue heron at noon"))
        m.secretEngine = { nil }
        await m.readSecretRequests()
        XCTAssertEqual(sent, [], "no engine of its own, no call: the node already answered engine-untrusted")
        XCTAssertEqual(m.secretRequests, [])
        let r = try! XCTUnwrap(SecretRequest(request()))
        await m.answerSecret(r, value: value)
        XCTAssertEqual(sent, [], "a typed value never reaches any program")
        XCTAssertEqual(m.secretFailure?.code, "engine-untrusted")
    }

    func testTheSecretCallNeverUsesTheExecutablePreference() async {
        let d = scratchDefaults()
        d.set("/usr/bin/git", forKey: "executable")
        let m = Model(defaults: d, transport: nil, phraseStore: MemoryPhraseStore("blue heron at noon"))
        m.secretEngine = { SecretPromptTests.bundled }
        let backend = await m.secretBackend()
        XCTAssertEqual(backend?.executable, SecretPromptTests.bundled)
        XCTAssertNotEqual(backend?.executable, m.executable)
        m.secretEngine = { nil }
        let none = await m.secretBackend()
        XCTAssertNil(none, "no bundled engine: no backend, never the preference")
    }

    func testTheSignatureIsCheckedAgainForEveryCall() async throws {
        let checks = Counter()
        let m = Model(defaults: scratchDefaults(), transport: { _, input in
            input["op"] == "list" ? ["requests": [self.request()]] : ["request": ["outcome": "stored"]]
        }, phraseStore: MemoryPhraseStore("blue heron at noon"))
        m.secretEngine = { checks.bump(); return SecretPromptTests.bundled }
        await m.readSecretRequests()
        await m.answerSecret(try XCTUnwrap(m.secretRequests.first), value: value)
        XCTAssertEqual(checks.value, 2, "list and store each resolve the engine and check the signature")
    }

    func testResolveRequiresTheSealedTeamTheEngineInsideAndTheSignature() {
        let bundle = URL(fileURLWithPath: "/Applications/O.app")
        var asked: [String] = []
        XCTAssertNil(SecretEngine.resolve(bundle: bundle, team: nil, signatureValid: { _, r in asked.append(r); return true }, isExecutable: { _ in true }))
        XCTAssertNil(SecretEngine.resolve(bundle: bundle, team: "lower12345", signatureValid: { _, r in asked.append(r); return true }, isExecutable: { _ in true }))
        XCTAssertEqual(asked, [], "no team in the sealed Info.plist, no check and no engine")
        XCTAssertNil(SecretEngine.resolve(bundle: bundle, team: "ABCDE12345", signatureValid: { _, _ in false }, isExecutable: { _ in true }),
                     "a signature that does not check out starts nothing")
        XCTAssertNil(SecretEngine.resolve(bundle: bundle, team: "ABCDE12345", signatureValid: { _, r in asked.append(r); return true }, isExecutable: { _ in false }),
                     "this release bundles no engine")
        XCTAssertEqual(asked, [], "no engine inside, so the bundle is never hashed")
        let path = SecretEngine.resolve(bundle: bundle, team: "ABCDE12345", signatureValid: { _, r in asked.append(r); return true }, isExecutable: { _ in true })
        XCTAssertEqual(path, SecretPromptTests.bundled)
        XCTAssertTrue(asked.last!.contains("identifier \"ai.passioncode.observatory\""))
        XCTAssertTrue(asked.last!.contains("certificate leaf[subject.OU] = \"ABCDE12345\""))
        XCTAssertTrue(asked.last!.hasPrefix("anchor apple generic"))
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
}

/// Counts calls from a `@Sendable` closure.
final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var n = 0
    func bump() { lock.lock(); n += 1; lock.unlock() }
    var value: Int { lock.lock(); defer { lock.unlock() }; return n }
}
