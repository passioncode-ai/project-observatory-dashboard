import SwiftUI
import AppKit
import WebKit
import ObservatoryCore

/// The dashboard's WKWebView and everything it may do. One per app: the main window
/// is single, and a reopened window adopts the same view, history included.
@MainActor final class WebController: NSObject, ObservableObject, WKNavigationDelegate, WKUIDelegate {
    let view: WKWebView
    @Published var canGoBack = false
    @Published var canGoForward = false
    @Published var isLoading = false
    @Published var title = ""
    @Published var loadError: String?
    private(set) var origin: DashboardOrigin?
    /// The newest `WebViewHost` box; only it may hold the view.
    weak var host: NSView?
    private var shownSeed = -1, shownRussian: Bool?
    private var observers: [NSKeyValueObservation] = []
    /// A language chosen on the page itself travels back to the app, which adopts it.
    var onPageLocale: ((String) -> Void)?
    /// The app's words in its chosen language, for the sheets the page asks for.
    var t: (String) -> String = { $0 }
    /// A failed load of a live page usually means the server went away: re-check.
    var onLiveFailure: (() -> Void)?

    override init() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()          // keeps the dashboard's own choices (filters, folds)
        config.preferences.isElementFullscreenEnabled = false
        view = WKWebView(frame: .zero, configuration: config)
        super.init()
        view.navigationDelegate = self
        view.uiDelegate = self
        view.allowsBackForwardNavigationGestures = true
        view.setValue(false, forKey: "drawsBackground")   // no white flash before a dark page paints
        observers = [
            view.observe(\.canGoBack) { [weak self] v, _ in Task { @MainActor in self?.canGoBack = v.canGoBack } },
            view.observe(\.canGoForward) { [weak self] v, _ in Task { @MainActor in self?.canGoForward = v.canGoForward } },
            view.observe(\.isLoading) { [weak self] v, _ in Task { @MainActor in self?.isLoading = v.isLoading } },
            view.observe(\.title) { [weak self] v, _ in Task { @MainActor in self?.title = v.title ?? "" } },
        ]
    }

    /// Show this origin in this language. Reloads only when something changed, so a
    /// refresh of the mode does not throw away the page the person is reading.
    func show(_ new: DashboardOrigin, russian: Bool, seed: Int) {
        let scriptChanged = seed != shownSeed || shownRussian != russian
        if scriptChanged { installScripts(russian: russian, seed: seed) }
        if new != origin {
            origin = new; loadError = nil; load(new)
        } else if seed != shownSeed { view.reload() }
        shownSeed = seed; shownRussian = russian
    }
    private func load(_ o: DashboardOrigin) {
        if o.isLive { view.load(URLRequest(url: o.base, cachePolicy: .reloadIgnoringLocalCacheData)) }
        else { view.loadFileURL(o.base, allowingReadAccessTo: o.directory) }
    }
    func reload() { loadError = nil; if let origin, view.url == nil { load(origin) } else { view.reloadFromOrigin() } }
    func home() { if let origin { load(origin) } }
    var currentURL: URL? { view.url ?? origin?.base }

    /// The app's language reaches the dashboard through the key the dashboard itself
    /// reads (`observatory.locale`), once per app-side change (`seed`): afterwards the
    /// page's own EN/RU switch is respected, and `didFinish` reports it to the app.
    private func installScripts(russian: Bool, seed: Int) {
        let ucc = view.configuration.userContentController
        ucc.removeAllUserScripts()
        let js = """
        (function(){try{
          var k="observatory.locale", want="\(russian ? "ru" : "en")", seed="\(seed)";
          if (sessionStorage.getItem("observatory.app.seed") !== seed) {
            localStorage.setItem(k, want); sessionStorage.setItem("observatory.app.seed", seed);
          }
        }catch(e){}})();
        """
        ucc.addUserScript(WKUserScript(source: js, injectionTime: .atDocumentStart, forMainFrameOnly: true))
    }

    // MARK: navigation — one workspace's pages in, everything else out
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url, let origin else { decisionHandler(.cancel); return }
        switch origin.decide(url) {
        case .allow: decisionHandler(.allow)
        case .openExternally: NSWorkspace.shared.open(url); decisionHandler(.cancel)
        case .deny: decisionHandler(.cancel)
        }
    }
    /// `target=_blank`: a dashboard page opens here, anything else in the browser.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, let origin {
            switch origin.decide(url) {
            case .allow: webView.load(action.request)
            case .openExternally: NSWorkspace.shared.open(url)
            case .deny: break
            }
        }
        return nil
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { failed(error) }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { failed(error) }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loadError = nil
        // The page's own EN/RU switch reloads it; the language it settled on is read
        // here, after every load, and the app follows — no reload, so no loop.
        webView.evaluateJavaScript("document.documentElement.lang") { [weak self] value, _ in
            Task { @MainActor in
                guard let self, let lang = (value as? String)?.prefix(2).lowercased(), ["en", "ru"].contains(lang) else { return }
                if self.shownRussian != (lang == "ru") { self.shownRussian = lang == "ru"; self.onPageLocale?(String(lang)) }
            }
        }
    }
    private func failed(_ error: Error) {
        let e = error as NSError
        if e.domain == NSURLErrorDomain && e.code == NSURLErrorCancelled { return }
        if e.domain == "WebKitErrorDomain" && e.code == 102 { return }      // frame load interrupted by our own policy
        loadError = e.localizedDescription
        if origin?.isLive == true { onLiveFailure?() }
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { reload() }
    /// No credential, ever: see `DashboardOrigin.challenge`.
    func webView(_ webView: WKWebView, respondTo challenge: URLAuthenticationChallenge) async
        -> (URLSession.AuthChallengeDisposition, URLCredential?) {
        switch DashboardOrigin.challenge(challenge.protectionSpace.authenticationMethod) {
        case .performDefault: return (.performDefaultHandling, nil)
        case .cancel: return (.cancelAuthenticationChallenge, nil)
        }
    }

    // MARK: the page's confirm() and prompt() — native sheets, never a silent "yes"
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor () -> Void) {
        let a = alert(message); a.addButton(withTitle: "OK")
        present(a) { _ in completionHandler() }
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor (Bool) -> Void) {
        let a = alert(message); a.addButton(withTitle: "OK"); a.addButton(withTitle: t("Cancel"))
        present(a) { completionHandler($0 == .alertFirstButtonReturn) }
    }
    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor (String?) -> Void) {
        let a = alert(prompt); a.addButton(withTitle: "OK"); a.addButton(withTitle: t("Cancel"))
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24)); field.stringValue = defaultText ?? ""
        a.accessoryView = field; a.window.initialFirstResponder = field
        present(a) { completionHandler($0 == .alertFirstButtonReturn ? field.stringValue : nil) }
    }
    private func alert(_ text: String) -> NSAlert { let a = NSAlert(); a.messageText = text; a.alertStyle = .informational; return a }
    private func present(_ a: NSAlert, _ done: @escaping @MainActor (NSApplication.ModalResponse) -> Void) {
        if let w = view.window { a.beginSheetModal(for: w) { r in Task { @MainActor in done(r) } } }
        else { done(a.runModal()) }
    }
}

