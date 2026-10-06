import SwiftUI
import ObservatoryCore
import AppKit

struct Conversation: Identifiable { let id: String; let title: String }
struct Turn: Identifiable {
    let id: String; let question: String; let answer: String; let status: String
    let evidence: [[String: Any]]; let steps: [String]; let error: String?
    let model: String; let cost: Double; let degraded: [[String: Any]]
}
/// What went wrong, as a code and the engine's own words. The sentence is built
/// when it is shown, so switching the language re-words an error on screen.
struct Failure: Equatable { let code: String; let detail: String? }

/// What the main window shows. The built pages are a dashboard in their own right
/// (self-contained, readable with no server), so «no server» is a mode, not a failure.
enum DashboardMode: Equatable {
    case checking
    case live(DashboardOrigin)
    case files(DashboardOrigin, builtAt: Date?, alwaysOn: Bool, portBusy: Bool)
    case notBuilt(portBusy: Bool)
    case unavailable
    var origin: DashboardOrigin? {
        switch self { case .live(let o), .files(let o, _, _, _): return o; default: return nil }
    }
}

@MainActor final class Model: ObservableObject {
    static let questionLimit = 6000           // the engine's, in code points (Python `len`)
    @Published var executable: String
    @Published var workspace: String
    /// The person's choice — System, English or Русский (L10N-01). Persisted, and pinned
    /// for the menus AppKit draws itself, which follow at the next launch.
    @Published var language: AppLanguage {
        didSet {
            guard language != oldValue else { return }
            defaults.set(language.rawValue, forKey: Localization.choiceKey)
            Localization.pinBundleLanguage(language, in: defaults)
            let before = Localization.resolve(oldValue, preferred: preferredLanguages())
            if before != languageCode && !localeFromPage { localeSeed += 1 }
        }
    }
    /// The language the app speaks now: `en` or `ru`.
    var languageCode: String { Localization.resolve(language, preferred: preferredLanguages()) }
    /// Whether the app speaks Russian; setting it chooses that language explicitly.
    var russian: Bool {
        get { languageCode == "ru" }
        set { language = newValue ? .russian : .english }
    }
    private let preferredLanguages: () -> [String]
    /// Set while a language choice made INSIDE the dashboard is being adopted.
    private var localeFromPage = false
    /// The page switched its own language: the app follows, unless it already speaks it.
    func adoptPageLocale(_ code: String) {
        guard ["en", "ru"].contains(code), code != languageCode else { return }
        localeFromPage = true; language = code == "ru" ? .russian : .english; localeFromPage = false
    }
    @Published var conversations: [Conversation] = []
    @Published var turns: [Turn] = []
    @Published var projects: [Conversation] = []
    @Published var selected: String?; @Published var scope = ""
    @Published var question = "" { didSet { if question != oldValue { draftRequest = nil } } }
    @Published var failure: Failure?
    @Published var version = ""; @Published var providerStatus = ""
    @Published var ready = false; @Published var busy = false; @Published var job: String?
    @Published var connecting = false; @Published var connected = false
    @Published var agentEnabled = false; @Published var providerConfigured = false
    /// nil when a model chain and a budget are set; else the engine's `model_status`.
    @Published var modelState: String?
    /// Bumped whenever the transcript's content changes, so the view can follow it.
    @Published var revision = 0
    @Published var savedAt: Date?
    /// The conversation awaiting a delete confirmation, from the menu or a row's context menu.
    @Published var deleting: String?
    @Published var dashboardMode: DashboardMode = .checking
    /// When the pages were last built; the scheduled cycle rebuilds them, and a
    /// change makes the open page reload so the window does not show stale numbers.
    @Published var dashboardBuiltAt: String?
    @Published var dashboardFailure: Failure?
    @Published var dashboardWorking = false
    /// Bumped when the APP's language changes, so the dashboard adopts it once and
    /// a choice made inside the dashboard afterwards is not overwritten on reload.
    @Published var localeSeed = 0
    var dashboardError: String? { dashboardFailure.map { message($0.code, $0.detail) } }
    var error: String? { failure.map { message($0.code, $0.detail) } }

