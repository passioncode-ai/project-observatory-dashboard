//! Project Observatory for Windows and Linux (docs/desktop/SCENARIOS.md, W8a).
//!
//! One window shows this workspace's dashboard: the live server when the engine proves it serves
//! THIS workspace, otherwise the built pages read from the workspace under a banner with their build
//! time and «Start server», otherwise the app's own «Build the dashboard» page. Navigation stays
//! inside those pages; other sites and mail open in the default browser or mail app and any other
//! scheme is refused. The dashboard's pages get no IPC: the banner's buttons are navigations to
//! `obs-action:` that this process intercepts. Only the app's own pages (first run, empty, settings)
//! call commands. Closing the window keeps the app in the tray.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod bridge;
mod l10n;
mod pages;
mod settings;

use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;
use tauri::menu::{Menu, MenuBuilder, MenuItem, PredefinedMenuItem, SubmenuBuilder};
use tauri::tray::TrayIconBuilder;
use tauri::webview::PageLoadEvent;
use tauri::{AppHandle, Manager, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_opener::OpenerExt;

const GUIDE: &str = "https://github.com/passioncode-ai/project-observatory-dashboard#install";
const ACTION_SCHEME: &str = "obs-action";

#[derive(Default)]
struct Shown {
    /// The live origin (`http://127.0.0.1:<port>`) when the dashboard is live.
    live: Option<String>,
    /// The built pages' folder when the saved pages are shown.
    pages: Option<PathBuf>,
    built_at: Option<String>,
    port: Option<u16>,
    port_busy: bool,
}

struct State {
    settings: Mutex<settings::Settings>,
    shown: Mutex<Shown>,
    config_dir: PathBuf,
    /// Bumped on every settings change, so a late answer cannot paint another workspace (SCN-005).
    generation: Mutex<u64>,
}

fn lang(app: &AppHandle) -> &'static str {
    let s = app.state::<State>();
    let choice = s.settings.lock().unwrap().language.clone();
    settings::resolved_language(&choice, sys_locale::get_locale())
}

fn backend(app: &AppHandle) -> bridge::Backend {
    let s = app.state::<State>().settings.lock().unwrap().clone();
    bridge::Backend { program: s.program, workspace: s.workspace }
}

fn app_page(page: &str, query: &[(&str, &str)]) -> WebviewUrl {
    let mut path = page.to_string();
    if !query.is_empty() {
        let q: Vec<String> = query
            .iter()
            .map(|(k, v)| format!("{k}={}", percent_encoding::utf8_percent_encode(v, percent_encoding::NON_ALPHANUMERIC)))
            .collect();
        path = format!("{path}?{}", q.join("&"));
    }
    WebviewUrl::App(path.into())
}

fn app_url(window: &WebviewWindow, page: &str, query: &[(&str, &str)]) -> Option<Url> {
    match app_page(page, query) {
        WebviewUrl::App(path) => {
            // The app's own origin is whatever the window started on (tauri://localhost or
            // http://tauri.localhost); join the page onto it.
            let base = window.url().ok()?;
            let origin = format!("{}://{}", base.scheme(), base.host_str().unwrap_or("localhost"));
            let origin = if base.port().is_some() { format!("{origin}:{}", base.port().unwrap()) } else { origin };
            Url::parse(&format!("{origin}/{}", path.to_string_lossy())).ok()
        }
        _ => None,
    }
}

/// Ask the engine where the dashboard is and show it (SCN-007, SCN-008); errors go to the
/// first-run page with their code (SCN-001).
fn show_dashboard(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        let generation = *app.state::<State>().generation.lock().unwrap();
        let b = backend(&app);
        let answer = b
            .call("status", &json!({}), Duration::from_secs(30))
            .and_then(|_| b.call("dashboard", &json!({}), Duration::from_secs(30)));
        if *app.state::<State>().generation.lock().unwrap() != generation {
            return; // the settings changed while the engine answered
        }
        let Some(window) = app.get_webview_window("main") else { return };
        match answer {
            Err(e) => {
                *app.state::<State>().shown.lock().unwrap() = Shown::default();
                if let Some(url) = app_url(&window, "setup.html", &[("code", &e.code), ("detail", e.detail.as_deref().unwrap_or(""))]) {
                    let _ = window.navigate(url);
                }
            }
            Ok(doc) => {
                let port = doc.get("port").and_then(Value::as_u64).map(|p| p as u16);
                let mut shown = Shown {
                    port,
                    port_busy: doc.get("server").and_then(Value::as_str) == Some("other-workspace"),
                    built_at: doc.get("built_at").and_then(Value::as_str).map(str::to_string),
                    ..Shown::default()
                };
                let target = if let Some(url) = doc.get("url").and_then(Value::as_str).and_then(|u| Url::parse(u).ok()) {
                    shown.live = Some(format!("{}://{}:{}", url.scheme(), url.host_str().unwrap_or(""), url.port().unwrap_or(80)));
                    Some(url)
                } else if let Some(files) = doc.get("files").and_then(Value::as_str) {
                    shown.pages = PathBuf::from(files).parent().map(PathBuf::from);
                    Url::parse(&format!("{}/index.html", pages::origin())).ok()
                } else {
                    app_url(&window, "empty.html", &[])
                };
                *app.state::<State>().shown.lock().unwrap() = shown;
                if let Some(url) = target {
                    let _ = window.navigate(url);
                }
            }
        }
    });
}

