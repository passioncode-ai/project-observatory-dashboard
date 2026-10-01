import SwiftUI
import AppKit

private enum Design { static let gap: CGFloat = 16; static let transcript: CGFloat = 760 }
@MainActor final class ApplicationDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }
}
@main struct ObservatoryApp: App {
    @NSApplicationDelegateAdaptor(ApplicationDelegate.self) var appDelegate
    @StateObject private var model = Model()
    var body: some Scene {
        WindowGroup("Project Observatory") {
            MainView().environmentObject(model).frame(minWidth: 900, minHeight: 620)
        }.defaultSize(width: 1120, height: 760)
            .commands { CommandGroup(after: .newItem) {
                Button(model.t("New conversation", "Новый диалог")) { model.newConversation() }
                    .keyboardShortcut("n").disabled(model.busy)
                Button(model.t("Refresh", "Обновить")) { Task { await model.refresh() } }.keyboardShortcut("r")
            } }
        Settings { SettingsView().environmentObject(model) }
    }
}

struct MainView: View {
    @EnvironmentObject var m: Model
    var body: some View {
        NavigationSplitView {
            List(selection: $m.selected) {
                Section(m.t("Conversations", "Диалоги")) {
                    ForEach(m.conversations) { c in Text(c.title).lineLimit(2).tag(c.id) }
                }
            }.disabled(m.busy).navigationSplitViewColumnWidth(min: 200, ideal: 245, max: 320)
            .safeAreaInset(edge: .bottom) {
                HStack { Image(systemName: "circle.fill").foregroundStyle(m.ready ? .green : .secondary).font(.caption2)
                    Text(m.version.isEmpty ? m.t("Not connected", "Нет подключения") : "Observatory \(m.version)").font(.caption)
                    Spacer(); SettingsLink { Image(systemName: "gearshape") }.accessibilityLabel(m.t("Settings", "Настройки"))
                }.padding()
            }
        } detail: {
            VStack(spacing: 0) {
                if let error = m.error {
                    HStack(alignment: .top) { Image(systemName: "exclamationmark.triangle"); Text(error).textSelection(.enabled); Spacer(); SettingsLink { Text(m.t("Settings", "Настройки")) } }
                        .font(.callout).padding().background(.orange.opacity(0.10)).accessibilityElement(children: .combine)
                }
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 24) {
                            if m.turns.isEmpty { welcome }
                            ForEach(m.turns) { turn in turnView(turn).id(turn.id) }
                        }.frame(maxWidth: Design.transcript, alignment: .leading).padding(24).frame(maxWidth: .infinity)
                    }.onChange(of: m.turns.count) { _, _ in if let id = m.turns.last?.id { proxy.scrollTo(id, anchor: .bottom) } }
                }
                Divider()
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Picker(m.t("Scope", "Область"), selection: $m.scope) {
                            Text(m.t("All projects", "Все проекты")).tag("")
                            ForEach(m.projects) { Text($0.title).tag($0.id) }
                        }.labelsHidden().frame(maxWidth: 300).disabled(m.busy)
                        Spacer()
                        if m.busy { ProgressView().controlSize(.small); Text(m.t("Working…", "Выполняется…")).font(.caption) }
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
                                .disabled(!m.ready || m.question.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || m.question.count > 6000)
                        }
                    }
                    Text(m.t("Sending shares your question and selected local facts with your configured model. Model costs apply. Answers suggest actions; they do not run them.", "При отправке вопрос и выбранные локальные факты передаются настроенной модели. Применяются её тарифы. Ответы предлагают действия, но не выполняют их."))
                        .font(.caption).foregroundStyle(.secondary)
                }.padding(Design.gap)
            }.navigationTitle("Project Observatory")
        }.toolbar {
            ToolbarItemGroup {
                Button { m.newConversation() } label: { Label(m.t("New conversation", "Новый диалог"), systemImage: "square.and.pencil") }.disabled(m.busy)
                Button { Task { await m.refresh() } } label: { Label(m.t("Refresh", "Обновить"), systemImage: "arrow.clockwise") }.disabled(m.connecting)
                Button { NSWorkspace.shared.open(URL(string: "http://127.0.0.1:47311")!) } label: { Label(m.t("Dashboard", "Дэшборд"), systemImage: "rectangle.grid.2x2") }
            }
        }.task { await m.refresh() }
        .onChange(of: m.selected) { _, id in if let id { Task { await m.load(id) } } }
    }
    var welcome: some View {
        VStack(alignment: .leading, spacing: 18) {
            Image(systemName: "binoculars").font(.system(size: 36)).foregroundStyle(.tint)
            Text(m.t("What is happening across your projects?", "Что происходит с вашими проектами?")).font(.largeTitle).bold()
            Text(m.t("Ask Observatory to explain its latest local snapshots. Every answer can show the facts it used. Missing or old data remains visible.", "Попросите Observatory объяснить последние локальные данные. У ответа можно раскрыть использованные факты. Отсутствующие и старые данные не скрываются.")).foregroundStyle(.secondary)
            ForEach([m.t("Which projects need attention?", "Какие проекты требуют внимания?"), m.t("How much disk space is available?", "Сколько места осталось на диске?")], id: \.self) { q in
                Button(q) { m.question = q }.buttonStyle(.bordered)
            }
            if !m.ready { SettingsLink { Text(m.t("Connect Observatory", "Подключить Observatory")) }.buttonStyle(.borderedProminent) }
        }.padding(.vertical, 36)
    }
    func turnView(_ t: Turn) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(t.question).font(.title3).bold().textSelection(.enabled)
            if !t.answer.isEmpty {
                Text(t.answer).textSelection(.enabled)
                Text("\(t.model) · $\(t.cost, specifier: "%.4f")").font(.caption).foregroundStyle(.secondary)
                if !t.degraded.isEmpty {
                    Text(m.t("Some evidence is unavailable or limited:", "Часть данных недоступна или ограничена:")).font(.callout).bold()
                    ForEach(Array(t.degraded.enumerated()), id: \.offset) { _, d in
                        Text("\(d["source"] as? String ?? ""): \(d["reason"] as? String ?? "")").font(.caption).foregroundStyle(.secondary)
                    }
                }
                ForEach(Array(t.steps.enumerated()), id: \.offset) { _, step in Label(step, systemImage: "arrow.right").textSelection(.enabled) }
                if !t.evidence.isEmpty {
                    DisclosureGroup(m.t("Sources", "Источники") + " (\(t.evidence.count))") {
                        ForEach(Array(t.evidence.enumerated()), id: \.offset) { _, e in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(e["title"] as? String ?? "").bold()
                                Text(e["source"] as? String ?? "").font(.caption).foregroundStyle(.secondary)
                                Text((e["measured_at"] as? String) ?? m.t("Measurement time unknown", "Время измерения неизвестно")).font(.caption).foregroundStyle(.secondary)
                                if let facts = e["facts"] as? [String: Any] {
                                    ForEach(facts.keys.sorted(), id: \.self) { key in Text("\(key): \(String(describing: facts[key]!))").font(.callout).textSelection(.enabled) }
                                }
                            }.padding(.vertical, 6).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }.padding().background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
                }
            } else if t.status == "working" { Text(m.t("Reading evidence and preparing an answer…", "Читаю факты и готовлю ответ…")).foregroundStyle(.secondary) }
            else { Text(m.message(t.error ?? t.status)).foregroundStyle(.secondary) }
            Divider()
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
            Button(m.t("Save and check connection", "Сохранить и проверить")) { Task { await m.saveSettings() } }.disabled(m.connecting)
            if let error = m.error { Text(error).font(.callout).foregroundStyle(.secondary).textSelection(.enabled) }
            Text(m.t("Changing settings leaves accepted jobs running in their original workspace. Return there to stop or read them.", "Смена настроек оставляет принятые задания в прежней папке данных. Вернитесь к ней, чтобы остановить их или прочитать результат.")).font(.caption).foregroundStyle(.secondary)
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