    private var generation = UUID()
    private var polling: Task<Void, Never>?, pollToken: UUID?
    private var sending = false
    /// One request id per draft, until the backend accepts it: a retry after a
    /// timeout replays the same id, and the engine answers with the job it started
    /// instead of spending twice.
    private var draftRequest: String?
    private var activeExecutable: String
    private var activeWorkspace: String
    private let defaults: UserDefaults
    private let transport: ((String, [String: String]) async throws -> [String: Any])?
    init(defaults: UserDefaults = .standard,
         transport: ((String, [String: String]) async throws -> [String: Any])? = nil,
         preferredLanguages: @escaping () -> [String] = Localization.systemPreferredLanguages) {
        self.defaults = defaults; self.transport = transport; self.preferredLanguages = preferredLanguages
        let (cli, home) = Self.configured(defaults)
        executable = cli; activeExecutable = cli; workspace = home; activeWorkspace = home
        // The 0.18 switch is read when no choice is stored; `bool(forKey:)` also reads
        // "YES"/"1" given as a launch argument (a string there).
        language = Localization.choice(stored: defaults.object(forKey: Localization.choiceKey),
                                       legacy: defaults.object(forKey: Localization.legacyKey) != nil ? defaults.bool(forKey: Localization.legacyKey) : nil)
    }
    /// The saved program and workspace, or where a first launch looks for them. The
    /// update check reads the same pair, so it follows the workspace Settings saved.
    nonisolated static func configured(_ defaults: UserDefaults) -> (executable: String, workspace: String) {
        (defaults.string(forKey: "executable") ?? defaultExecutable(home: NSHomeDirectory()),
         defaults.string(forKey: "workspace") ?? NSHomeDirectory() + "/.local/share/project-observatory-full")
    }
    /// Where a first launch looks for the engine, before anything was chosen in
    /// Settings: a link on PATH first, then the virtual environment README → Install
    /// creates, then Homebrew's prefixes. With none present, the documented location —
    /// the message then names a real install step instead of an arbitrary path.
    nonisolated static func defaultExecutable(home: String, exists: (String) -> Bool = { FileManager.default.isExecutableFile(atPath: $0) }) -> String {
        let venv = home + "/.local/share/project-observatory-venv/bin/project-observatory"
        let candidates = [home + "/.local/bin/project-observatory", venv,
                          "/opt/homebrew/bin/project-observatory", "/usr/local/bin/project-observatory"]
        return candidates.first(where: exists) ?? venv
    }
    /// Where a person without an engine learns to install one.
    nonisolated static let installGuide = URL(string: "https://github.com/passioncode-ai/project-observatory-dashboard#install")!
    private var backend: Backend { Backend(executable: activeExecutable, workspace: activeWorkspace) }
    /// The English text in the app's language (L10N-02): the Russian dictionary maps it,
    /// a missing entry shows the English. `{name}` placeholders are filled from `args`.
    func t(_ key: String, _ args: [String: String] = [:]) -> String { Localizer.shared.text(key, language: languageCode, args) }
    /// A sentence about `count` things, in the language's plural form (L10N-03).
    func plural(_ key: String, _ count: Int, _ args: [String: String] = [:]) -> String {
        Localizer.shared.plural(key, count: count, language: languageCode, args)
    }
    /// The app's language as a locale, for dates and numbers (L10N-05).
    var locale: Locale { Locale(identifier: russian ? "ru_RU" : "en_US") }
    /// A date in the APP's language, not the system's: "2 окт. 2026 г., 18:57" beside Russian text.
    func when(_ date: Date) -> String {
        date.formatted(Date.FormatStyle(date: .abbreviated, time: .shortened).locale(locale))
    }

