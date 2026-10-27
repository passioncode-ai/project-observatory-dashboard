import SwiftUI
import AppKit
import ObservatoryCore

/// One button the pairing window offers: a step the engine takes, never a rule the window decides.
struct PairingAction: Equatable {
    let label: String
    let op: String
    var role: String? = nil
    var prominent = false
    var enabled = true
}

extension Model {
    /// One step of the pairing ceremony through the engine; the answer's screen replaces the shown one.
    func pairingStep(_ op: String, role: String? = nil) async {
        guard let input = CeremonyScreen.input(op: op, role: role) else { return }
        if pairingBusy && op != "status" { return }
        if op != "status" { pairingBusy = true }
        defer { if op != "status" { pairingBusy = false } }
        do {
            let answer = try await ceremonyCall(input)
            if let screen = CeremonyScreen(answer: answer) {
                pairing = screen
                if op != "status" { pairingFailure = nil }
            } else {
                pairingFailure = Failure(code: "backend-invalid-response", detail: nil)
            }
        } catch {
            pairingFailure = failureOf(error)
        }
    }

    /// The window's title and its one sentence, for every state the engine answers (portable scenario OSS-94).
    func pairingText(_ s: CeremonyScreen) -> (title: String, body: String) {
        let seconds = String(s.secondsLeft ?? 0)
        switch s.state {
        case .notPaired:
            return (t("Pair this machine"), t("Add this machine to your network. Both machines will show a six-digit code, and you check that they match."))
        case .hubUnreachable:
            return (t("Waiting for the hub"), s.hub == "not-ready"
                    ? t("The network's hub does not run pairing yet. Nothing was sent and no machine was added. Cancel, and pair again once the hub is ready.")
                    : t("The network's hub did not answer. Nothing was sent and no machine was added. Check the connection, then try again."))
        case .waitingForHub:
            if s.step == "revision" {
                return (t("Waiting for the new revision"), t("The codes match. The machine already in your network now signs a revision that lists this one; it arrives through the hub. {seconds} s left.", ["seconds": seconds]))
            }
            return (t("Waiting for the other machine"), t("Open pairing on a machine that is already in your network. This one waits {seconds} s more.", ["seconds": seconds]))
        case .codeShown:
            return (t("Compare the codes"), t("Both screens must show the same six digits. If they differ, someone may be in between: stop the pairing."))
        case .passkey:
            return (t("Sign with your passkey"), t("Your passkey signs a revision that adds {machine} to your network. Check that these keys are the new machine's before you sign:", ["machine": s.machine ?? ""]))
        case .confirmed:
            return (t("Pairing complete"), t("Your network now lists {machine} with its keys.", ["machine": s.machine ?? ""]))
        case .refused:
            return (t("Pairing stopped"), pairingReason(s.group))
        case .expired:
            return (t("The pairing expired"), t("Ten minutes passed before both machines finished. Nothing was added; start again on both machines."))
        case .unknown(let raw):
            return (t("Pair this machine"), message("backend-failed", raw))
        }
    }

    /// The sentence for a refusal's group; a group this app does not know reads as a mismatch.
    func pairingReason(_ group: String?) -> String {
        switch group {
        case "codes-differ": return t("You said the codes differ. Nothing was added. Check for anyone between the machines, then start again on both.")
        case "aborted": return t("The other machine stopped the pairing. Nothing was added.")
        case "other-estate": return t("The other machine belongs to another network. Nothing was added.")
        case "not-listed": return t("The network's new revision does not list this machine with the keys it showed. Nothing was added.")
        case "trusted-not-listed": return t("The network's new revision no longer lists the machine you paired with. Nothing was added.")
        default: return t("A message did not match what its machine had promised, so the pairing stopped. Nothing was added; start again on both machines.")
        }
    }

