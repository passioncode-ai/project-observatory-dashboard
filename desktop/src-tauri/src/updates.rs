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

use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::AppHandle;
use tauri_plugin_updater::{Update, UpdaterExt};

/// The first check, shortly after the app starts (operator rule, 2026-10-17).
pub const FIRST_CHECK: Duration = Duration::from_secs(90);
/// Every check after it, including the retry after a check that failed (operator rule, 2026-10-17).
pub const EVERY: Duration = Duration::from_secs(5 * 60);

/// The longest a package that keeps failing waits before it is downloaded again (the engine's
/// `RETRY_FAILED_UPDATE`): a bad package is not fetched every five minutes (review M2).
pub const MAX_BACKOFF: Duration = Duration::from_secs(6 * 3600);

/// A downloaded and verified update waiting for the person's restart. Kept in memory: the
/// package is a few tens of megabytes, held only between the download and the restart.
static READY: Mutex<Option<(Update, Vec<u8>)>> = Mutex::new(None);
/// Failed package downloads, per version.
static BACKOFF: Mutex<Backoff> = Mutex::new(Backoff { version: None, failures: 0, next: None });

/// After a failed download of a version, the next one waits 5 minutes, then 10, 20, … up to six
/// hours; a newer version, or a success, starts over. The feed is still read every five minutes.
#[derive(Debug)]
pub struct Backoff {
    version: Option<String>,
    failures: u32,
    next: Option<Instant>,
}

impl Backoff {
    pub fn delay(failures: u32) -> Duration {
        let steps = failures.saturating_sub(1).min(16);
        EVERY.saturating_mul(1u32 << steps).min(MAX_BACKOFF)
    }

    /// Whether `version` may be downloaded now.
    pub fn allowed(&self, version: &str, now: Instant) -> bool {
        self.version.as_deref() != Some(version) || self.next.map_or(true, |at| now >= at)
    }

    pub fn failed(&mut self, version: &str, now: Instant) -> Duration {
        if self.version.as_deref() != Some(version) {
            self.version = Some(version.to_string());
            self.failures = 0;
        }
        self.failures += 1;
        let wait = Self::delay(self.failures);
        self.next = Some(now + wait);
        wait
    }

    pub fn succeeded(&mut self) {
        *self = Backoff { version: None, failures: 0, next: None };
    }
}

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
/// The last version asked about is kept in a file of the app's config folder, so a restart of the
/// app does not ask again (review L2, as the Mac app's `updatePromptedVersion`).
#[derive(Debug, Default)]
pub struct PromptOnce {
    asked: Option<String>,
}

pub const PROMPTED_FILE: &str = "update-prompted-version";

impl PromptOnce {
    /// From the stored file's text (empty or absent: never asked).
    pub fn from_stored(text: Option<&str>) -> Self {
        PromptOnce { asked: text.map(str::trim).filter(|t| !t.is_empty()).map(str::to_string) }
    }

    /// True once per version; the caller stores `version` when it is.
    pub fn should_ask(&mut self, version: &str) -> bool {
        if self.asked.as_deref() == Some(version) {
            return false;
        }
        self.asked = Some(version.to_string());
        true
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
    if !BACKOFF.lock().unwrap().allowed(&version, Instant::now()) {
        return Outcome::Failed(format!("the package of {version} failed earlier; waiting before the next download"));
    }
    match update.download(|_, _| {}, || {}).await {
        Ok(bytes) => {
            BACKOFF.lock().unwrap().succeeded();
            *READY.lock().unwrap() = Some((update, bytes));
            Outcome::Found(version)
        }
        Err(e) => {
            let wait = BACKOFF.lock().unwrap().failed(&version, Instant::now());
            Outcome::Failed(format!("the package of {version} could not be downloaded: {e}; next attempt in {} min",
                                    wait.as_secs() / 60))
        }
    }
}

/// The person's «Restart now» or «Restart to update»: install what was downloaded and restart;
/// with nothing kept, download, verify and install first. A failure is never silent (review M3):
/// it is logged, `failed` receives the reason for the person, and kept bytes stay kept so the
/// menu item retries the same package.
pub fn install_and_restart(app: AppHandle, failed: impl Fn(&AppHandle, String) + Send + 'static) {
    tauri::async_runtime::spawn(async move {
        let kept = READY.lock().unwrap().take();
        let result = match kept {
            Some((update, bytes)) => match update.install(&bytes) {
                Ok(()) => Ok(()),
                Err(e) => {
                    let why = format!("version {} could not be installed: {e}", update.version);
                    *READY.lock().unwrap() = Some((update, bytes));
                    Err(why)
                }
            },
            None => match app.updater() {
                Err(e) => Err(format!("the updater is unavailable: {e}")),
                Ok(updater) => match updater.check().await {
                    Ok(Some(update)) => update
                        .download_and_install(|_, _| {}, || {})
                        .await
                        .map_err(|e| format!("version {} could not be downloaded or installed: {e}", update.version)),
                    Ok(None) => Err("no update is available any more".to_string()),
                    Err(e) => Err(format!("the update could not be checked: {e}")),
                },
            },
        };
        match result {
            Ok(()) => app.restart(),
            Err(why) => {
                eprintln!("Project Observatory: {}", install_failure_line(&why));
                failed(&app, why);
            }
        }
    });
}

/// The log line for a failed install: the reason, and that the menu item tries again.
pub fn install_failure_line(why: &str) -> String {
    format!("update install failed: {why}; «Restart to update» in the menu tries again")
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
    fn the_restart_prompt_is_asked_once_per_version_across_restarts() {
        let mut p = PromptOnce::default();
        assert!(p.should_ask("1.2.3"));
        assert!(!p.should_ask("1.2.3"));
        assert!(p.should_ask("1.2.4"));
        // The stored version survives the app's restart (review L2).
        let mut again = PromptOnce::from_stored(Some("1.2.4\n"));
        assert!(!again.should_ask("1.2.4"));
        assert!(again.should_ask("1.2.5"));
        assert!(PromptOnce::from_stored(Some("  ")).should_ask("1.2.4"), "an empty file: never asked");
    }

    #[test]
    fn a_failing_package_backs_off_to_six_hours_and_a_new_version_starts_over() {
        // Review M2: an 80 MB package that failed was fetched again every five minutes.
        let t0 = Instant::now();
        let mut b = Backoff { version: None, failures: 0, next: None };
        assert!(b.allowed("1.2.3", t0));
        assert_eq!(b.failed("1.2.3", t0), Duration::from_secs(300));
        assert!(!b.allowed("1.2.3", t0 + Duration::from_secs(299)));
        assert!(b.allowed("1.2.3", t0 + Duration::from_secs(300)));
        assert_eq!(b.failed("1.2.3", t0), Duration::from_secs(600));
        assert_eq!(b.failed("1.2.3", t0), Duration::from_secs(1200));
        for _ in 0..20 {
            b.failed("1.2.3", t0);
        }
        assert_eq!(Backoff::delay(30), MAX_BACKOFF, "never more than six hours apart");
        assert!(!b.allowed("1.2.3", t0 + Duration::from_secs(5 * 3600)));
        assert!(b.allowed("1.2.4", t0), "a newer version is tried at once");
        b.succeeded();
        assert!(b.allowed("1.2.3", t0));
    }

    #[test]
    fn a_failed_install_is_said_with_its_retry() {
        // Review M3: the install error was dropped and the bytes thrown away.
        let line = install_failure_line("version 1.2.3 could not be installed: access denied");
        assert!(line.contains("access denied") && line.contains("tries again"), "{line}");
    }
}
