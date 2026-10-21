//! The app's own updates (docs/desktop/SCENARIOS.md SCN-010, fabric-workspace platforms.md PL-04):
//! the release's `latest.json` names a package per platform, each signed with the app's minisign key
//! (its public half is in tauri.conf.json); the plugin refuses a package whose signature does not
//! verify. The feed is a static release asset (`releases/latest/download/latest.json`), never the
//! rate-limited GitHub REST API, so a check every five minutes costs one small file.
//!
//! Automatic updates are on: a found update is downloaded and verified in the background, then the
//! person is asked once per version «Restart to finish updating» (Restart now / Later). Later keeps
//! «Restart to update» in the menu and the tray; its click installs the bytes already downloaded
//! (Windows: the NSIS installer in passive mode) and restarts the app. A copy installed from the
//! .deb never checks: the next .deb updates it.

use std::collections::HashSet;
use std::sync::Mutex;
use std::time::Duration;
use tauri::AppHandle;
use tauri_plugin_updater::{Update, UpdaterExt};

/// The first check, shortly after the app starts (operator rule, 2026-10-17).
pub const FIRST_CHECK: Duration = Duration::from_secs(90);
/// Every check after it, including the retry after a check that failed (operator rule, 2026-10-17).
pub const EVERY: Duration = Duration::from_secs(5 * 60);

/// A downloaded and verified update waiting for the person's restart.
static READY: Mutex<Option<(Update, Vec<u8>)>> = Mutex::new(None);

/// Whether this copy may update itself: not a debug build, and on Linux only an AppImage.
pub fn self_updating() -> bool {
    if cfg!(debug_assertions) {
        return false;
    }
    if cfg!(target_os = "linux") {
        return std::env::var_os("APPIMAGE").is_some();
    }
    true
}

/// What one check found.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    Current,
    Found(String),
    Failed(String),
}

/// A check that finds nothing new is logged once, not every five minutes: a line is written for
/// the first result, for every change of result and for a new version, never for a repeat.
#[derive(Debug, Default)]
pub struct QuietLog {
    last: Option<Outcome>,
}

impl QuietLog {
    /// The line to log for this outcome, or None when it repeats the previous one.
    pub fn observe(&mut self, outcome: Outcome) -> Option<String> {
        let same = match (&self.last, &outcome) {
            (Some(Outcome::Current), Outcome::Current) => true,
            (Some(Outcome::Found(a)), Outcome::Found(b)) => a == b,
            // A failure that repeats is one condition, logged when it starts.
            (Some(Outcome::Failed(_)), Outcome::Failed(_)) => true,
            _ => false,
        };
        let line = match &outcome {
            Outcome::Current => "Project Observatory: update check: up to date".to_string(),
            Outcome::Found(v) => format!("Project Observatory: update check: version {v} is available"),
            Outcome::Failed(why) => format!("Project Observatory: update check failed: {why}; retrying in five minutes"),
        };
        self.last = Some(outcome);
        (!same).then_some(line)
    }
}

/// «Restart to finish updating» is asked once per version; Later keeps the menu item instead.
#[derive(Debug, Default)]
pub struct PromptOnce {
    asked: HashSet<String>,
}

impl PromptOnce {
    pub fn should_ask(&mut self, version: &str) -> bool {
        self.asked.insert(version.to_string())
    }
}

/// Look for an update now and then every five minutes; `ready` receives the version once its
/// package is downloaded and verified.
pub fn watch(app: AppHandle, ready: impl Fn(&AppHandle, String) + Send + 'static) {
    if !self_updating() {
        return;
    }
    tauri::async_runtime::spawn(async move {
        tokio_sleep(FIRST_CHECK).await;
        let mut log = QuietLog::default();
        loop {
            let outcome = check_and_download(&app).await;
            if let Outcome::Found(version) = &outcome {
                ready(&app, version.clone());
            }
            if let Some(line) = log.observe(outcome) {
                eprintln!("{line}");
            }
            tokio_sleep(EVERY).await;
        }
    });
}

