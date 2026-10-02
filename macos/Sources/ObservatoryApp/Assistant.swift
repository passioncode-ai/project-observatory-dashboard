import SwiftUI
import AppKit

enum Design { static let gap: CGFloat = 16; static let transcript: CGFloat = 760 }

func rendered(_ text: String) -> AttributedString {
    let bulleted = text.split(separator: "\n", omittingEmptySubsequences: false).map { line -> String in
        let t = line.drop(while: { $0 == " " })
        return (t.hasPrefix("- ") || t.hasPrefix("* ")) ? String(line.prefix(line.count - t.count)) + "• " + t.dropFirst(2) : String(line)
    }.joined(separator: "\n")
    var out = (try? AttributedString(markdown: bulleted, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(text)
    for run in out.runs where run.link != nil { out[run.range].link = nil }
    return out
}
func money(_ cost: Double) -> String { "$" + String(format: "%.4f", cost) }    // not the locale's comma

struct AssistantView: View {
    @EnvironmentObject var m: Model
    @Environment(\.openWindow) private var openWindow
    var deletingTitle: String { m.conversations.first { $0.id == m.deleting }?.title ?? "" }
    var statusLine: String {
        if !m.connected { return m.t("Not connected", "Нет подключения") }
        return "Observatory \(m.version) · " + (m.ready ? m.t("ready", "готов") : m.t("setup needed", "нужна настройка"))
    }
    var body: some View {
        NavigationSplitView {
            List(selection: $m.selected) {
                Section(m.t("Conversations", "Диалоги")) {
                    ForEach(m.conversations) { c in
                        Text(c.title).lineLimit(2).tag(c.id)
                            .contextMenu { Button(m.t("Delete Conversation…", "Удалить диалог…"), role: .destructive) { m.deleting = c.id } }
                    }
                }
            }.disabled(m.busy).navigationSplitViewColumnWidth(min: 200, ideal: 245, max: 320)
            .safeAreaInset(edge: .bottom) {
                HStack {
                    HStack {
                        Image(systemName: m.ready ? "checkmark.circle.fill" : m.connected ? "exclamationmark.circle" : "xmark.circle")
                            .foregroundStyle(m.ready ? .green : .secondary).accessibilityHidden(true)
                        Text(statusLine).font(.caption)
                    }.accessibilityElement(children: .combine)
                    Spacer(); SettingsLink { Image(systemName: "gearshape") }.accessibilityLabel(m.t("Settings", "Настройки"))
                }.padding()
            }
        } detail: {
            VStack(spacing: 0) {
                if let error = m.error {
                    HStack(alignment: .top) {
                        Image(systemName: "exclamationmark.triangle").accessibilityHidden(true)
                        Text(rendered(error)).textSelection(.enabled); Spacer()
                        Button(m.t("Retry", "Повторить")) { Task { await m.refresh() } }.disabled(m.connecting)
                        SettingsLink { Text(m.t("Settings", "Настройки")) }
                    }.font(.callout).padding().background(.orange.opacity(0.10))
                }
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 24) {
                            if m.turns.isEmpty { welcome }
                            ForEach(m.turns) { turn in turnView(turn).id(turn.id) }
                            Color.clear.frame(height: 1).id("end")
                        }.frame(maxWidth: Design.transcript, alignment: .leading).padding(24).frame(maxWidth: .infinity)
                    }
                    // Follows every change of content, not only a new turn: an answer
                    // arriving into an existing turn used to stay below the fold.
                    .onChange(of: m.revision) { _, _ in withAnimation(nil) { proxy.scrollTo("end", anchor: .bottom) } }
                }
                Divider()
                composer
            }.navigationTitle(m.t("Assistant", "Ассистент"))
        }.toolbar {
            ToolbarItemGroup {
                Button { m.newConversation() } label: { Label(m.t("New conversation", "Новый диалог"), systemImage: "square.and.pencil") }
                    .disabled(m.busy).help(m.t("New conversation (⌘N)", "Новый диалог (⌘N)"))
                Button { Task { await m.refresh() } } label: { Label(m.t("Refresh", "Обновить"), systemImage: "arrow.clockwise") }
                    .disabled(m.connecting).help(m.t("Refresh (⌘R)", "Обновить (⌘R)"))
                Button { openWindow(id: WindowID.dashboard) } label: { Label(m.t("Dashboard", "Дашборд"), systemImage: "rectangle.grid.2x2") }
                    .help(m.t("Show the dashboard window (⌘1)", "Показать окно дашборда (⌘1)"))
            }
        }.task { await m.refresh() }
        .onChange(of: m.selected) { _, id in if let id { Task { await m.load(id) } } }
        .confirmationDialog(m.t("Delete “\(deletingTitle)”?", "Удалить «\(deletingTitle)»?"), isPresented: Binding(get: { m.deleting != nil }, set: { if !$0 { m.deleting = nil } })) {
            Button(m.t("Delete", "Удалить"), role: .destructive) { if let id = m.deleting { Task { await m.delete(id) } }; m.deleting = nil }
        } message: { Text(m.t("Its questions and answers are removed from this workspace. This cannot be undone.", "Его вопросы и ответы будут удалены из этой папки данных. Отменить это нельзя.")) }
    }
    var composer: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Picker(m.t("Scope", "Область"), selection: $m.scope) {
                    Text(m.t("All projects", "Все проекты")).tag("")
                    ForEach(m.projects) { Text($0.title).tag($0.id) }
                }.labelsHidden().frame(maxWidth: 300).disabled(m.busy).accessibilityLabel(m.t("Scope", "Область"))
                Spacer()
                if m.busy { ProgressView().controlSize(.small); Text(m.t("Working…", "Выполняется…")).font(.caption) }
                else if let why = m.sendBlocked { Text(why).font(.caption).foregroundStyle(.secondary) }
                if m.question.unicodeScalars.count > Model.questionLimit - 500 {
                    Text("\(m.question.unicodeScalars.count)/\(Model.questionLimit)").font(.caption.monospacedDigit())
                        .foregroundStyle(m.questionTooLong ? .red : .secondary)
                }
            }
            HStack(alignment: .bottom) {
                TextField(m.t("Ask about your projects…", "Спросите о проектах…"), text: $m.question, axis: .vertical)
                    .lineLimit(2...5).textFieldStyle(.roundedBorder).disabled(m.busy)
                    .accessibilityLabel(m.t("Question", "Вопрос"))
                if m.busy {
                    Button(m.t("Stop", "Остановить"), systemImage: "stop.fill") { Task { await m.stop() } }.disabled(m.job == nil)
                } else {
                    Button(m.t("Send", "Отправить"), systemImage: "arrow.up") { Task { await m.send() } }
                        .keyboardShortcut(.return, modifiers: .command).buttonStyle(.borderedProminent)
                        .disabled(!m.ready || m.questionBlank || m.questionTooLong)
                        .help(m.sendBlocked ?? m.t("Send (⌘↩)", "Отправить (⌘↩)"))
                }
            }
            Text(m.t("Sending shares your question and selected local facts with your configured model. Model costs apply. Answers suggest actions; they do not run them.", "При отправке вопрос и выбранные локальные факты передаются настроенной модели. Применяются её тарифы. Ответы предлагают действия, но не выполняют их."))
                .font(.caption).foregroundStyle(.secondary)
        }.padding(Design.gap)
    }
    var welcome: some View {
        VStack(alignment: .leading, spacing: 18) {
            Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 56, height: 56).accessibilityHidden(true)
            Text(m.t("What is happening across your projects?", "Что происходит с вашими проектами?")).font(.largeTitle).bold()
            Text(m.t("Ask Observatory to explain its latest local snapshots. Every answer can show the facts it used. Missing or old data remains visible.", "Попросите Observatory объяснить последние локальные данные. У ответа можно раскрыть использованные факты. Отсутствующие и старые данные не скрываются.")).foregroundStyle(.secondary)
            ForEach([m.t("Which projects need attention?", "Какие проекты требуют внимания?"), m.t("How much disk space is available?", "Сколько места осталось на диске?")], id: \.self) { q in
                Button(q) { m.question = q }.buttonStyle(.bordered).disabled(m.busy)
            }
            if !m.connected { SettingsLink { Text(m.t("Connect Observatory", "Подключить Observatory")) }.buttonStyle(.borderedProminent) }
        }.padding(.vertical, 36)
    }
    func turnView(_ t: Turn) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(t.question).font(.title3).bold().textSelection(.enabled)
            if !t.answer.isEmpty {
                Text(rendered(t.answer)).textSelection(.enabled)
                Text("\(t.model) · \(money(t.cost))").font(.caption).foregroundStyle(.secondary)
                if !t.degraded.isEmpty {
                    Text(m.t("Some evidence is unavailable or limited:", "Часть данных недоступна или ограничена:")).font(.callout).bold()
                    ForEach(Array(t.degraded.enumerated()), id: \.offset) { _, d in
                        Text(m.limitation(d)).font(.caption).foregroundStyle(.secondary)
                    }
                }
                ForEach(Array(t.steps.enumerated()), id: \.offset) { _, step in Label { Text(rendered(step)) } icon: { Image(systemName: "arrow.right") }.textSelection(.enabled) }
                if !t.evidence.isEmpty {
                    DisclosureGroup(m.t("Sources", "Источники") + " (\(t.evidence.count))") {
                        ForEach(Array(t.evidence.enumerated()), id: \.offset) { _, e in
                            VStack(alignment: .leading, spacing: 4) {
                                // The id the answer cites, so "(E17)" in the text leads somewhere.
                                Text("\(e["id"] as? String ?? "") · \(e["title"] as? String ?? "")").bold()
                                Text("\(e["source"] as? String ?? "") · " + ((e["measured_at"] as? String) ?? m.t("measurement time unknown", "время измерения неизвестно")))
                                    .font(.caption).foregroundStyle(.secondary)
                                if let facts = e["facts"] as? [String: Any] {
                                    ForEach(facts.keys.sorted(), id: \.self) { key in Text("\(key): \(describe(facts[key]))").font(.callout).textSelection(.enabled) }
                                }
                            }.padding(.vertical, 6).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }.padding().background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
                }
            } else if t.status == "working" { Text(m.t("Reading evidence and preparing an answer…", "Читаю факты и готовлю ответ…")).foregroundStyle(.secondary) }
            else { Text(rendered(m.message(t.error ?? t.status))).foregroundStyle(.secondary) }
            Divider()
        }
    }
    func describe(_ value: Any?) -> String {
        switch value {
        case let s as String: return s
        case let n as NSNumber: return n.stringValue
        case let rows as [[String: Any]]:
            // A row reads by its name first: "OrbStack VM disk · 24.2", not "gb 24.2, label …".
            let first = ["label", "name", "title", "id"]
            return rows.map { r in
                let named = first.first { r[$0] != nil }
                let rest = r.keys.filter { $0 != named }.sorted().map { "\($0) \(describe(r[$0]))" }
                return ([named.map { describe(r[$0]) }].compactMap { $0 } + rest).joined(separator: " · ")
            }.joined(separator: "; ")
        case let list as [Any]: return list.map { describe($0) }.joined(separator: ", ")
        case is NSNull, nil: return "—"
        default: return String(describing: value!)
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject var m: Model
    var body: some View {
        Form {
            Text(m.t("Connect to your local Observatory engine. Choose a compatible installed CLI and its private workspace.", "Подключитесь к локальному движку Observatory. Выберите совместимую установленную CLI-программу и её папку данных."))
            HStack { TextField(m.t("Executable", "Программа"), text: $m.executable); Button(m.t("Choose…", "Выбрать…")) { choose(false) } }
            HStack { TextField(m.t("Workspace", "Папка данных"), text: $m.workspace); Button(m.t("Choose…", "Выбрать…")) { choose(true) } }
            Toggle("Русский", isOn: $m.russian)
            HStack {
                Button(m.t("Save and check connection", "Сохранить и проверить")) { Task { await m.saveSettings() } }.disabled(m.connecting)
                if m.connecting { ProgressView().controlSize(.small); Text(m.t("Checking…", "Проверяю…")).font(.callout) }
            }
            // What the check found, in one place: engine, protocol, readiness.
            if m.connected {
                Label(m.t("Connected to Observatory \(m.version) (observatory-assistant/1).", "Подключено к Observatory \(m.version) (observatory-assistant/1)."), systemImage: "checkmark.circle")
                Text(m.ready ? m.t("The assistant is ready.", "Ассистент готов.") : (m.error ?? "")).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                if let at = m.savedAt { Text(m.t("Saved", "Сохранено") + " " + at.formatted(Date.FormatStyle(date: .omitted, time: .standard).locale(Locale(identifier: m.russian ? "ru_RU" : "en_US")))).font(.caption).foregroundStyle(.secondary) }
            } else if let error = m.error {
                Label { Text(rendered(error)).textSelection(.enabled) } icon: { Image(systemName: "xmark.circle") }.font(.callout)
            }
            Text(m.t("Changing the executable or workspace clears the draft and project scope. Accepted jobs keep running in their original workspace; return there to stop or read them.", "Смена программы или папки данных очищает черновик и выбор проекта. Принятые задания продолжают работу в прежней папке; вернитесь к ней для остановки или чтения результата.")).font(.caption).foregroundStyle(.secondary)
        }.padding(24).frame(width: 600)
    }
    func choose(_ directory: Bool) {
        let panel = NSOpenPanel(); panel.canChooseDirectories = directory; panel.canChooseFiles = !directory
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            if directory { m.workspace = url.resolvingSymlinksInPath().path } else { m.executable = url.path }
        }
    }
}
