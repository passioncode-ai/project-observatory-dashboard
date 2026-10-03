import SwiftUI
import AppKit

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
    @FocusState private var composing: Bool
    var deletingTitle: String { m.conversations.first { $0.id == m.deleting }?.title ?? "" }
    var statusLine: String {
        if !m.connected { return m.t("Not connected", "Нет подключения") }
        return "Observatory \(m.version) · " + (m.ready ? m.t("ready", "готов") : m.t("setup needed", "нужна настройка"))
    }
    /// Ready is positive; connected but not ready needs a person (warning); no engine is a failure.
    var statusTone: Tone { m.ready ? .positive : m.connected ? .warning : .negative }
    var scopeTitle: String { m.projects.first { $0.id == m.scope }?.title ?? m.t("All projects", "Все проекты") }

    var body: some View {
        NavigationSplitView {
            // Rows draw their own selection — the accent-soft fill and a 2 px gold bar —
            // because a List's native highlight is the user's system accent (blue by
            // default) once the list has focus, and white text on gold fails contrast.
            List {
                Section {
                    ForEach(m.conversations) { c in
                        let on = m.selected == c.id
                        Button { m.selected = c.id } label: {
                            Text(c.title).lineLimit(2).foregroundStyle(Theme.text)
                                .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4).padding(.leading, 6)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .listRowBackground(RoundedRectangle(cornerRadius: Theme.radiusControl).fill(on ? Theme.accentSoft : Color.clear)
                            .overlay(alignment: .leading) { if on { Rectangle().fill(Theme.accent).frame(width: 2) } }
                            .padding(.horizontal, 6))
                        .accessibilityAddTraits(on ? .isSelected : [])
                        .contextMenu { Button(m.t("Delete Conversation…", "Удалить диалог…"), role: .destructive) { m.deleting = c.id } }
                    }
                } header: {
                    Text(m.t("Conversations", "Диалоги")).font(.caption.weight(.semibold)).foregroundStyle(Theme.muted)
                }
            }
            .scrollContentBackground(.hidden).background(Theme.panel)
            .disabled(m.busy).navigationSplitViewColumnWidth(min: 200, ideal: 245, max: 320)
            .safeAreaInset(edge: .bottom) {
                HStack {
                    HStack(spacing: 6) {
                        Image(systemName: m.ready ? "checkmark.circle.fill" : m.connected ? "exclamationmark.circle" : "xmark.circle")
                            .foregroundStyle(statusTone.color).accessibilityHidden(true)
                        Text(statusLine).font(.caption).foregroundStyle(Theme.text)
                    }.accessibilityElement(children: .combine)
                    Spacer()
                    SettingsLink { Image(systemName: "gearshape") }.buttonStyle(SecondaryButtonStyle())
                        .accessibilityLabel(m.t("Settings", "Настройки"))
                }
                .padding(12).background(Theme.panel)
                .overlay(alignment: .top) { Rectangle().fill(Theme.border).frame(height: 1) }
            }
        } detail: {
            VStack(spacing: 0) {
                if let error = m.error {
                    Banner(tone: Self.tone(m.failure?.code), symbol: "exclamationmark.triangle", title: error) {
                        // The messages say "then Refresh": the button carries that word.
                        Button(m.t("Refresh", "Обновить")) { Task { await m.refresh() } }
                            .buttonStyle(SecondaryButtonStyle()).disabled(m.connecting)
                        SettingsLink { Text(m.t("Settings", "Настройки")) }.buttonStyle(SecondaryButtonStyle())
                    }
                }
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 24) {
                            if m.turns.isEmpty { welcome }
                            ForEach(m.turns) { turn in turnView(turn).id(turn.id) }
                            Color.clear.frame(height: 1).id("end")
                        }.frame(maxWidth: Theme.transcript, alignment: .leading).padding(24).frame(maxWidth: .infinity)
                    }
                    // Follows every change of content, not only a new turn: an answer
                    // arriving into an existing turn used to stay below the fold.
                    .onChange(of: m.revision) { _, _ in withAnimation(nil) { proxy.scrollTo("end", anchor: .bottom) } }
                }
                composer
            }
            .background(Theme.bg)
            .navigationTitle(m.t("Assistant", "Ассистент"))
        }.toolbar {
            ToolbarItemGroup {
                Button { m.newConversation() } label: { Label(m.t("New conversation", "Новый диалог"), systemImage: "square.and.pencil") }
                    .disabled(m.busy).help(m.t("New conversation (⌘N)", "Новый диалог (⌘N)"))
                Button { Task { await m.refresh() } } label: { Label(m.t("Refresh", "Обновить"), systemImage: "arrow.clockwise") }
                    .disabled(m.connecting).help(m.t("Refresh the connection and conversations", "Обновить подключение и диалоги"))
                Button { openWindow(id: WindowID.dashboard) } label: { Label(m.t("Dashboard", "Дашборд"), systemImage: "rectangle.grid.2x2") }
                    .help(m.t("Show the dashboard window (⌘1)", "Показать окно дашборда (⌘1)"))
            }
        }
        .passionCodeWindow()
        .task { await m.refresh() }
        .onChange(of: m.selected) { _, id in if let id { Task { await m.load(id) } } }
        .confirmationDialog(m.t("Delete “\(deletingTitle)”?", "Удалить «\(deletingTitle)»?"), isPresented: Binding(get: { m.deleting != nil }, set: { if !$0 { m.deleting = nil } })) {
            Button(m.t("Delete", "Удалить"), role: .destructive) { if let id = m.deleting { Task { await m.delete(id) } }; m.deleting = nil }
        } message: { Text(m.t("Its questions and answers are removed from this workspace. This cannot be undone.", "Его вопросы и ответы будут удалены из этой папки данных. Отменить это нельзя.")) }
    }
    /// Setup a person must do is a warning; anything that broke is negative.
    static func tone(_ code: String?) -> Tone {
        ["agent-disabled", "provider-unconfigured", "model-unconfigured", "budget-unset", "unknown-workspace", "backend-missing", "backend-configuration",
         "backend-incompatible", "budget-reached", "assistant-busy", "conversation-full", "history-full"].contains(code ?? "") ? .warning : .negative
    }
    var composer: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Menu {
                    Button(m.t("All projects", "Все проекты")) { m.scope = "" }
                    if !m.projects.isEmpty { Divider() }
                    ForEach(m.projects) { p in Button(p.title) { m.scope = p.id } }
                } label: {
                    Label(scopeTitle, systemImage: "scope").foregroundStyle(Theme.text)
                }
                .menuStyle(.borderlessButton).fixedSize().disabled(m.busy)
                .padding(.horizontal, 10).padding(.vertical, 4)
                .overlay(RoundedRectangle(cornerRadius: Theme.radiusControl).strokeBorder(Theme.borderStrong))
                .accessibilityLabel(m.t("Scope", "Область") + ": " + scopeTitle)
                Spacer()
                if m.busy {
                    ProgressView().controlSize(.small)
                    Text(m.t("Working…", "Выполняется…")).font(.caption).foregroundStyle(Tone.info.color)
                } else if let why = m.sendBlocked { Text(why).font(.caption).foregroundStyle(Theme.muted) }
                if m.question.unicodeScalars.count > Model.questionLimit - 500 {
                    Text("\(m.question.unicodeScalars.count)/\(Model.questionLimit)").font(.caption.monospacedDigit())
                        .foregroundStyle(m.questionTooLong ? Tone.negative.color : Theme.muted)
                }
            }
            HStack(alignment: .bottom, spacing: 10) {
                TextField(m.t("Ask about your projects…", "Спросите о проектах…"), text: $m.question, axis: .vertical)
                    .lineLimit(2...5).focused($composing).focusEffectDisabled()
                    .fieldChrome(focused: composing).disabled(m.busy)
                    .accessibilityLabel(m.t("Question", "Вопрос"))
                if m.busy {
                    Button { Task { await m.stop() } } label: { Label(m.t("Stop", "Остановить"), systemImage: "stop.fill") }
                        .buttonStyle(SecondaryButtonStyle(destructive: true)).disabled(m.job == nil)
                } else {
                    Button { Task { await m.send() } } label: { Label(m.t("Send", "Отправить"), systemImage: "arrow.up") }
                        .keyboardShortcut(.return, modifiers: .command).buttonStyle(PrimaryButtonStyle())
                        .disabled(!m.ready || m.questionBlank || m.questionTooLong)
                        .help(m.sendBlocked ?? m.t("Send (⌘↩)", "Отправить (⌘↩)"))
                }
            }
            Text(m.t("Sending shares your question and selected local facts with your configured model. Model costs apply. Answers suggest actions; they do not run them.", "При отправке вопрос и выбранные локальные факты передаются настроенной модели. Применяются её тарифы. Ответы предлагают действия, но не выполняют их."))
                .font(.caption).foregroundStyle(Theme.muted)
        }
        .padding(Theme.gap).background(Theme.panel)
        .overlay(alignment: .top) { Rectangle().fill(Theme.border).frame(height: 1) }
    }
    var welcome: some View {
        VStack(alignment: .leading, spacing: 18) {
            Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 56, height: 56).accessibilityHidden(true)
            Text(m.t("What is happening across your projects?", "Что происходит с вашими проектами?"))
                .font(.system(size: 28, weight: .bold)).foregroundStyle(Theme.text)
            Text(m.t("Ask Observatory to explain its latest local snapshots. Every answer can show the facts it used. Missing or old data remains visible.", "Попросите Observatory объяснить последние локальные данные. У ответа можно раскрыть использованные факты. Отсутствующие и старые данные не скрываются."))
                .foregroundStyle(Theme.muted)
            ForEach([m.t("Which projects need attention?", "Какие проекты требуют внимания?"), m.t("How much disk space is available?", "Сколько места осталось на диске?")], id: \.self) { q in
                Button(q) { m.question = q; composing = true }.buttonStyle(SecondaryButtonStyle()).disabled(m.busy)
            }
            if !m.connected { SettingsLink { Text(m.t("Connect Observatory", "Подключить Observatory")) }.buttonStyle(PrimaryButtonStyle()) }
        }.padding(.vertical, 36)
    }
    func turnView(_ t: Turn) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(t.question).font(.title3.bold()).foregroundStyle(Theme.text).textSelection(.enabled)
            if !t.answer.isEmpty {
                Text(rendered(t.answer)).foregroundStyle(Theme.text).textSelection(.enabled)
                Text("\(t.model) · \(money(t.cost))").font(.caption.monospaced()).foregroundStyle(Theme.muted)
                if !t.degraded.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Label(m.t("Some evidence is unavailable or limited:", "Часть данных недоступна или ограничена:"), systemImage: "exclamationmark.triangle")
                            .font(.callout.weight(.semibold)).foregroundStyle(Tone.warning.color)
                        ForEach(Array(t.degraded.enumerated()), id: \.offset) { _, d in
                            Text(m.limitation(d)).font(.caption).foregroundStyle(Theme.text)
                        }
                    }
                    .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Tone.warning.fill, in: RoundedRectangle(cornerRadius: Theme.radiusControl))
                }
                ForEach(Array(t.steps.enumerated()), id: \.offset) { _, step in
                    Label { Text(rendered(step)).foregroundStyle(Theme.text) } icon: { Image(systemName: "arrow.right").foregroundStyle(Theme.accent) }
                        .textSelection(.enabled)
                }
                if !t.evidence.isEmpty {
                    DisclosureGroup {
                        ForEach(Array(t.evidence.enumerated()), id: \.offset) { _, e in
                            VStack(alignment: .leading, spacing: 4) {
                                // The id the answer cites, so "(E17)" in the text leads somewhere.
                                HStack(spacing: 6) {
                                    Text(e["id"] as? String ?? "").font(.caption.monospaced()).foregroundStyle(Theme.accent)
                                    Text(e["title"] as? String ?? "").bold().foregroundStyle(Theme.text)
                                }
                                Text("\(e["source"] as? String ?? "") · " + ((e["measured_at"] as? String) ?? m.t("measurement time unknown", "время измерения неизвестно")))
                                    .font(.caption.monospaced()).foregroundStyle(Theme.muted)
                                if let facts = e["facts"] as? [String: Any] {
                                    ForEach(facts.keys.sorted(), id: \.self) { key in
                                        Text("\(key): \(describe(facts[key]))").font(.callout).foregroundStyle(Theme.text).textSelection(.enabled)
                                    }
                                }
                            }.padding(.vertical, 6).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    } label: {
                        Text(m.t("Sources", "Источники") + " (\(t.evidence.count))").font(.callout.weight(.semibold)).foregroundStyle(Theme.text)
                    }
                    .padding(12)
                    .background(Theme.panel, in: RoundedRectangle(cornerRadius: Theme.radiusControl))
                    .overlay(RoundedRectangle(cornerRadius: Theme.radiusControl).strokeBorder(Theme.border))
                }
            } else if t.status == "working" {
                Label(m.t("Reading evidence and preparing an answer…", "Читаю факты и готовлю ответ…"), systemImage: "ellipsis")
                    .foregroundStyle(Tone.info.color)
            } else {
                Text(rendered(m.message(t.error ?? t.status))).foregroundStyle(t.status == "cancelled" ? Theme.muted : Tone.negative.color)
            }
            Rectangle().fill(Theme.border).frame(height: 1)
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
    @FocusState private var field: String?
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(m.t("Connect to your local Observatory engine. Choose a compatible installed CLI and its private workspace.", "Подключитесь к локальному движку Observatory. Выберите совместимую установленную CLI-программу и её папку данных."))
                .foregroundStyle(Theme.text).fixedSize(horizontal: false, vertical: true)
            row(m.t("Program", "Программа"), m.t("…/bin/project-observatory", "…/bin/project-observatory"), text: $m.executable, id: "executable", directory: false)
            row(m.t("Workspace", "Папка данных"), m.t("the folder `full init` created", "папка, созданная `full init`"), text: $m.workspace, id: "workspace", directory: true)
            VStack(alignment: .leading, spacing: 6) {
                label(m.t("Language", "Язык"))
                Segmented(options: [("en", "English"), ("ru", "Русский")],
                          selection: Binding(get: { m.russian ? "ru" : "en" }, set: { m.russian = $0 == "ru" }))
                    .accessibilityLabel(m.t("Language", "Язык"))
            }
            HStack(spacing: 10) {
                Button(m.t("Save and check connection", "Сохранить и проверить")) { Task { await m.saveSettings() } }
                    .buttonStyle(PrimaryButtonStyle()).disabled(m.connecting).keyboardShortcut(.defaultAction)
                if m.connecting { ProgressView().controlSize(.small); Text(m.t("Checking…", "Проверяю…")).font(.callout).foregroundStyle(Tone.info.color) }
            }
            // What the check found, in one place: engine, protocol, readiness.
            if m.connected {
                VStack(alignment: .leading, spacing: 6) {
                    Label { Text(m.t("Connected to Observatory \(m.version) (observatory-assistant/1).", "Подключено к Observatory \(m.version) (observatory-assistant/1).")).foregroundStyle(Theme.text) }
                        icon: { Image(systemName: "checkmark.circle.fill").foregroundStyle(Tone.positive.color) }
                    if m.ready { Text(m.t("The assistant is ready.", "Ассистент готов.")).font(.callout).foregroundStyle(Theme.muted) }
                    else if let e = m.error {
                        Label { Text(rendered(e)).font(.callout).foregroundStyle(Theme.text).textSelection(.enabled).fixedSize(horizontal: false, vertical: true) }
                            icon: { Image(systemName: "exclamationmark.circle").foregroundStyle(Tone.warning.color) }
                    }
                    if let at = m.savedAt { Text(m.t("Saved", "Сохранено") + " " + at.formatted(Date.FormatStyle(date: .omitted, time: .standard).locale(Locale(identifier: m.russian ? "ru_RU" : "en_US")))).font(.caption).foregroundStyle(Theme.muted) }
                }
            } else if let error = m.error {
                Label { Text(rendered(error)).font(.callout).foregroundStyle(Theme.text).textSelection(.enabled).fixedSize(horizontal: false, vertical: true) }
                    icon: { Image(systemName: "xmark.circle").foregroundStyle(AssistantView.tone(m.failure?.code).color) }
            }
            Text(m.t("Changing the program or workspace clears the draft and project scope. Accepted jobs keep running in their original workspace; return there to stop or read them.", "Смена программы или папки данных очищает черновик и выбор проекта. Принятые задания продолжают работу в прежней папке; вернитесь к ней для остановки или чтения результата."))
                .font(.caption).foregroundStyle(Theme.muted).fixedSize(horizontal: false, vertical: true)
        }
        .padding(24).frame(width: 600, alignment: .leading)
        .passionCodeWindow()
    }
    private func label(_ s: String) -> some View { Text(s).font(.caption.weight(.semibold)).foregroundStyle(Theme.muted) }
    private func row(_ title: String, _ hint: String, text: Binding<String>, id: String, directory: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            label(title)
            HStack(spacing: 8) {
                TextField(hint, text: text).focused($field, equals: id).focusEffectDisabled()
                    .fieldChrome(focused: field == id).accessibilityLabel(title)
                Button(m.t("Choose…", "Выбрать…")) { choose(directory) }.buttonStyle(SecondaryButtonStyle())
                    // Two "Choose…" buttons read the same to VoiceOver; each names its field.
                    .accessibilityLabel(m.t("Choose", "Выбрать") + ": " + title)
            }
        }
    }
    func choose(_ directory: Bool) {
        let panel = NSOpenPanel(); panel.canChooseDirectories = directory; panel.canChooseFiles = !directory
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            if directory { m.workspace = url.resolvingSymlinksInPath().path } else { m.executable = url.path }
        }
    }
}