    var questionTooLong: Bool { question.unicodeScalars.count > Self.questionLimit }
    var questionBlank: Bool { question.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    /// Why Send is unavailable, in words; nil when it is available.
    var sendBlocked: String? {
        if !connected { return t("Not connected to an engine.") }
        if !agentEnabled { return t("The agent is turned off in this workspace.") }
        if !providerConfigured { return t("No model provider is configured.") }
        if modelState == "no-budget" { return t("No spending limit is set.") }
        if modelState != nil { return t("No model is chosen.") }
        if questionTooLong { return plural("The question is longer than {limit} characters.", Self.questionLimit, ["limit": String(Self.questionLimit)]) }
        return nil
    }

    func message(_ code: String, _ detail: String? = nil) -> String {
        let extra = detail.map { " (\($0))" } ?? ""
        switch code {
        case "backend-incompatible":
            return t("This Observatory engine is too old for this app — it needs version 0.12 or newer. Update it with `project-observatory full update --apply`, then Refresh.") + extra
        case "backend-missing":
            return t("No Observatory engine was found at {path}. Install it as README → Install describes (a Python 3.11+ virtual environment), then choose its `project-observatory` in Settings.",
                     ["path": detail ?? t("the chosen path")])
        case "backend-configuration":
            return t("Choose the absolute path of the project-observatory program and its workspace folder in Settings.")
        case "backend-failed":
            return t("The engine stopped with an error{detail}. Check the program and workspace in Settings.", ["detail": extra])
        case "backend-timeout": return t("The engine took too long. Refresh to check whether your request was accepted.")
        case "backend-cancelled": return t("The request was stopped before it finished.")
        case "backend-invalid-response", "backend-response-too-large":
            return t("The engine returned an answer this app cannot read. Check that Settings point at a project-observatory program.")
        case "dashboard-port-busy":
            return t("The dashboard port is held by another workspace's server. Stop it, or choose that workspace in Settings.")
        case "dashboard-start-failed":
            return t("The dashboard server did not start. Its log is store/logs/serverd.out in the workspace; `project-observatory full open --serve` shows the reason.")
        case "dashboard-build-failed":
            return t("The dashboard could not be built. Run `project-observatory full local` once; it measures this machine and builds the pages.")
        case "unknown-workspace":
            return t("This folder is not an Observatory workspace yet. Create it with `project-observatory full init` (with OBSERVATORY_HOME set to it), or choose an existing workspace in Settings.")
        case "dashboard-workspace-mismatch", "dashboard-unavailable":
            return t("No running dashboard was verified for this workspace. Start its server with `project-observatory full open --serve`, then try again.")
        case "agent-disabled": return t("The agent is turned off in this workspace. Turn it on with `project-observatory full configure features agent true`, then Refresh.")
        case "provider-unconfigured": return t("No model provider is configured. Install its key from a protected file on stdin: `python \"$(project-observatory full-path)/tools/install_key.py\" --for observatory < KEY_FILE` (see ONBOARDING), then Refresh.") + extra
        case "model-unconfigured": return t("No model is chosen for this workspace. Choose one with `project-observatory full configure model chain MODEL_ID` (an id from your provider's model list), then Refresh.")
        case "budget-unset": return t("No spending limit is set, so nothing is sent. Set all three with `project-observatory full configure budget daily_ceiling 0.50` (and `monthly_ceiling`, `velocity_ceiling`), then Refresh.")
        case "assistant-busy": return t("Another question is running. Open its conversation or wait until it finishes.")
        case "budget-reached": return t("The configured model budget has been reached.")
        case "cancelled": return t("Stopped. Provider usage already incurred may still be charged.")
        case "interrupted", "runner-failed": return t("The runner stopped before an answer was saved. Send the question again.")
        case "provider-failed", "assistant-timeout", "job-failed": return t("The model did not return an answer. Your question is saved; try again.")
        case "invalid-evidence", "invalid-answer": return t("The model's answer cited facts it was not given, so it was refused. Try again.")
        case "conversation-full": return t("This conversation has reached its limit of 32 turns. Start a new conversation.")
        case "history-full": return t("100 conversations are stored. Delete old ones to start another.")
        case "request-history-full": return t("Too many recent requests are still tracked. Try again later.")
        case "conversation-busy": return t("Wait for the answer before deleting this conversation.")
        case "credential-shaped-input": return t("The question looks like it contains a credential. Remove it — keys are never sent to the model.")
        case "invalid-question", "input-too-large": return plural("A question must be 1 to {limit} characters.", Self.questionLimit, ["limit": String(Self.questionLimit)])
        case "unknown-project": return t("That project is no longer in the registry. The scope was reset to all projects.")
        case "unknown-job", "expired-request": return t("That request is no longer tracked. Send the question again.")
        case "unknown-conversation": return t("That conversation no longer exists.")
        case "disk-full": return t("The disk is full, so nothing was sent. Free some space and try again.")
        case "workspace-unwritable", "workspace-write-failed", "OSError": return t("The workspace could not be written. Check its permissions and free space.")
        case "unreadable-history", "linked-history": return t("Stored conversation history could not be read safely; it was left untouched.")
        case "request-id-conflict": return t("That request was already sent with a different question. Send the question again.")
        case "invalid-input", "invalid-conversation-id", "invalid-project-id", "invalid-request-id":
            return t("The engine refused the app's request ({code}). The app and the engine may be from different releases: update both, then Refresh.", ["code": code])
        case "completed": return t("The answer was empty.")
        default: return t("Could not complete the request ({code}).{detail}", ["code": code, "detail": extra])
        }
    }
    /// One evidence limitation in the chosen language; an unknown code keeps the engine's own sentence.
    func limitation(_ d: [String: Any]) -> String {
        let source = d["source"] as? String ?? "", reason = d["reason"] as? String ?? ""
        let shown = (d["shown"] as? NSNumber)?.intValue, total = (d["total"] as? NSNumber)?.intValue
        switch (d["code"] as? String, source) {
        case ("trimmed", "findings"):
            if let shown, let total { return plural("{shown} of {total} findings were included, most severe first.", total, ["shown": String(shown), "total": String(total)]) }
        case ("trimmed", "projects"):
            if let shown, let total { return plural("{shown} of {total} projects were included; choose a project for detail.", total, ["shown": String(shown), "total": String(total)]) }
        case ("freshness-unknown", _): return t("Some snapshots carry no measurement time, so their freshness is unknown.")
        case ("not-measured", "machine"): return t("This machine's disk and memory are not measured yet: run `project-observatory full machine`.")
        case ("unavailable", "machine"): return t("The machine snapshot could not be read.")
        case ("unavailable", "projects.json"): return t("The project registry could not be read.")
        case ("unavailable", "findings.json"): return t("The findings could not be read.")
        case ("unavailable", _): return t("{source} could not be read.", ["source": source])
        case ("relations-unavailable", _): return t("Repository and site links could not be read; only findings naming the project itself are included.")
        default: break
        }
        return "\(source): \(reason)"
    }
    private func failure(of error: Error) -> Failure {
        if let b = error as? BridgeError { return Failure(code: b.code, detail: b.detail) }
        return Failure(code: "backend-failed", detail: error.localizedDescription)
    }
    private func fail(_ error: Error) {
        failure = failure(of: error)
        if failure?.code == "unknown-project" { scope = "" }
    }

    private func call(_ action: String, _ input: [String: String] = [:], timeout: TimeInterval = 20) async throws -> [String: Any] {
        if let transport { return try await transport(action, input) }
        let data = try await backend.call(action, input: input, timeout: timeout)
        guard let doc = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw BridgeError.invalidResponse }
        return doc
    }