fn run_action(app: &AppHandle, action: &str, busy: &str, failed: &str) {
    let app = app.clone();
    let (action, busy, failed) = (action.to_string(), busy.to_string(), failed.to_string());
    std::thread::spawn(move || {
        let l = lang(&app);
        if let Some(w) = app.get_webview_window("main") {
            let _ = w.eval(&format!("window.__obsBanner && window.__obsBanner({})", json!(l10n::t(l, &busy, &[]))));
        }
        match backend(&app).call(&action, &json!({}), Duration::from_secs(120)) {
            Ok(_) => show_dashboard(&app),
            Err(e) => {
                if let Some(w) = app.get_webview_window("main") {
                    let text = format!("{} ({})", l10n::t(l, &failed, &[]), e.code);
                    let _ = w.eval(&format!("window.__obsBanner && window.__obsBanner({})", json!(text)));
                }
            }
        }
    });
}

/// The banner over the saved pages: their build time and «Start server», or why it is disabled.
fn banner_script(app: &AppHandle) -> String {
    let l = lang(app);
    let shown = app.state::<State>();
    let shown = shown.shown.lock().unwrap();
    let built = shown.built_at.clone().unwrap_or_default();
    let (text, can_start) = if shown.port_busy {
        (l10n::t(l, "Port {port} is held by another workspace's server; stop it there first.",
                 &[("port", &shown.port.unwrap_or(0).to_string())]), false)
    } else {
        (l10n::t(l, "Saved pages from {built}. The live dashboard needs this workspace's server.", &[("built", &built)]), true)
    };
    let button = l10n::t(l, "Start server", &[]);
    format!(
        r#"(() => {{
  const bar = document.createElement("div");
  bar.setAttribute("role", "status");
  bar.style.cssText = "position:sticky;top:0;z-index:2147483647;display:flex;gap:12px;align-items:center;padding:8px 16px;background:#30270e;color:#fff9f0;font:14px system-ui,'Segoe UI',sans-serif;border-bottom:1px solid #6f5e77";
  const text = document.createElement("span"); text.textContent = {text}; text.style.flex = "1";
  const start = document.createElement("button"); start.textContent = {button};
  start.style.cssText = "font:inherit;padding:4px 12px;border-radius:6px;border:1px solid #ffd21a;background:#ffd21a;color:#211900;cursor:pointer";
  start.disabled = {disabled};
  start.onclick = () => {{ location.href = "{scheme}:start-server"; }};
  bar.append(text, start);
  document.body.prepend(bar);
  window.__obsBanner = message => {{ text.textContent = message; start.disabled = true; }};
}})();"#,
        text = json!(text),
        button = json!(button),
        disabled = if can_start { "false" } else { "true" },
        scheme = ACTION_SCHEME,
    )
}

/// Where a navigation may go (docs/macos/SPEC.md: navigation stays inside this workspace's pages).
fn allow_navigation(app: &AppHandle, url: &Url) -> bool {
    if url.scheme() == ACTION_SCHEME {
        match url.path() {
            "start-server" => run_action(app, "serve", "Starting the server…", "Could not start the server"),
            "build" => run_action(app, "build", "Building the dashboard…", "Could not build the dashboard"),
            _ => {}
        }
        return false;
    }
    let host = url.host_str().unwrap_or("");
    // The app's own pages.
    if (url.scheme() == "tauri" && host == "localhost") || (url.scheme() == "http" && host == "tauri.localhost") {
        return true;
    }
    let origin = format!("{}://{}", url.scheme(), host);
    if origin == pages::origin() {
        return app.state::<State>().shown.lock().unwrap().pages.is_some();
    }
    let live = app.state::<State>().shown.lock().unwrap().live.clone();
    if let (Some(live), Some(port)) = (live, url.port()) {
        if format!("{}://{}:{}", url.scheme(), host, port) == live {
            return true;
        }
    }
    if matches!(url.scheme(), "http" | "https" | "mailto") {
        let _ = app.opener().open_url(url.as_str(), None::<&str>);
    }
    false
}