    /// The buttons a state offers. `cancel` also starts over: it drops the ended session, and the next screen offers
    /// the start again.
    func pairingActions(_ s: CeremonyScreen) -> [PairingAction] {
        switch s.state {
        case .notPaired: return [PairingAction(label: t("Start pairing"), op: "start", role: "new", prominent: true)]
        case .hubUnreachable, .waitingForHub: return [PairingAction(label: t("Cancel pairing"), op: "cancel")]
        case .codeShown:
            return [PairingAction(label: t("The codes match"), op: "confirm", prominent: true),
                    PairingAction(label: t("They differ"), op: "differ")]
        case .passkey:
            return [PairingAction(label: t("Sign with passkey"), op: "sign", prominent: true, enabled: s.canSign),
                    PairingAction(label: t("Cancel pairing"), op: "cancel")]
        case .refused, .expired: return [PairingAction(label: t("Start again"), op: "cancel", prominent: true)]
        case .confirmed, .unknown: return []
        }
    }

    /// A step the engine refused, in words; the bridge's own failures read as everywhere else in the app.
    func pairingMessage(_ f: Failure) -> String {
        switch f.code {
        case "hub-not-ready": return t("Your passkey signs on the hub's page, which is not live yet. Nothing has been signed.")
        case "device-keys-refused": return t("This machine's keys could not be read or created. Run `project-observatory full node --init` in a terminal to see why.")
        case "ceremony-in-progress": return t("A pairing is already under way on this machine.")
        default: return message(f.code, f.detail)
        }
    }
}

struct PairingView: View {
    @EnvironmentObject var m: Model

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.gap) {
            if let failure = m.pairingFailure {
                Banner(tone: .negative, symbol: "exclamationmark.triangle", title: m.pairingMessage(failure)) { EmptyView() }
            }
            if let s = m.pairing {
                let text = m.pairingText(s)
                Text(text.title).font(.title2.weight(.semibold)).foregroundStyle(Theme.text)
                    .accessibilityAddTraits(.isHeader)
                Text(text.body).foregroundStyle(Theme.muted).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                if let code = s.code, s.state == .codeShown || s.state == .passkey {
                    Text(code).font(.system(size: 44, weight: .semibold, design: .monospaced)).kerning(8)
                        .foregroundStyle(Theme.text).textSelection(.enabled)
                }
                if s.state == .passkey, !s.keys.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(s.keys, id: \.kid) { key in
                            Text(key.kid + "  " + key.fingerprint).font(.system(.caption, design: .monospaced))
                                .foregroundStyle(Theme.text).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                if let fingerprint = s.fingerprint, [.codeShown, .passkey, .confirmed].contains(s.state) {
                    Text(m.t("Network fingerprint: {fingerprint}", ["fingerprint": fingerprint]))
                        .font(.callout).foregroundStyle(Theme.muted).textSelection(.enabled)
                }
                if s.state == .passkey, !s.canSign {
                    Text(m.t("Your passkey signs on the hub's page, which is not live yet. Nothing has been signed."))
                        .font(.callout).foregroundStyle(Tone.warning.color).fixedSize(horizontal: false, vertical: true)
                }
                if s.state == .refused, let reason = s.reason {
                    Text(m.t("Reason: {reason}", ["reason": reason])).font(.caption).foregroundStyle(Theme.muted)
                        .textSelection(.enabled)
                }
                HStack(spacing: 10) {
                    ForEach(m.pairingActions(s), id: \.op) { action in
                        if action.prominent {
                            Button(action.label) { Task { await m.pairingStep(action.op, role: action.role) } }
                                .buttonStyle(PrimaryButtonStyle()).disabled(!action.enabled || m.pairingBusy)
                        } else {
                            Button(action.label) { Task { await m.pairingStep(action.op, role: action.role) } }
                                .buttonStyle(SecondaryButtonStyle()).disabled(!action.enabled || m.pairingBusy)
                        }
                    }
                }
                .padding(.top, 8)
            } else {
                ProgressView().controlSize(.small)
            }
            Spacer(minLength: 0)
        }
        .padding(28)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Theme.bg)
        // The wait and the code's time left are the engine's to count: read them again while the window is open.
        .task {
            while !Task.isCancelled {
                await m.pairingStep("status")
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }
}