    /// Readiness, conversations and projects. Loads the selected conversation once:
    /// a selection it changes is loaded by the view's selection observer instead.
    func refresh() async {
        let g = generation; connecting = true
        defer { if g == generation { connecting = false } }
        do {
            let doc = try await call("status"); guard g == generation else { return }
            apply(status: doc)
            let target = selected.flatMap { id in conversations.contains { $0.id == id } ? id : nil } ?? conversations.first?.id
            if target != selected { selected = target; if target == nil { turns = [] } }
            else if let id = target { await load(id) }
        } catch {
            guard g == generation else { return }
            connected = false; ready = false; version = ""; fail(error)
        }
    }
    private func apply(status doc: [String: Any]) {
        connected = true
        version = doc["engine_version"] as? String ?? ""
        providerStatus = doc["provider_status"] as? String ?? ""
        conversations = ((doc["conversations"] as? [[String: Any]]) ?? []).compactMap {
            guard let id = $0["id"] as? String else { return nil }; return Conversation(id: id, title: $0["title"] as? String ?? id)
        }
        projects = ((doc["projects"] as? [[String: Any]]) ?? []).compactMap {
            guard let id = $0["id"] as? String else { return nil }; return Conversation(id: id, title: $0["name"] as? String ?? id)
        }
        if !scope.isEmpty && !projects.contains(where: { $0.id == scope }) { scope = "" }
        agentEnabled = doc["agent_enabled"] as? Bool == true
        providerConfigured = doc["provider_configured"] as? Bool == true
        // An engine that does not report the model state is judged as before.
        modelState = doc["model_configured"] as? Bool == false ? (doc["model_status"] as? String ?? "no-model") : nil
        ready = agentEnabled && providerConfigured && modelState == nil
        if ready { failure = nil }
        else if !agentEnabled { failure = Failure(code: "agent-disabled", detail: nil) }
        else if !providerConfigured { failure = Failure(code: "provider-unconfigured", detail: providerStatus.isEmpty ? nil : providerStatus) }
        else { failure = Failure(code: modelState == "no-budget" ? "budget-unset" : "model-unconfigured", detail: nil) }
    }