fn serve_pages(app: &AppHandle, request: &tauri::http::Request<Vec<u8>>) -> tauri::http::Response<Vec<u8>> {
    let root = app.state::<State>().shown.lock().unwrap().pages.clone();
    let not_found = || tauri::http::Response::builder().status(404).body(Vec::new()).unwrap();
    let Some(root) = root else { return not_found() };
    let Some(file) = pages::resolve(&root, request.uri().path()) else { return not_found() };
    let Some(kind) = pages::content_type(&file) else { return not_found() };
    match std::fs::read(&file) {
        Ok(body) => tauri::http::Response::builder()
            .header("Content-Type", kind)
            .header("Cache-Control", "no-store")
            .header("X-Content-Type-Options", "nosniff")
            .body(body)
            .unwrap(),
        Err(_) => not_found(),
    }
}

fn build_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let l = lang(app);
    let t = |s: &str| l10n::t(l, s, &[]);
    let file = SubmenuBuilder::new(app, t("File"))
        .item(&MenuItem::with_id(app, "settings", t("Settings…"), true, Some("CmdOrCtrl+,"))?)
        .separator()
        .item(&MenuItem::with_id(app, "quit", t("Quit"), true, Some("CmdOrCtrl+Q"))?)
        .build()?;
    let view = SubmenuBuilder::new(app, t("View"))
        .item(&MenuItem::with_id(app, "back", t("Back"), true, Some("Alt+Left"))?)
        .item(&MenuItem::with_id(app, "forward", t("Forward"), true, Some("Alt+Right"))?)
        .item(&MenuItem::with_id(app, "overview", t("Overview"), true, Some("CmdOrCtrl+Shift+H"))?)
        .item(&MenuItem::with_id(app, "reload", t("Reload"), true, Some("F5"))?)
        .separator()
        .item(&MenuItem::with_id(app, "browser", t("Open in browser"), true, None::<&str>)?)
        .build()?;
    let server = SubmenuBuilder::new(app, t("Server"))
        .item(&MenuItem::with_id(app, "start", t("Start server"), true, None::<&str>)?)
        .item(&MenuItem::with_id(app, "build", t("Build the dashboard"), true, None::<&str>)?)
        .build()?;
    let help = SubmenuBuilder::new(app, t("Help"))
        .item(&MenuItem::with_id(app, "guide", t("Installation guide"), true, None::<&str>)?)
        .item(&PredefinedMenuItem::about(app, Some(&t("About Project Observatory")), None)?)
        .build()?;
    MenuBuilder::new(app).items(&[&file, &view, &server, &help]).build()
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn open_settings_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("settings") {
        let _ = w.show();
        let _ = w.set_focus();
        return;
    }
    let _ = WebviewWindowBuilder::new(app, "settings", WebviewUrl::App("settings.html".into()))
        .title(l10n::t(lang(app), "Settings", &[]))
        .inner_size(640.0, 560.0)
        .min_inner_size(520.0, 480.0)
        .build();
}

fn on_menu(app: &AppHandle, id: &str) {
    let main = app.get_webview_window("main");
    match id {
        "settings" => open_settings_window(app),
        "quit" => app.exit(0),
        "back" => { main.map(|w| w.eval("history.back()")); }
        "forward" => { main.map(|w| w.eval("history.forward()")); }
        "overview" | "reload" => show_dashboard(app),
        "browser" => {
            let live = app.state::<State>().shown.lock().unwrap().live.clone();
            if let Some(live) = live {
                let _ = app.opener().open_url(format!("{live}/dashboard/index.html"), None::<&str>);
            }
        }
        "start" => run_action(app, "serve", "Starting the server…", "Could not start the server"),
        "build" => run_action(app, "build", "Building the dashboard…", "Could not build the dashboard"),
        "guide" => { let _ = app.opener().open_url(GUIDE, None::<&str>); }
        "open" => show_main(app),
        _ => {}
    }
}

#[tauri::command]
fn l10n(app: AppHandle) -> Value {
    let l = lang(&app);
    json!({ "lang": l, "words": l10n::dictionary(l) })
}

#[tauri::command]
fn get_settings(state: tauri::State<'_, State>) -> settings::Settings {
    state.settings.lock().unwrap().clone()
}

