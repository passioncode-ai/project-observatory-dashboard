//! The app's own updates (docs/desktop/SCENARIOS.md SCN-010, fabric-workspace platforms.md PL-04):
//! the release's `latest.json` names a package per platform, each signed with the app's minisign key
//! (its public half is in tauri.conf.json); the plugin refuses a package whose signature does not
//! verify. A found update becomes «Restart to update» in the menu and the tray, never a modal; the
//! person's click installs it (Windows: the NSIS installer in passive mode) and restarts the app.
//! A copy installed from the .deb never checks: the next .deb updates it.

use std::time::Duration;
use tauri::AppHandle;
use tauri_plugin_updater::UpdaterExt;

const FIRST_CHECK: Duration = Duration::from_secs(90);
const EVERY: Duration = Duration::from_secs(6 * 3600);

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

/// Look for an update now and then every six hours; `found` receives the version when one is ready.
pub fn watch(app: AppHandle, found: impl Fn(&AppHandle, String) + Send + 'static) {
    if !self_updating() {
        return;
    }
    tauri::async_runtime::spawn(async move {
        tokio_sleep(FIRST_CHECK).await;
        loop {
            if let Ok(updater) = app.updater() {
                if let Ok(Some(update)) = updater.check().await {
                    found(&app, update.version.clone());
                }
            }
            tokio_sleep(EVERY).await;
        }
    });
}

/// The person's «Restart to update»: download, verify, install, restart.
pub fn install_and_restart(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
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
    #[test]
    fn a_debug_build_never_updates_itself() {
        assert!(!super::self_updating() || !cfg!(debug_assertions));
    }
}