    func load(_ id: String) async {
        let g = generation
        do {
            let doc = try await call("get", ["id": id]); guard g == generation, selected == id else { return }
            let rows = doc["turns"] as? [[String: Any]] ?? []
            turns = rows.map {
                let a = $0["answer"] as? [String: Any] ?? [:]
                return Turn(id: $0["request_id"] as? String ?? UUID().uuidString, question: $0["question"] as? String ?? "",
                    answer: a["answer"] as? String ?? "", status: $0["status"] as? String ?? "failed",
                    evidence: a["evidence"] as? [[String: Any]] ?? [], steps: a["next_steps"] as? [String] ?? [], error: $0["error"] as? String,
                    model: a["model"] as? String ?? "", cost: (a["cost"] as? NSNumber)?.doubleValue ?? 0, degraded: a["degraded"] as? [[String: Any]] ?? [])
            }
            revision += 1
            // `busy` follows the stored rows: a working turn means a job to follow,
            // none means nothing is running — whatever an earlier poll left behind.
            if let active = rows.first(where: { $0["status"] as? String == "working" }), let jid = active["job_id"] as? String {
                busy = true; job = jid; startPolling(id, jid)
            } else if !sending {
                stopPolling(); busy = false; job = nil
            }
        } catch { if g == generation { fail(error) } }
    }

