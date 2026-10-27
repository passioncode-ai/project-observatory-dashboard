import SwiftUI
import AppKit
import ObservatoryCore

/// The native secret prompt (OBS-260, scenario OSS-84, docs/design/PERSON-SURFACES.md D4). The node files an agent's
/// request and asks its lifecycle broker to open this app; the app reads the waiting requests through its own child
/// (`full assistant secret`), shows this window with the person's phrase, and its child writes the value into the
/// vault slot. The value goes only into that child's stdin: never an argument, the environment, a log, a
/// notification or anything the agent reads.
extension Model {
    /// How often the app asks its door whether a request waits. The node opens the app through the broker; an app
    /// that already runs learns of a new request at the next read.
    static let secretPollSeconds: UInt64 = 5

    /// Started once, from the dashboard's first appearance; it outlives the window, so a closed dashboard still
    /// brings a prompt forward.
    func startSecretWatch() {
        guard secretWatch == nil else { return }
        secretWatch = Task { [weak self] in
            while !Task.isCancelled {
                await self?.readSecretRequests()
                try? await Task.sleep(nanoseconds: Self.secretPollSeconds * 1_000_000_000)
            }
        }
    }

    /// One read of the queue, through the bundled engine only (`secretCall`): with none, or a signature that does not
    /// check out, nothing is read and nothing is shown (the node already answered `engine-untrusted`). A request
    /// appears: with no place for the phrase it is answered `no-client` at once and no window is shown; otherwise the
    /// window comes forward. A request that is gone from the queue (withdrawn, answered elsewhere, past its ten
    /// minutes) takes the window down.
    func readSecretRequests() async {
        guard let input = SecretRequest.input(op: "list"),
              let answer = try? await secretCall(input), let requests = SecretRequest.list(answer: answer) else { return }
        let stage = SecretPromptStage.of(phraseStore)
        secretStage = stage
        let refusal = stage == .unavailable ? "phrase-store-unavailable" : nil
        if let refusal {
            for r in requests {
                if let refuse = SecretRequest.input(op: "unavailable", requestId: r.requestId, reason: refusal) {
                    _ = try? await secretCall(refuse)
                }
            }
            secretRequests = []
            return
        }
        let appeared = requests.contains { r in !secretRequests.contains(where: { $0.requestId == r.requestId }) }
        secretRequests = requests
        if appeared, let open = ApplicationDelegate.openWindow {
            open(id: WindowID.secret)
            NSApp.activate()
        }
    }

    /// Save or Cancel for the request on screen. The value is handed to the child and dropped here.
    func answerSecret(_ request: SecretRequest, value: String?) async {
        let input = value.map { SecretRequest.input(op: "store", requestId: request.requestId, value: $0) }
            ?? SecretRequest.input(op: "decline", requestId: request.requestId)
        guard let input, !secretBusy else { return }
        secretBusy = true
        defer { secretBusy = false }
        do {
            _ = try await secretCall(input)
            secretFailure = nil
            secretRequests.removeAll { $0.requestId == request.requestId }
        } catch {
            secretFailure = failureOf(error)
            if secretFailure?.code == "request-closed" {
                secretRequests.removeAll { $0.requestId == request.requestId }
            }
        }
    }

    /// Set or change the phrase; false when it is not a phrase or this Mac cannot keep it.
    @discardableResult
    func savePhrase(_ text: String) -> Bool {
        guard let phrase = Phrase.normalized(text) else { return false }
        do {
            try phraseStore.write(phrase)
            secretStage = .prompt(phrase: phrase)
            return true
        } catch {
            secretStage = .unavailable
            return false
        }
    }

    func secretMessage(_ f: Failure) -> String {
        switch f.code {
        case "client-unverified": return t("Only Observatory's own signed app can answer a secret request.")
        case "request-closed": return t("This request is no longer waiting.")
        case "engine-untrusted": return t("Observatory takes a key only through the engine inside its own signed app, and this app has none yet. Store a key yourself with vault.py put in your own shell.")
        case "value-empty": return t("The key is empty. Nothing was saved.")
        case "value-multiline": return t("The key must be one line.")
        case "value-unchanged": return t("This is the value the slot already holds. Nothing was saved.")
        case "slot-filled-now": return t("The slot was filled after the agent asked. Nothing was saved; the agent can ask again to replace it.")
        case "not-stored": return t("The key was not saved. Check that it is one line, then try again.")
        default: return message(f.code, f.detail)
        }
    }
}