async fn check_and_download(app: &AppHandle) -> Outcome {
    let updater = match app.updater() {
        Ok(u) => u,
        Err(e) => return Outcome::Failed(e.to_string()),
    };
    let update = match updater.check().await {
        Ok(Some(update)) => update,
        Ok(None) => return Outcome::Current,
        Err(e) => return Outcome::Failed(e.to_string()),
    };
    let version = update.version.clone();
    let have = READY.lock().unwrap().as_ref().map(|(u, _)| u.version.clone());
    if have.as_deref() == Some(version.as_str()) {
        return Outcome::Found(version);
    }
    match update.download(|_, _| {}, || {}).await {
        Ok(bytes) => {
            *READY.lock().unwrap() = Some((update, bytes));
            Outcome::Found(version)
        }
        Err(e) => Outcome::Failed(format!("the package of {version} could not be downloaded: {e}")),
    }
}

/// The person's «Restart now» or «Restart to update»: install what was downloaded and restart;
/// with nothing kept, download, verify and install first.
pub fn install_and_restart(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        let kept = READY.lock().unwrap().take();
        if let Some((update, bytes)) = kept {
            if update.install(&bytes).is_ok() {
                app.restart();
            }
            return;
        }
        let Ok(updater) = app.updater() else { return };
        let Ok(Some(update)) = updater.check().await else { return };
        if update.download_and_install(|_, _| {}, || {}).await.is_ok() {
            app.restart();
        }
    });
}

async fn tokio_sleep(d: Duration) {
    let (tx, rx) = tauri::async_runtime::channel::<()>(1);
    std::thread::spawn(move || {
        std::thread::sleep(d);
        let _ = tx.blocking_send(());
    });
    let mut rx = rx;
    let _ = rx.recv().await;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_debug_build_never_updates_itself() {
        assert!(!super::self_updating() || !cfg!(debug_assertions));
    }

    #[test]
    fn checks_run_every_five_minutes_and_the_first_shortly_after_start() {
        assert_eq!(EVERY, Duration::from_secs(300));
        assert!(FIRST_CHECK <= Duration::from_secs(90));
    }

    #[test]
    fn the_feed_is_the_static_release_asset_never_the_rest_api() {
        let conf: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let endpoints = conf["plugins"]["updater"]["endpoints"].as_array().unwrap();
        assert!(!endpoints.is_empty());
        for e in endpoints {
            let e = e.as_str().unwrap();
            assert!(e.starts_with("https://github.com/") && e.ends_with("/releases/latest/download/latest.json"), "{e}");
            assert!(!e.contains("api.github.com"), "{e}");
        }
    }

    #[test]
    fn a_check_that_finds_nothing_new_is_logged_once() {
        let mut log = QuietLog::default();
        assert!(log.observe(Outcome::Current).is_some(), "the first result is logged");
        for _ in 0..10 {
            assert_eq!(log.observe(Outcome::Current), None, "a repeat is quiet");
        }
        let failed = log.observe(Outcome::Failed("offline".into())).unwrap();
        assert!(failed.contains("offline"), "an error after a success is logged");
        assert_eq!(log.observe(Outcome::Failed("still offline".into())), None);
        assert!(log.observe(Outcome::Current).is_some(), "the recovery is logged");
        assert!(log.observe(Outcome::Found("1.2.3".into())).unwrap().contains("1.2.3"));
        assert_eq!(log.observe(Outcome::Found("1.2.3".into())), None);
        assert!(log.observe(Outcome::Found("1.2.4".into())).is_some(), "a newer version is logged");
    }

    #[test]
    fn the_restart_prompt_is_asked_once_per_version() {
        let mut p = PromptOnce::default();
        assert!(p.should_ask("1.2.3"));
        assert!(!p.should_ask("1.2.3"));
        assert!(p.should_ask("1.2.4"));
    }
}
