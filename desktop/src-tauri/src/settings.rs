//! The app's settings: the engine program, the workspace and the interface language, kept in the
//! app's own configuration folder (docs/desktop/SCENARIOS.md SCN-001, SCN-005, SCN-011). A first
//! launch looks for the engine where README → Install puts it on this system.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Settings {
    pub program: String,
    pub workspace: String,
    /// `system`, `en` or `ru`.
    #[serde(default = "system")]
    pub language: String,
}

fn system() -> String {
    "system".to_string()
}

fn home() -> PathBuf {
    std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).map(PathBuf::from).unwrap_or_default()
}

fn local_app_data() -> PathBuf {
    std::env::var_os("LOCALAPPDATA").map(PathBuf::from).unwrap_or_else(|| home().join("AppData").join("Local"))
}

/// Where README → Install puts the engine, in the order a first launch tries them.
pub fn program_candidates() -> Vec<PathBuf> {
    if cfg!(windows) {
        vec![
            local_app_data().join("project-observatory-venv").join("Scripts").join("project-observatory.exe"),
            home().join(".local").join("bin").join("project-observatory.exe"),
        ]
    } else {
        vec![
            home().join(".local/share/project-observatory-venv/bin/project-observatory"),
            home().join(".local/bin/project-observatory"),
            PathBuf::from("/usr/local/bin/project-observatory"),
            PathBuf::from("/usr/bin/project-observatory"),
        ]
    }
}

/// The engine's own default (`osprivacy.default_home`): local app data on Windows, unless a
/// workspace already lives in the place 0.21.0 used.
pub fn default_workspace() -> PathBuf {
    let legacy = home().join(".local").join("share").join("project-observatory-full");
    if !cfg!(windows) || legacy.exists() {
        return legacy;
    }
    local_app_data().join("project-observatory-full")
}

pub fn defaults() -> Settings {
    let candidates = program_candidates();
    let program = candidates.iter().find(|p| p.is_file()).unwrap_or(&candidates[0]);
    Settings {
        program: program.to_string_lossy().to_string(),
        workspace: default_workspace().to_string_lossy().to_string(),
        language: system(),
    }
}

pub fn load(dir: &Path) -> Settings {
    std::fs::read(dir.join("settings.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Settings>(&bytes).ok())
        .map(|s| Settings { language: normalized(&s.language), ..s })
        .unwrap_or_else(defaults)
}

pub fn save(dir: &Path, settings: &Settings) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)?;
    let tmp = dir.join("settings.json.tmp");
    std::fs::write(&tmp, serde_json::to_vec_pretty(settings).expect("settings serialize"))?;
    std::fs::rename(tmp, dir.join("settings.json"))
}

/// An unknown stored value is System, as on macOS.
pub fn normalized(language: &str) -> String {
    match language {
        "en" | "ru" => language.to_string(),
        _ => system(),
    }
}

/// The interface language: an explicit choice, else Russian for a `ru`/`ru-*` first system
/// language, else English (L10N-01…06).
pub fn resolved_language(choice: &str, system_locale: Option<String>) -> &'static str {
    match normalized(choice).as_str() {
        "ru" => "ru",
        "en" => "en",
        _ => match system_locale.map(|l| l.to_lowercase()) {
            Some(l) if l == "ru" || l.starts_with("ru-") || l.starts_with("ru_") => "ru",
            _ => "en",
        },
    }
}

/// A settings change is checked before it is kept: absolute paths, a program that is a file.
pub fn problems(settings: &Settings) -> Vec<&'static str> {
    let mut out = Vec::new();
    if !Path::new(&settings.program).is_absolute() {
        out.push("program-not-absolute");
    } else if !Path::new(&settings.program).is_file() {
        out.push("backend-missing");
    }
    if !Path::new(&settings.workspace).is_absolute() {
        out.push("workspace-not-absolute");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn russian_follows_the_first_system_language_and_an_explicit_choice_wins() {
        assert_eq!(resolved_language("system", Some("ru-RU".into())), "ru");
        assert_eq!(resolved_language("system", Some("ru".into())), "ru");
        assert_eq!(resolved_language("system", Some("en-US".into())), "en");
        assert_eq!(resolved_language("system", None), "en");
        assert_eq!(resolved_language("en", Some("ru-RU".into())), "en");
        assert_eq!(resolved_language("ru", Some("en-US".into())), "ru");
        assert_eq!(resolved_language("klingon", Some("ru-RU".into())), "ru");
    }

    #[test]
    fn the_engine_is_looked_for_where_the_readme_installs_it() {
        let first = program_candidates()[0].to_string_lossy().to_string();
        if cfg!(windows) {
            assert!(first.ends_with("project-observatory-venv\\Scripts\\project-observatory.exe"), "{first}");
        } else {
            assert!(first.ends_with(".local/share/project-observatory-venv/bin/project-observatory"), "{first}");
        }
    }

    #[test]
    fn settings_round_trip_and_a_broken_file_falls_back_to_the_defaults() {
        let dir = std::env::temp_dir().join(format!("obs-settings-{}", std::process::id()));
        let s = Settings { program: "/x/p".into(), workspace: "/x/w".into(), language: "ru".into() };
        save(&dir, &s).unwrap();
        assert_eq!(load(&dir), s);
        std::fs::write(dir.join("settings.json"), b"{not json").unwrap();
        assert_eq!(load(&dir).language, "system");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn a_relative_path_is_refused_before_it_is_kept() {
        let s = Settings { program: "project-observatory".into(), workspace: "ws".into(), language: "system".into() };
        assert_eq!(problems(&s), vec!["program-not-absolute", "workspace-not-absolute"]);
    }
}