/// Keeps the window out of screen sharing and capture that honours `NSWindow.sharingType` (OBS-260, M2 of the review
/// of #382): the phrase is the one thing an imitation would need.
struct UnsharedWindow: NSViewRepresentable {
    final class Marker: NSView {
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            window?.sharingType = .none
        }
    }
    func makeNSView(context: Context) -> NSView { Marker() }
    func updateNSView(_ view: NSView, context: Context) { view.window?.sharingType = .none }
}

struct SecretPromptView: View {
    @EnvironmentObject var m: Model
    @Environment(\.dismiss) private var dismiss
    @State private var value = ""
    @State private var phrase = ""
    @State private var phraseRefused = false
    @State private var shownId: String?
    @FocusState private var field: String?

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.gap) {
            if let failure = m.secretFailure {
                Banner(tone: .negative, symbol: "exclamationmark.triangle", title: m.secretMessage(failure)) { EmptyView() }
            }
            switch m.secretStage {
            case .unavailable:
                Text(m.t("This release of Project Observatory cannot keep your phrase yet, so it shows no secret prompt. Store a key yourself with vault.py put in your own shell."))
                    .foregroundStyle(Theme.muted).fixedSize(horizontal: false, vertical: true)
            case .needsPhrase:
                phraseSetup
            case .prompt(let shown):
                if let request = m.secretRequests.first {
                    prompt(request, phrase: shown)
                } else {
                    Text(m.t("This request is no longer waiting.")).foregroundStyle(Theme.muted)
                    Button(m.t("Close")) { dismiss() }.buttonStyle(SecondaryButtonStyle())
                }
            }
            Spacer(minLength: 0)
        }
        .padding(28)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Theme.bg)
        .background(UnsharedWindow())
        // A new request never inherits what was typed for another; a withdrawn one takes the window down.
        .onChange(of: m.secretRequests.first?.requestId) { _, now in
            value = ""
            if shownId != nil, now == nil { dismiss() }
            shownId = now
        }
        .onAppear { shownId = m.secretRequests.first?.requestId }
        // The value never outlives the window.
        .onDisappear { value = "" }
    }

    private var phraseSetup: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(m.t("Set your phrase")).font(.title2.weight(.semibold)).foregroundStyle(Theme.text)
                .accessibilityAddTraits(.isHeader)
            Text(m.t("Choose a short phrase only you know. Every genuine secret prompt shows it, so a window without it is not Observatory's."))
                .foregroundStyle(Theme.muted).fixedSize(horizontal: false, vertical: true)
            SecureField(m.t("Your phrase"), text: $phrase).focused($field, equals: "phrase").focusEffectDisabled()
                .fieldChrome(focused: field == "phrase").accessibilityLabel(m.t("Your phrase"))
            if phraseRefused {
                Text(m.t("A phrase is 3 to 64 characters on one line.")).font(.callout).foregroundStyle(Tone.warning.color)
            }
            Button(m.t("Save phrase")) { phraseRefused = !m.savePhrase(phrase); if !phraseRefused { phrase = "" } }
                .buttonStyle(PrimaryButtonStyle())
        }
    }

    private func prompt(_ r: SecretRequest, phrase: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            // The request's text scrolls; the phrase, the field and the buttons stay on screen.
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text(m.t("{agent} asks for a key", ["agent": r.agent])).font(.title2.weight(.semibold))
                        .foregroundStyle(Theme.text).accessibilityAddTraits(.isHeader)
                    Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 6) {
                        GridRow { label(m.t("Project")); Text(r.project).foregroundStyle(Theme.text).textSelection(.enabled) }
                        GridRow { label(m.t("Slot")); Text(r.env + " / " + r.name).foregroundStyle(Theme.text).textSelection(.enabled) }
                        if let provider = r.provider {
                            GridRow { label(m.t("Provider")); Text(provider).foregroundStyle(Theme.text) }
                        }
                        // The agent's own words, quoted: never the app's sentence.
                        GridRow { label(m.t("Why")); Text("“" + r.reason + "”").foregroundStyle(Theme.text).fixedSize(horizontal: false, vertical: true) }
                    }
                    if r.replaces {
                        Text(m.t("This slot already holds a value, last changed {when}. Saving replaces it; the old value is archived.",
                                 ["when": r.lastChange ?? "?"]))
                            .font(.callout).foregroundStyle(Tone.warning.color).fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 220)
            // The phrase is drawn for the eye only: it is out of the accessibility tree, and the line below carries a
            // neutral label in its place.
            Text(m.t("Your phrase: “{phrase}”", ["phrase": phrase])).font(.callout.weight(.semibold)).foregroundStyle(Tone.info.color)
                .accessibilityHidden(true)
            Text(m.t("If this window does not show your phrase, it is not Observatory. Cancel it."))
                .font(.caption).foregroundStyle(Theme.muted).fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel(m.t("Your phrase is shown on screen") + ". " + m.t("If this window does not show your phrase, it is not Observatory. Cancel it."))
            SecureField(m.t("Paste the key"), text: $value).focused($field, equals: "value").focusEffectDisabled()
                .fieldChrome(focused: field == "value").accessibilityLabel(m.t("Paste the key"))
            if !value.isEmpty, SecretValue.problem(value) == .lineBreak {
                Text(m.t("The key must be one line.")).font(.callout).foregroundStyle(Tone.warning.color)
            }
            Text(m.t("It is stored in the vault, not in the agent's chat.")).font(.caption).foregroundStyle(Theme.muted)
            // No default action: Return in the field saves nothing, Save takes a deliberate click.
            HStack(spacing: 10) {
                Button(m.t("Cancel")) { let shown = r; value = ""; Task { await m.answerSecret(shown, value: nil) } }
                    .buttonStyle(SecondaryButtonStyle()).disabled(m.secretBusy)
                Button(r.replaces ? m.t("Replace") : m.t("Save")) {
                    let shown = r, typed = value
                    value = ""
                    Task { await m.answerSecret(shown, value: typed) }
                }
                .buttonStyle(PrimaryButtonStyle())
                .disabled(m.secretBusy || SecretValue.problem(value) != nil)
            }
            .padding(.top, 8)
        }
    }

    private func label(_ text: String) -> some View {
        Text(text).font(.callout).foregroundStyle(Theme.muted)
    }
}