/// Hosts the controller's one web view. SwiftUI can build a new host (a mode
/// change rebuilds the branch) while an old one still receives an update; the
/// old host then took the view back and left with it. Only the NEWEST host may
/// hold the view.
struct WebViewHost: NSViewRepresentable {
    let web: WebController
    let view: WKWebView
    func makeNSView(context: Context) -> NSView {
        let box = NSView(); web.host = box; place(view, in: box); return box
    }
    func updateNSView(_ box: NSView, context: Context) {
        if web.host === box, view.superview !== box { place(view, in: box) }
    }
    private func place(_ v: WKWebView, in box: NSView) {
        v.removeFromSuperview(); v.translatesAutoresizingMaskIntoConstraints = false; box.addSubview(v)
        NSLayoutConstraint.activate([v.leadingAnchor.constraint(equalTo: box.leadingAnchor), v.trailingAnchor.constraint(equalTo: box.trailingAnchor),
                                     v.topAnchor.constraint(equalTo: box.topAnchor), v.bottomAnchor.constraint(equalTo: box.bottomAnchor)])
    }
}

enum WindowID { static let dashboard = "dashboard"; static let assistant = "assistant" }

/// The main window: the workspace's dashboard, live when its server answers and from
/// its built pages when it does not, with the assistant one click away.
struct DashboardView: View {
    @EnvironmentObject var m: Model
    @EnvironmentObject var web: WebController
    @EnvironmentObject var updates: Updates
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(spacing: 0) {
            banner
            switch m.dashboardMode {
            case .checking: placeholder { ProgressView(m.t("Opening the dashboard…")) }
            case .live, .files:
                ZStack {
                    WebViewHost(web: web, view: web.view)
                    if let err = web.loadError { loadFailure(err) }
                }
            case .notBuilt(let busy): notBuilt(busy)
            case .unavailable: unavailable
            }
        }
        .frame(minWidth: 900, minHeight: 620)
        .passionCodeWindow()
        .navigationTitle(web.title.isEmpty ? "Project Observatory" : web.title)
        .navigationSubtitle(subtitle)
        .toolbar { toolbar }
        .task {
            // The controller outlives this view; it holds the model weakly, by a local
            // reference (a weak capture of the property wrapper itself is a warning).
            let model = m
            web.onPageLocale = { [weak model] code in model?.adoptPageLocale(code) }
            web.onLiveFailure = { [weak model] in Task { await model?.refreshDashboard() } }
            web.t = { [weak model] key in model?.t(key) ?? key }
            updates.workInFlight = { [weak model] in model.map { $0.busy || $0.dashboardWorking } ?? false }
            await m.refreshDashboard()
            await m.refresh()
        }
        // `initial`: a window opened while the mode is already settled — reopened
        // after the workspace changed in Settings — shows THIS mode's pages. The web
        // view outlives the window, and without it kept the previous workspace's page.
        .onChange(of: m.dashboardMode, initial: true) { _, mode in show(mode) }
        .onChange(of: m.localeSeed) { _, _ in show(m.dashboardMode) }
        // The scheduled cycle rebuilt the pages: reload the one being read, in place.
        .onChange(of: m.dashboardBuiltAt) { old, new in if old != nil, new != nil, old != new { web.reload() } }
        // Back from another app: the server may have started or stopped meanwhile.
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            Task { await m.refreshDashboard() }
        }
    }
    private func show(_ mode: DashboardMode) { if let o = mode.origin { web.show(o, russian: m.russian, seed: m.localeSeed) } }

    private var subtitle: String {
        switch m.dashboardMode {
        case .live: return m.t("Live")
        case .files(_, let at, _, _): return at.map { m.t("Saved pages · {time}", ["time": m.when($0)]) } ?? m.t("Saved pages")
        default: return ""
        }
    }

    @ViewBuilder private var banner: some View {
        if case .files(_, let at, let alwaysOn, let busy) = m.dashboardMode {
            Banner(tone: busy ? .warning : .neutral, symbol: "externaldrive",
                   title: at.map { m.t("The dashboard server is not running — these are the saved pages from {time}.", ["time": m.when($0)]) }
                       ?? m.t("The dashboard server is not running — these are the saved pages."),
                   detail: [busy ? m.message("dashboard-port-busy")
                                 : m.t("Reading works as usual. “Start server” serves the same pages on 127.0.0.1."),
                            m.dashboardError].compactMap { $0 }.joined(separator: "\n")) {
                if m.dashboardWorking { ProgressView().controlSize(.small) }
                Button(alwaysOn ? m.t("Restart server") : m.t("Start server")) { Task { await m.startServer() } }
                    .buttonStyle(SecondaryButtonStyle()).disabled(!m.canStartServer)
            }
        }
    }

    private func placeholder<C: View>(@ViewBuilder _ c: () -> C) -> some View {
        VStack { Spacer(); c(); Spacer() }.frame(maxWidth: .infinity).background(Theme.bg)
    }
    private func notBuilt(_ busy: Bool) -> some View {
        placeholder {
            VStack(spacing: 14) {
                Image(systemName: "square.grid.2x2").font(.system(size: 40)).foregroundStyle(Theme.muted).accessibilityHidden(true)
                Text(m.t("This workspace has no dashboard yet")).font(.title2.bold()).foregroundStyle(Theme.text)
                Text(m.t("Build it from the registry — local, no model call.")).foregroundStyle(Theme.muted)
                if busy { Text(m.message("dashboard-port-busy")).font(.callout).foregroundStyle(Tone.warning.color).multilineTextAlignment(.center).frame(maxWidth: 560) }
                HStack {
                    Button(m.t("Build the dashboard")) { Task { await m.buildDashboard() } }
                        .buttonStyle(PrimaryButtonStyle()).disabled(m.dashboardWorking)
                    if m.dashboardWorking { ProgressView().controlSize(.small) }
                }
                if let e = m.dashboardError { Text(rendered(e)).font(.callout).foregroundStyle(Tone.negative.color).textSelection(.enabled).frame(maxWidth: 560) }
            }.padding()
        }
    }
    /// A first launch is not a failure: no engine yet, or no workspace yet, says so.
    private var unavailableTitle: String {
        switch m.dashboardFailure?.code {
        case "backend-missing": return m.t("Observatory is not installed yet")
        case "unknown-workspace": return m.t("This folder is not a workspace yet")
        default: return m.t("The dashboard cannot be opened")
        }
    }
    private var unavailable: some View {
        placeholder {
            VStack(spacing: 14) {
                Image(systemName: "exclamationmark.triangle").font(.system(size: 40))
                    .foregroundStyle(AssistantView.tone(m.dashboardFailure?.code).color).accessibilityHidden(true)
                Text(unavailableTitle).font(.title2.bold()).foregroundStyle(Theme.text)
                if let e = m.dashboardError {
                    Text(rendered(e)).foregroundStyle(Theme.text).multilineTextAlignment(.center).textSelection(.enabled).frame(maxWidth: 560)
                }
                HStack {
                    Button(m.t("Retry")) { Task { await m.refreshDashboard() } }.buttonStyle(PrimaryButtonStyle())
                    SettingsLink { Text(m.t("Settings")) }.buttonStyle(SecondaryButtonStyle())
                    if m.dashboardFailure?.code == "backend-missing" {
                        Link(m.t("Installation guide"), destination: Model.installGuide).buttonStyle(SecondaryButtonStyle())
                    }
                }
            }.padding()
        }
    }
    private func loadFailure(_ err: String) -> some View {
        VStack(spacing: 12) {
            Text(m.t("The page did not load")).font(.title3.bold()).foregroundStyle(Theme.text)
            Text(err).foregroundStyle(Theme.muted).textSelection(.enabled)
            Button(m.t("Reload")) { web.reload() }.buttonStyle(PrimaryButtonStyle())
        }
        .padding(24).background(Theme.raised, in: RoundedRectangle(cornerRadius: Theme.radiusPanel))
        .overlay(RoundedRectangle(cornerRadius: Theme.radiusPanel).strokeBorder(Theme.border))
    }

    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        ToolbarItemGroup(placement: .navigation) {
            Button { web.view.goBack() } label: { Label(m.t("Back"), systemImage: "chevron.left") }
                .disabled(!web.canGoBack).help(m.t("Back (⌘[)"))
            Button { web.view.goForward() } label: { Label(m.t("Forward"), systemImage: "chevron.right") }
                .disabled(!web.canGoForward).help(m.t("Forward (⌘])"))
        }
        ToolbarItemGroup(placement: .primaryAction) {
            UpdateToolbarButton()
            Button { web.home() } label: { Label(m.t("Overview"), systemImage: "house") }
                .disabled(m.dashboardMode.origin == nil).help(m.t("Overview (⇧⌘H)"))
            Button { Task { await m.refreshDashboard(); web.reload() } } label: {
                if web.isLoading { ProgressView().controlSize(.small) } else { Label(m.t("Reload"), systemImage: "arrow.clockwise") }
            }.help(m.t("Reload (⌘R)"))
            Button { if let u = web.currentURL { NSWorkspace.shared.open(u) } } label: { Label(m.t("Open in Browser"), systemImage: "safari") }
                .disabled(m.dashboardMode.origin == nil).help(m.t("Open this page in the default browser"))
            Button { openWindow(id: WindowID.assistant) } label: { Label(m.t("Assistant"), systemImage: "bubble.left.and.text.bubble.right") }
                .help(m.t("Ask about your projects (⇧⌘A)"))
        }
    }
}
