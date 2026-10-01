import SwiftUI
import ObservatoryCore

struct Conversation: Identifiable { let id: String; let title: String }
struct Turn: Identifiable {
    let id: String; let question: String; let answer: String; let status: String
    let evidence: [[String: Any]]; let steps: [String]; let error: String?
    let model: String; let cost: Double; let degraded: [[String: Any]]
}
@MainActor final class Model: ObservableObject {
    @Published var executable: String
    @Published var workspace: String
    @Published var russian: Bool
    @Published var conversations: [Conversation] = []
    @Published var turns: [Turn] = []
    @Published var projects: [Conversation] = []
    @Published var selected: String?; @Published var scope = ""
    @Published var question = ""; @Published var error: String?; @Published var version = ""
    @Published var ready = false; @Published var busy = false; @Published var job: String?
    @Published var connecting = false
    private var generation = UUID(), polling: Task<Void, Never>?
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
    func message(_ code: String) -> String {
        switch code {
        case "agent-disabled": return t("Enable the agent in Observatory configuration.", "Включите агента в настройках Observatory.")
        case "provider-unconfigured": return t("Configure a model provider in Observatory first.", "Сначала настройте провайдера модели в Observatory.")
        case "assistant-busy": return t("Another question is running. Open its conversation or try again when it finishes.", "Другой запрос ещё выполняется. Откройте его диалог или дождитесь завершения.")
        case "budget-reached": return t("The configured model budget has been reached.", "Достигнут заданный бюджет модели.")
        case "cancelled": return t("Stopped. Provider usage already incurred may still be charged.", "Остановлено. Уже использованные токены могут быть оплачены.")
        case "interrupted": return t("The runner stopped before an answer was saved. Send a new question to retry.", "Процесс остановился до сохранения ответа. Отправьте новый запрос для повторной попытки.")
        case "provider-failed", "assistant-timeout": return t("The model did not return an answer. Your question is saved; try again.", "Модель не вернула ответ. Вопрос сохранён; попробуйте ещё раз.")
        case "backend-timeout": return t("The backend took too long. Refresh to check whether your request was accepted.", "Сервер не ответил вовремя. Обновите состояние, чтобы проверить, принят ли запрос.")
        default: return t("Could not complete the request", "Не удалось выполнить запрос") + " (\(code)). " + t("Check the executable and workspace in Settings.", "Проверьте программу и папку данных в настройках.")
        }
    }
    private func call(_ action: String, _ input: [String: String] = [:]) async throws -> [String: Any] {
        if let transport { return try await transport(action, input) }
        let data = try await backend.call(action, input: input)
        guard let doc = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw BridgeError.invalidResponse }
        return doc
    }
    func refresh() async {
        let g = generation; connecting = true
        defer { if g == generation { connecting = false } }
        do {
            let doc = try await call("status"); guard g == generation else { return }
            version = doc["engine_version"] as? String ?? ""
            conversations = ((doc["conversations"] as? [[String: Any]]) ?? []).compactMap {
                guard let id = $0["id"] as? String else { return nil }; return Conversation(id: id, title: $0["title"] as? String ?? id)
            }
            projects = ((doc["projects"] as? [[String: Any]]) ?? []).compactMap {
                guard let id = $0["id"] as? String else { return nil }; return Conversation(id: id, title: $0["name"] as? String ?? id)
            }
            ready = doc["agent_enabled"] as? Bool == true && doc["provider_configured"] as? Bool == true
            error = ready ? nil : message(doc["agent_enabled"] as? Bool == true ? "provider-unconfigured" : "agent-disabled")
            if selected == nil { selected = conversations.first?.id }
            if let id = selected { await load(id) }
        } catch { if g == generation { ready = false; self.error = message(error.localizedDescription) } }
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
                    evidence: a["evidence"] as? [[String: Any]] ?? [], steps: a["next_steps"] as? [String] ?? [], error: $0["error"] as? String, model: a["model"] as? String ?? "", cost: a["cost"] as? Double ?? 0, degraded: a["degraded"] as? [[String: Any]] ?? [])
            }
            if let active = rows.first(where: { $0["status"] as? String == "working" }), let jid = active["job_id"] as? String {
                busy = true; job = jid; startPolling(id)
            }
        } catch { if g == generation { self.error = message(error.localizedDescription) } }
    }
    func send() async {
        guard ready, !busy, !question.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        let g = generation; busy = true; error = nil
        var input = ["question": question, "request_id": UUID().uuidString]
        if let selected { input["conversation_id"] = selected }
        if !scope.isEmpty { input["project_id"] = scope }
        do {
            let doc = try await call("ask", input); guard g == generation else { return }
            guard let cid = doc["conversation_id"] as? String, let j = doc["job"] as? [String: Any], let jid = j["id"] as? String else { throw BridgeError.invalidResponse }
            selected = cid; job = jid; question = ""
            await refresh(); startPolling(cid)
        } catch { if g == generation { busy = false; self.error = message(error.localizedDescription) } }
    }
    private func startPolling(_ cid: String) {
        guard polling == nil else { return }
        let g = generation
        polling = Task { [weak self] in
            defer { if let self, g == self.generation { self.polling = nil } }
            while !Task.isCancelled {
                guard let self, g == self.generation, let jid = self.job else { return }
                do {
                    let d = try await self.call("job", ["id": jid]); guard g == self.generation else { return }
                    let status = (d["job"] as? [String: Any])?["status"] as? String
                    if ["completed", "failed", "cancelled"].contains(status ?? "") {
                        self.busy = false; self.job = nil; await self.load(cid); return
                    }
                } catch { if g == self.generation { self.error = self.message(error.localizedDescription) }; return }
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }
    func stop() async {
        guard let job else { return }; let g = generation
        do {
            _ = try await call("cancel", ["id": job]); guard g == generation else { return }
            busy = false; self.job = nil; polling?.cancel(); polling = nil
            if let selected { await load(selected) }
        } catch { if g == generation { self.error = message(error.localizedDescription) } }
    }
    func newConversation() { guard !busy else { return }; selected = nil; turns = []; error = nil }
    func saveSettings() async {
        generation = UUID(); activeExecutable = executable; activeWorkspace = workspace; polling?.cancel(); polling = nil; busy = false; job = nil
        selected = nil; turns = []; conversations = []; ready = false
        defaults.set(executable, forKey: "executable"); defaults.set(workspace, forKey: "workspace")
        defaults.set(russian, forKey: "russian"); await refresh()
    }
}