/// The phrase in Settings: set or changed here, never shown in a tool result.
struct PhraseSettings: View {
    @EnvironmentObject var m: Model
    @State private var phrase = ""
    @State private var refused = false
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(m.t("Secret prompt phrase")).font(.callout.weight(.semibold)).foregroundStyle(Theme.text)
            switch SecretPromptStage.of(m.phraseStore) {
            case .unavailable:
                Text(m.t("This release of Project Observatory cannot keep your phrase yet, so it shows no secret prompt. Store a key yourself with vault.py put in your own shell."))
                    .font(.caption).foregroundStyle(Theme.muted).fixedSize(horizontal: false, vertical: true)
            case .needsPhrase, .prompt:
                HStack(spacing: 10) {
                    SecureField(m.t("Your phrase"), text: $phrase).focused($focused).focusEffectDisabled()
                        .fieldChrome(focused: focused).accessibilityLabel(m.t("Your phrase"))
                    Button(m.t("Save phrase")) { refused = !m.savePhrase(phrase); if !refused { phrase = "" } }
                        .buttonStyle(SecondaryButtonStyle())
                }
                Text(refused ? m.t("A phrase is 3 to 64 characters on one line.")
                     : m.t("Every genuine secret prompt shows this phrase. It is kept in this app's own keychain group, which other programs cannot read."))
                    .font(.caption).foregroundStyle(Theme.muted).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}
