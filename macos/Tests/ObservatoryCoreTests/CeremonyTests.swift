import XCTest
@testable import ObservatoryApp
import ObservatoryCore

/// The pairing window (OBS-197): the engine's screen read as it is, the steps it may send, and the words and buttons
/// of every state. The screens below are the engine's own answers for the contract's pairing vectors
/// (`observatory/engine/tests/test_node_ceremony.py` builds them), with synthetic key fingerprints.
@MainActor final class CeremonyTests: XCTestCase {
    private let netPrint = "15635 08363 79221 33032 89140"
    private let fp = "sha256:" + String(repeating: "a", count: 64)

    private func answer(_ screen: [String: Any], hub: String = "ready") -> [String: Any] {
        ["screen": screen.merging(["schema": "node-ceremony/1"]) { a, _ in a }, "hub": hub]
    }
    private var screens: [String: [String: Any]] {
        [
            "not-paired": answer(["state": "not-paired"], hub: "not-ready"),
            "hub-unreachable": answer(["state": "hub-unreachable", "hub": "not-ready", "step": "other-side",
                                       "expiresAt": "2026-10-15T09:10:00Z", "secondsLeft": 480], hub: "not-ready"),
            "waiting-for-hub": answer(["state": "waiting-for-hub", "step": "revision", "code": "724799",
                                       "fingerprint": netPrint, "secondsLeft": 480]),
            "code-shown": answer(["state": "code-shown", "code": "724799", "fingerprint": netPrint, "secondsLeft": 480]),
            "passkey": answer(["state": "passkey", "machine": "m-7b2d", "canSign": false, "hub": "not-ready",
                               "keys": [["kid": "m-7b2d#sig", "use": "sig", "fingerprint": fp],
                                        ["kid": "m-7b2d#kem", "use": "kem", "fingerprint": fp]],
                               "code": "724799", "fingerprint": netPrint, "secondsLeft": 480], hub: "not-ready"),
            "confirmed": answer(["state": "confirmed", "machine": "m-7b2d", "fingerprint": netPrint]),
            "refused": answer(["state": "refused", "reason": "codes-differ", "group": "codes-differ", "index": 4]),
            "expired": answer(["state": "expired", "expiresAt": "2026-10-15T09:10:00Z"]),
        ]
    }

    func testEveryStateTheEngineAnswersIsReadAsIs() throws {
        let expected: [String: CeremonyScreen.State] = [
            "not-paired": .notPaired, "hub-unreachable": .hubUnreachable, "waiting-for-hub": .waitingForHub,
            "code-shown": .codeShown, "passkey": .passkey, "confirmed": .confirmed, "refused": .refused, "expired": .expired,
        ]
        for (name, doc) in screens {
            let screen = try XCTUnwrap(CeremonyScreen(answer: doc), name)
            XCTAssertEqual(screen.state, expected[name], name)
        }
        let passkey = try XCTUnwrap(CeremonyScreen(answer: screens["passkey"]!))
        XCTAssertEqual(passkey.keys.map(\.kid), ["m-7b2d#sig", "m-7b2d#kem"])
        XCTAssertFalse(passkey.canSign)
        XCTAssertEqual(passkey.code, "724799")
        let waiting = try XCTUnwrap(CeremonyScreen(answer: screens["hub-unreachable"]!))
        XCTAssertEqual(waiting.hub, "not-ready")
        XCTAssertEqual(waiting.secondsLeft, 480)
    }

    func testAScreenFromANewerEngineIsUnknownNeverAnotherState() throws {
        let screen = try XCTUnwrap(CeremonyScreen(answer: answer(["state": "teleported"])))
        XCTAssertEqual(screen.state, .unknown("teleported"))
        XCTAssertNil(CeremonyScreen(answer: ["screen": ["schema": "another/1", "state": "confirmed"]]))
        XCTAssertNil(CeremonyScreen(answer: ["error": "not-now"]))
    }

    func testTheWindowSendsOnlyTheCeremonysSteps() {
        XCTAssertEqual(CeremonyScreen.input(op: "status"), ["op": "status"])
        XCTAssertEqual(CeremonyScreen.input(op: "start", role: "new"), ["op": "start", "role": "new"])
        XCTAssertNil(CeremonyScreen.input(op: "start"), "a start names its role")
        XCTAssertNil(CeremonyScreen.input(op: "start", role: "observer"))
        XCTAssertNil(CeremonyScreen.input(op: "confirm", role: "new"), "only a start carries a role")
        XCTAssertNil(CeremonyScreen.input(op: "ask"))
        XCTAssertTrue(Backend.actions.contains("ceremony"))
    }

    func testEveryStateHasItsWordsAndOnlyTheButtonsItOffers() throws {
        for russian in [false, true] {
            let m = Model(defaults: scratchDefaults()) { _, _ in [:] }
            m.russian = russian
            for (name, doc) in screens {
                let s = try XCTUnwrap(CeremonyScreen(answer: doc))
                let text = m.pairingText(s)
                XCTAssertFalse(text.title.isEmpty, name)
                XCTAssertFalse(text.body.isEmpty, name)
                XCTAssertFalse(text.body.contains("{"), "\(name): a placeholder was left unfilled: \(text.body)")
            }
            let ops = { (name: String) in m.pairingActions(CeremonyScreen(answer: self.screens[name]!)!).map(\.op) }
            XCTAssertEqual(ops("not-paired"), ["start"])
            XCTAssertEqual(ops("hub-unreachable"), ["cancel"])
            XCTAssertEqual(ops("code-shown"), ["confirm", "differ"])
            XCTAssertEqual(ops("passkey"), ["sign", "cancel"])
            XCTAssertEqual(ops("confirmed"), [])
            XCTAssertEqual(ops("refused"), ["cancel"])
            XCTAssertEqual(ops("expired"), ["cancel"])
            let sign = m.pairingActions(CeremonyScreen(answer: screens["passkey"]!)!).first!
            XCTAssertFalse(sign.enabled, "the passkey never signs while the hub serves no page")
        }
    }

    func testTheWaitOnTheHubIsSaidAsSuchNeverAsProgress() throws {
        let m = Model(defaults: scratchDefaults()) { _, _ in [:] }
        m.russian = false
        let s = try XCTUnwrap(CeremonyScreen(answer: screens["hub-unreachable"]!))
        XCTAssertEqual(m.pairingText(s).title, "Waiting for the hub")
        XCTAssertTrue(m.pairingText(s).body.contains("Nothing was sent"), m.pairingText(s).body)
    }

    func testAStepGoesThroughTheBridgeAndARefusalIsWorded() async throws {
        var sent: [(String, [String: String])] = []
        let screen = screens["code-shown"]!
        let m = Model(defaults: scratchDefaults()) { action, input in
            sent.append((action, input))
            if input["op"] == "sign" { throw BridgeError.backend("hub-not-ready") }
            return screen
        }
        m.russian = false
        await m.pairingStep("status")
        XCTAssertEqual(sent.first?.0, "ceremony")
        XCTAssertEqual(sent.first?.1, ["op": "status"])
        XCTAssertEqual(m.pairing?.state, .codeShown)
        await m.pairingStep("sign")
        XCTAssertEqual(m.pairingFailure?.code, "hub-not-ready")
        XCTAssertEqual(m.pairingMessage(m.pairingFailure!), m.t("Your passkey signs on the hub's page, which is not live yet. Nothing has been signed."))
        await m.pairingStep("serve")
        XCTAssertEqual(sent.count, 2, "a step the window does not offer never reaches the bridge")
    }
}