    func send() async {
        guard ready, !busy, !questionBlank, !questionTooLong else { return }
        let g = generation; busy = true; sending = true; failure = nil
        defer { if g == generation { sending = false } }
        let request = draftRequest ?? UUID().uuidString
        draftRequest = request
        var input = ["question": question, "request_id": request]
        if let selected { input["conversation_id"] = selected }
        if !scope.isEmpty { input["project_id"] = scope }
        do {
            let doc = try await call("ask", input); guard g == generation else { return }
            guard let cid = doc["conversation_id"] as? String, let j = doc["job"] as? [String: Any], let jid = j["id"] as? String else { throw BridgeError.invalidResponse }
            question = ""; draftRequest = nil; job = jid
            sending = false
            if selected != cid {
                selected = cid                       // the selection observer loads it
                await refreshList(g)
            } else {
                await refreshList(g); await load(cid)
            }
            startPolling(cid, jid)
        } catch { if g == generation { busy = false; fail(error) } }
    }
    private func refreshList(_ g: UUID) async {
        guard let doc = try? await call("status"), g == generation else { return }
        apply(status: doc)
    }

    private func stopPolling() { polling?.cancel(); polling = nil; pollToken = nil }
    /// One poller per job, owned by a token: a stale poller (after Stop, a new
    /// question or a workspace change) finds the token gone and touches nothing.
    private func startPolling(_ cid: String, _ jid: String) {
        if pollToken != nil, job == jid { return }
        stopPolling()
        let token = UUID(), g = generation
        pollToken = token
        polling = Task { [weak self] in
            var failures = 0
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(failures == 0 ? 2 : min(30, 2 << failures)))
                guard let self, self.pollToken == token, g == self.generation else { return }
                do {
                    let d = try await self.call("job", ["id": jid])
                    guard self.pollToken == token, g == self.generation else { return }
                    failures = 0
                    let status = (d["job"] as? [String: Any])?["status"] as? String
                    if ["completed", "failed", "cancelled"].contains(status ?? "") {
                        self.pollToken = nil; self.polling = nil
                        self.busy = false; self.job = nil
                        await self.load(cid); return
                    }
                } catch {
                    guard self.pollToken == token, g == self.generation else { return }
                    if (error as? BridgeError) == .cancelled { return }
                    failures += 1
                    // A failed poll is retried with backoff; after five in a row the
                    // reason is shown, and Refresh re-derives the state from the rows.
                    if failures >= 5 { self.fail(error); self.pollToken = nil; self.polling = nil; return }
                }
            }
        }
    }

    func stop() async {
        guard let jid = job else { return }; let g = generation
        stopPolling()
        do {
            _ = try await call("cancel", ["id": jid]); guard g == generation else { return }
            busy = false; job = nil
            if let selected { await load(selected) }
        } catch { if g == generation { fail(error); if let selected { await load(selected) } } }
    }

    func delete(_ id: String) async {
        guard !busy else { return }; let g = generation
        do {
            _ = try await call("delete", ["id": id]); guard g == generation else { return }
            if selected == id { selected = nil; turns = [] }
            await refresh()
        } catch { if g == generation { fail(error) } }
    }

    /// Which dashboard this workspace can show now; never starts anything.
    func refreshDashboard() async {
        let g = generation
        do {
            let doc = try await call("dashboard"); guard g == generation else { return }
            // An engine before 0.12 answers in its old shape, without `server`.
            guard doc["server"] is String else {
                throw BridgeError.incompatible(t("engine without the dashboard actions"))
            }
            dashboardMode = Self.mode(doc, workspace: activeWorkspace)
            dashboardBuiltAt = doc["built_at"] as? String
            dashboardFailure = nil
        } catch {
            guard g == generation else { return }
            dashboardMode = .unavailable; dashboardFailure = failure(of: error)
        }
    }
    /// Whether «Start server» can do anything now: saved pages shown, the port free,
    /// nothing running. The banner's button and the Dashboard menu ask this one question.
    var canStartServer: Bool {
        guard !dashboardWorking, case .files(_, _, _, let portBusy) = dashboardMode else { return false }
        return !portBusy
    }
    /// «Start server»: the operator's explicit act, never a side effect of opening the app.
    func startServer() async { await dashboardAction("serve", timeout: 75) }
    /// «Build the dashboard» from the registry — local, no provider call.
    func buildDashboard() async { await dashboardAction("build", timeout: 300) }
    private func dashboardAction(_ action: String, timeout: TimeInterval) async {
        guard !dashboardWorking else { return }
        let g = generation; dashboardWorking = true
        defer { if g == generation { dashboardWorking = false } }
        do {
            let doc = try await call(action, timeout: timeout); guard g == generation else { return }
            dashboardMode = Self.mode(doc, workspace: activeWorkspace); dashboardFailure = nil
        } catch { if g == generation { dashboardFailure = failure(of: error) } }
    }
    nonisolated static func mode(_ doc: [String: Any], workspace: String) -> DashboardMode {
        let busy = doc["server"] as? String == "other-workspace"
        if doc["server"] as? String == "verified", let raw = doc["url"] as? String, let url = URL(string: raw),
           let origin = DashboardOrigin.live(url) { return .live(origin) }
        if let path = doc["files"] as? String, let origin = DashboardOrigin.files(path, workspace: workspace) {
            let built = (doc["built_at"] as? String).flatMap { ISO8601DateFormatter().date(from: $0) }
            return .files(origin, builtAt: built, alwaysOn: doc["always_on"] as? Bool == true, portBusy: busy)
        }
        return .notBuilt(portBusy: busy)
    }
    /// The previous or next conversation in the list (⌥⌘↑ / ⌥⌘↓); never while a question runs.
    func selectAdjacent(_ step: Int) {
        guard !busy, !conversations.isEmpty else { return }
        let at = selected.flatMap { id in conversations.firstIndex { $0.id == id } } ?? (step > 0 ? -1 : conversations.count)
        selected = conversations[max(0, min(conversations.count - 1, at + step))].id
    }
    func newConversation() { guard !busy else { return }; selected = nil; turns = []; failure = nil; revision += 1 }

    /// Saves Settings. Only a change of program or workspace starts over — clearing
    /// the draft, the scope and the transcript, and orphaning any late answer; the
    /// language and a re-check keep the session as it is.
    func saveSettings() async {
        workspace = Self.resolved(workspace); executable = executable.trimmingCharacters(in: .whitespaces)
        let changed = activeExecutable != executable || activeWorkspace != workspace
        defaults.set(executable, forKey: "executable"); defaults.set(workspace, forKey: "workspace")
        if changed {
            question = ""; scope = ""; projects = []; version = ""
            generation = UUID(); activeExecutable = executable; activeWorkspace = workspace
            stopPolling(); busy = false; job = nil; sending = false
            // A build or start for the old workspace may still be running; its end
            // is dropped as stale, so its `dashboardWorking = false` never comes.
            dashboardWorking = false; dashboardFailure = nil
            selected = nil; turns = []; conversations = []; ready = false; connected = false
        }
        savedAt = nil
        if changed { dashboardMode = .checking }
        await refresh()
        await refreshDashboard()
        if connected { savedAt = Date() }
    }
    /// The engine refuses a workspace path through a symbolic link (`/tmp` is one),
    /// so a typed or picked path is saved as its real path. `realpath(3)`, not
    /// `URL.resolvingSymlinksInPath`: that one strips a leading `/private` again,
    /// turning `/private/tmp/ws` back into the `/tmp/ws` the engine refuses. A
    /// folder that does not exist yet resolves through its parent.
    nonisolated static func resolved(_ path: String) -> String {
        let p = path.trimmingCharacters(in: .whitespaces)
        guard p.hasPrefix("/") else { return p }
        func real(_ s: String) -> String? {
            guard let r = realpath(s, nil) else { return nil }
            defer { free(r) }
            return String(cString: r)
        }
        if let r = real(p) { return r }
        let url = URL(fileURLWithPath: p)
        if let parent = real(url.deletingLastPathComponent().path) {
            return (parent as NSString).appendingPathComponent(url.lastPathComponent)
        }
        return p
    }
}
