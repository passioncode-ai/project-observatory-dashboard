import SwiftUI
import ObservatoryCore

struct Conversation: Identifiable { let id: String; let title: String }
struct Turn: Identifiable {
    let id: String; let question: String; let answer: String; let status: String
    let evidence: [[String: Any]]; let steps: [String]; let error: String?
    let model: String; let cost: Double; let degraded: [[String: Any]]
}
/// What went wrong, as a code and the engine's own words. The sentence is built
/// when it is shown, so switching the language re-words an error on screen.
struct Failure: Equatable { let code: String; let detail: String? }

@MainActor final class Model: ObservableObject {
    static let questionLimit = 6000           // the engine's, in code points (Python `len`)
    @Published var executable: String
    @Published var workspace: String
    @Published var russian: Bool { didSet { defaults.set(russian, forKey: "russian") } }
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
    /// Bumped whenever the transcript's content changes, so the view can follow it.
    @Published var revision = 0
    @Published var savedAt: Date?
    /// The conversation awaiting a delete confirmation, from the menu or a row's context menu.
    @Published var deleting: String?
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
         transport: ((String, [String: String]) async throws -> [String: Any])? = nil) {
        self.defaults = defaults; self.transport = transport
        let cli = defaults.string(forKey: "executable") ?? NSHomeDirectory() + "/.local/bin/project-observatory"
        let home = defaults.string(forKey: "workspace") ?? NSHomeDirectory() + "/.local/share/project-observatory-full"
        executable = cli; activeExecutable = cli; workspace = home; activeWorkspace = home
        russian = defaults.object(forKey: "russian") as? Bool ?? Locale.preferredLanguages.first?.hasPrefix("ru") ?? false
    }
    private var backend: Backend { Backend(executable: activeExecutable, workspace: activeWorkspace) }
    func t(_ en: String, _ ru: String) -> String { russian ? ru : en }

    var questionTooLong: Bool { question.unicodeScalars.count > Self.questionLimit }
    var questionBlank: Bool { question.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    /// Why Send is unavailable, in words; nil when it is available.
    var sendBlocked: String? {
        if !connected { return t("Not connected to an engine.", "Нет подключения к движку.") }
        if !agentEnabled { return t("The agent is turned off in this workspace.", "В этой папке данных агент выключен.") }
        if !providerConfigured { return t("No model provider is configured.", "Провайдер модели не настроен.") }
        if questionTooLong { return t("The question is longer than \(Self.questionLimit) characters.", "Вопрос длиннее \(Self.questionLimit) символов.") }
        return nil
    }

    func message(_ code: String, _ detail: String? = nil) -> String {
        let extra = detail.map { " (\($0))" } ?? ""
        switch code {
        case "backend-incompatible":
            return t("This Observatory engine has no assistant — it needs version 0.11 or newer. Update it with `project-observatory full update`, then Refresh.",
                     "В этом движке Observatory нет ассистента — нужна версия 0.11 или новее. Обновите его командой `project-observatory full update` и нажмите «Обновить».") + extra
        case "backend-configuration":
            return t("Choose the absolute path of the project-observatory program and its workspace folder in Settings.",
                     "Укажите в настройках абсолютный путь к программе project-observatory и её папку данных.")
        case "backend-failed":
            return t("The engine stopped with an error", "Движок завершился с ошибкой") + extra + ". " + t("Check the program and workspace in Settings.", "Проверьте программу и папку данных в настройках.")
        case "backend-timeout": return t("The engine took too long. Refresh to check whether your request was accepted.", "Движок не ответил вовремя. Обновите состояние, чтобы проверить, принят ли запрос.")
        case "backend-cancelled": return t("The request was stopped before it finished.", "Запрос остановлен до завершения.")
        case "backend-invalid-response", "backend-response-too-large":
            return t("The engine returned an answer this app cannot read. Check that Settings point at a project-observatory program.", "Движок вернул ответ, который приложение не может прочитать. Проверьте, что в настройках выбрана программа project-observatory.")
        case "unknown-workspace":
            return t("This folder is not an Observatory workspace. Choose the folder `project-observatory full init` created.", "Эта папка не является папкой данных Observatory. Выберите папку, созданную `project-observatory full init`.")
        case "dashboard-workspace-mismatch", "dashboard-unavailable":
            return t("No running dashboard was verified for this workspace. Start its server with `project-observatory full open --serve`, then try again.", "Для этой папки данных не найден подтверждённый дэшборд. Запустите её сервер командой `project-observatory full open --serve` и попробуйте снова.")
        case "agent-disabled": return t("The agent is turned off in this workspace. Turn it on with `project-observatory full configure features agent true`, then Refresh.", "В этой папке данных агент выключен. Включите его командой `project-observatory full configure features agent true` и нажмите «Обновить».")
        case "provider-unconfigured": return t("No model provider is configured. Install a key with `tools/install_key.py --for observatory` (see ONBOARDING), then Refresh.", "Провайдер модели не настроен. Установите ключ через `tools/install_key.py --for observatory` (см. ONBOARDING) и нажмите «Обновить».") + extra
        case "assistant-busy": return t("Another question is running. Open its conversation or wait until it finishes.", "Другой запрос ещё выполняется. Откройте его диалог или дождитесь завершения.")
        case "budget-reached": return t("The configured model budget has been reached.", "Достигнут заданный бюджет модели.")
        case "cancelled": return t("Stopped. Provider usage already incurred may still be charged.", "Остановлено. Уже использованные токены могут быть оплачены.")
        case "interrupted", "runner-failed": return t("The runner stopped before an answer was saved. Send the question again.", "Процесс остановился до сохранения ответа. Отправьте вопрос снова.")
        case "provider-failed", "assistant-timeout", "job-failed": return t("The model did not return an answer. Your question is saved; try again.", "Модель не вернула ответ. Вопрос сохранён; попробуйте ещё раз.")
        case "invalid-evidence", "invalid-answer": return t("The model's answer cited facts it was not given, so it was refused. Try again.", "Ответ модели ссылался на факты, которых ей не давали, поэтому он отклонён. Попробуйте ещё раз.")
        case "conversation-full": return t("This conversation has reached its 32 turns. Start a new conversation.", "В этом диалоге уже 32 хода. Начните новый диалог.")
        case "history-full": return t("100 conversations are stored. Delete old ones to start another.", "Сохранено 100 диалогов. Удалите старые, чтобы начать новый.")
        case "request-history-full": return t("Too many recent requests are still tracked. Try again later.", "Слишком много недавних запросов ещё отслеживается. Попробуйте позже.")
        case "conversation-busy": return t("Wait for the answer before deleting this conversation.", "Дождитесь ответа, прежде чем удалять диалог.")
        case "credential-shaped-input": return t("The question looks like it contains a credential. Remove it — keys are never sent to the model.", "Похоже, в вопросе есть ключ доступа. Удалите его — ключи модели не передаются.")
        case "invalid-question", "input-too-large": return t("A question must be 1 to \(Self.questionLimit) characters.", "Вопрос должен быть от 1 до \(Self.questionLimit) символов.")
        case "unknown-project": return t("That project is no longer in the registry. The scope was reset to all projects.", "Этого проекта больше нет в реестре. Область сброшена на все проекты.")
        case "unknown-job", "expired-request": return t("That request is no longer tracked. Send the question again.", "Этот запрос больше не отслеживается. Отправьте вопрос снова.")
        case "unknown-conversation": return t("That conversation no longer exists.", "Этого диалога больше нет.")
        case "disk-full": return t("The disk is full, so nothing was sent. Free some space and try again.", "Диск заполнен, поэтому ничего не отправлено. Освободите место и попробуйте снова.")
        case "workspace-unwritable", "workspace-write-failed", "OSError": return t("The workspace could not be written. Check its permissions and free space.", "Не удалось записать в папку данных. Проверьте права доступа и свободное место.")
        case "unreadable-history", "linked-history": return t("Stored conversation history could not be read safely; it was left untouched.", "Сохранённую историю не удалось безопасно прочитать; она не изменена.")
        case "completed": return t("The answer was empty.", "Ответ пустой.")
        default: return t("Could not complete the request", "Не удалось выполнить запрос") + " (\(code))." + extra
        }
    }
    /// One evidence limitation in the chosen language; an unknown code keeps the engine's own sentence.
    func limitation(_ d: [String: Any]) -> String {
        let source = d["source"] as? String ?? "", reason = d["reason"] as? String ?? ""
        let shown = (d["shown"] as? NSNumber)?.intValue, total = (d["total"] as? NSNumber)?.intValue
        switch (d["code"] as? String, source) {
        case ("trimmed", "findings"):
            if let shown, let total { return t("\(shown) of \(total) findings were included, most severe first.", "Включено \(shown) из \(total) находок, самые серьёзные первыми.") }
        case ("trimmed", "projects"):
            if let shown, let total { return t("\(shown) of \(total) projects were included; choose a project for detail.", "Включено \(shown) из \(total) проектов; выберите проект для подробностей.") }
        case ("freshness-unknown", _): return t("Some snapshots carry no measurement time, so their freshness is unknown.", "У части снимков нет времени измерения, поэтому их свежесть неизвестна.")
        case ("unavailable", _): return t("\(source) could not be read.", "Не удалось прочитать \(source).")
        case ("relations-unavailable", _): return t("Repository and site links could not be read; only findings naming the project itself are included.", "Связи с репозиториями и сайтами не прочитаны; включены только находки о самом проекте.")
        default: break
        }
        return "\(source): \(reason)"
    }
    private func fail(_ error: Error) {
        if let b = error as? BridgeError { failure = Failure(code: b.code, detail: b.detail) }
        else { failure = Failure(code: "backend-failed", detail: error.localizedDescription) }
        if failure?.code == "unknown-project" { scope = "" }
    }

    private func call(_ action: String, _ input: [String: String] = [:]) async throws -> [String: Any] {
        if let transport { return try await transport(action, input) }
        let data = try await backend.call(action, input: input)
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
        ready = agentEnabled && providerConfigured
        failure = ready ? nil : Failure(code: agentEnabled ? "provider-unconfigured" : "agent-disabled",
                                        detail: agentEnabled && !providerStatus.isEmpty ? providerStatus : nil)
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

    func dashboardURL() async -> URL? {
        let g = generation
        do {
            let doc = try await call("dashboard"); guard g == generation else { return nil }
            guard let raw = doc["url"] as? String, let url = URL(string: raw),
                  url.scheme == "http", url.host == "127.0.0.1", url.user == nil, url.password == nil,
                  url.path == "/dashboard/index.html" else { throw BridgeError.invalidResponse }
            return url
        } catch { if g == generation { fail(error) }; return nil }
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
            selected = nil; turns = []; conversations = []; ready = false; connected = false
        }
        savedAt = nil
        await refresh()
        if connected { savedAt = Date() }
    }
    /// The engine refuses a workspace path through a symbolic link (`/tmp` is one);
    /// a typed path is resolved the way the folder picker resolves a chosen one.
    static func resolved(_ path: String) -> String {
        let p = path.trimmingCharacters(in: .whitespaces)
        guard p.hasPrefix("/") else { return p }
        return URL(fileURLWithPath: p).resolvingSymlinksInPath().path
    }
}