#[tauri::command]
async fn save_settings(app: AppHandle, settings: settings::Settings) -> Result<Value, bridge::BridgeError> {
    let settings = settings::Settings { language: settings::normalized(&settings.language), ..settings };
    if let Some(code) = settings::problems(&settings).first() {
        let detail = (*code == "backend-missing").then(|| settings.program.clone());
        return Err(bridge::BridgeError { code: code.to_string(), detail });
    }
    let check = bridge::Backend { program: settings.program.clone(), workspace: settings.workspace.clone() };
    let status = tauri::async_runtime::spawn_blocking(move || check.call("status", &json!({}), Duration::from_secs(30)))
        .await
        .map_err(|e| bridge::BridgeError { code: "backend-failed".into(), detail: Some(e.to_string()) })??;
    let state = app.state::<State>();
    settings::save(&state.config_dir, &settings)
        .map_err(|e| bridge::BridgeError { code: "settings-unwritable".into(), detail: Some(e.to_string()) })?;
    *state.settings.lock().unwrap() = settings;
    *state.generation.lock().unwrap() += 1;
    if let Ok(menu) = build_menu(&app) {
        if let Some(w) = app.get_webview_window("main") {
            let _ = w.set_menu(menu);
        }
    }
    show_dashboard(&app);
    Ok(status)
}

#[tauri::command]
fn show_dashboard_cmd(app: AppHandle) {
    show_dashboard(&app);
}

#[tauri::command]
async fn build_dashboard(app: AppHandle) -> Result<(), bridge::BridgeError> {
    let b = backend(&app);
    tauri::async_runtime::spawn_blocking(move || b.call("build", &json!({}), Duration::from_secs(300)))
        .await
        .map_err(|e| bridge::BridgeError { code: "backend-failed".into(), detail: Some(e.to_string()) })??;
    show_dashboard(&app);
    Ok(())
}

#[tauri::command]
fn open_settings(app: AppHandle) {
    open_settings_window(&app);
}

#[tauri::command]
fn open_guide(app: AppHandle) {
    let _ = app.opener().open_url(GUIDE, None::<&str>);
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| show_main(app)))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .register_uri_scheme_protocol(pages::SCHEME, |ctx, request| serve_pages(ctx.app_handle(), &request))
        .invoke_handler(tauri::generate_handler![
            l10n,
            get_settings,
            save_settings,
            show_dashboard_cmd,
            build_dashboard,
            open_settings,
            open_guide
        ])
        .setup(|app| {
            let config_dir = app.path().app_config_dir()?;
            let loaded = settings::load(&config_dir);
            app.manage(State {
                settings: Mutex::new(loaded),
                shown: Mutex::new(Shown::default()),
                config_dir,
                generation: Mutex::new(0),
            });
            let handle = app.handle().clone();
            let nav = handle.clone();
            let loaded_handle = handle.clone();
            let window = WebviewWindowBuilder::new(app, "main", app_page("setup.html", &[("code", "starting")]))
                .title("Project Observatory")
                .inner_size(1280.0, 820.0)
                .min_inner_size(900.0, 620.0)
                .menu(build_menu(&handle)?)
                .on_navigation(move |url| allow_navigation(&nav, url))
                .on_page_load(move |webview, payload| {
                    if payload.event() != PageLoadEvent::Finished {
                        return;
                    }
                    let url = payload.url();
                    let origin = format!("{}://{}", url.scheme(), url.host_str().unwrap_or(""));
                    if origin == pages::origin() {
                        let _ = webview.eval(&banner_script(&loaded_handle));
                    }
                })
                .build()?;
            let hide = window.clone();
            window.on_window_event(move |event| {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close(); // SCN-009: the app stays in the tray
                    let _ = hide.hide();
                }
            });
            app.on_menu_event(|app, event| on_menu(app, event.id().as_ref()));
            let l = lang(&handle);
            let tray_menu = Menu::with_items(
                app,
                &[
                    &MenuItem::with_id(app, "open", l10n::t(l, "Open", &[]), true, None::<&str>)?,
                    &MenuItem::with_id(app, "quit", l10n::t(l, "Quit", &[]), true, None::<&str>)?,
                ],
            )?;
            TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().cloned().expect("bundle icon"))
                .tooltip("Project Observatory")
                .menu(&tray_menu)
                .on_menu_event(|app, event| on_menu(app, event.id().as_ref()))
                .build(app)?;
            show_dashboard(&handle);
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("Project Observatory could not start");
}
