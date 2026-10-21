//! Opening at login (docs/desktop/SCENARIOS.md SCN-012, SCN-013), on by default (operator rule,
//! 2026-10-16). The choice is `launch_at_login` in the app's settings: absent on a first launch,
//! which turns it on and keeps `true`; the person turns it off in Settings, and an update keeps
//! whatever was chosen. The entry starts the app with `--background`: in the tray, no window.
//!
//! Linux: an XDG autostart file, `$XDG_CONFIG_HOME/autostart/ai.passioncode.observatory.desktop`.
//! Windows: the value «Project Observatory» under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`,
//! written with `reg.exe`. No plugin and no new dependency.
//!
//! At every launch the entry is checked against the choice: an entry the app could not write, or
//! one that disappeared while the choice is on, is named in a message with how to fix it (restore
//! it, or turn the choice off in Settings), never left silent. A debug build and macOS (which has
//! its own app) never touch a login item.

use std::path::{Path, PathBuf};

pub const BACKGROUND_ARG: &str = "--background";
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub const DESKTOP_FILE: &str = "ai.passioncode.observatory.desktop";
pub const RUN_KEY: &str = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run";
pub const RUN_VALUE: &str = "Project Observatory";

/// What a launch does about the login entry, decided from the stored choice and what is there.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Launch {
    /// First launch: write the entry and keep the choice `true`.
    EnableFirstRun,
    /// The choice is on and the entry is there: write it again (the program may have moved).
    Refresh,
    /// The choice is on and the entry is gone: ask the person (restore, or turn it off).
    AskMissing,
    /// The choice is off and an entry is still there: remove it.
    Remove,
    /// Nothing to do, or this copy does not manage a login entry.
    Nothing,
}

/// `present` is None where this copy manages no entry (a debug build, macOS).
pub fn decide(stored: Option<bool>, present: Option<bool>) -> Launch {
    let Some(present) = present else { return Launch::Nothing };
    match (stored, present) {
        (None, _) => Launch::EnableFirstRun,
        (Some(true), true) => Launch::Refresh,
        (Some(true), false) => Launch::AskMissing,
        (Some(false), true) => Launch::Remove,
        (Some(false), false) => Launch::Nothing,
    }
}

/// Whether this copy manages a login entry at all.
pub fn managed() -> bool {
    !cfg!(debug_assertions) && (cfg!(windows) || cfg!(target_os = "linux"))
}

/// The program the entry starts: the AppImage when running from one, else this executable.
#[cfg_attr(not(any(target_os = "linux", windows)), allow(dead_code))]
pub fn program() -> Option<PathBuf> {
    if let Some(image) = std::env::var_os("APPIMAGE") {
        return Some(PathBuf::from(image));
    }
    std::env::current_exe().ok()
}

/// One argument of a desktop entry's `Exec` key, quoted (XDG Desktop Entry spec, "The Exec key"):
/// `"`, `` ` ``, `$` and `\` are backslash-escaped inside the quotes, then every backslash is
/// doubled by the string escape rule, and `%` is written `%%`.
pub fn exec_arg(value: &str) -> String {
    let mut quoted = String::from("\"");
    for c in value.chars() {
        match c {
            '"' | '`' | '$' => {
                quoted.push_str("\\\\");
                quoted.push(c);
            }
            '\\' => quoted.push_str("\\\\\\\\"),
            '%' => quoted.push_str("%%"),
            _ => quoted.push(c),
        }
    }
    quoted.push('"');
    quoted
}

/// The XDG autostart file that starts `program` in the tray at login.
pub fn desktop_entry(program: &str) -> String {
    format!(
        "[Desktop Entry]\nType=Application\nName=Project Observatory\nComment=Starts Project Observatory in the tray at login\nExec={} {BACKGROUND_ARG}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n",
        exec_arg(program)
    )
}

/// `reg.exe` arguments that write the Run value (the program quoted, then `--background`).
pub fn reg_add_args(program: &str) -> Vec<String> {
    ["add", RUN_KEY, "/v", RUN_VALUE, "/t", "REG_SZ", "/d"]
        .iter()
        .map(|s| s.to_string())
        .chain([format!("\"{program}\" {BACKGROUND_ARG}"), "/f".to_string()])
        .collect()
}

pub fn reg_delete_args() -> Vec<String> {
    ["delete", RUN_KEY, "/v", RUN_VALUE, "/f"].iter().map(|s| s.to_string()).collect()
}

pub fn reg_query_args() -> Vec<String> {
    ["query", RUN_KEY, "/v", RUN_VALUE].iter().map(|s| s.to_string()).collect()
}

/// `$XDG_CONFIG_HOME/autostart` (absolute only, as the spec asks), else `~/.config/autostart`.
pub fn autostart_dir(xdg_config_home: Option<&str>, home: &Path) -> PathBuf {
    match xdg_config_home {
        Some(dir) if Path::new(dir).is_absolute() => Path::new(dir).join("autostart"),
        _ => home.join(".config").join("autostart"),
    }
}

#[cfg(target_os = "linux")]
fn entry_file() -> PathBuf {
    let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default();
    let xdg = std::env::var("XDG_CONFIG_HOME").ok();
    autostart_dir(xdg.as_deref(), &home).join(DESKTOP_FILE)
}

/// Whether the entry is there; None where this copy manages none.
pub fn present() -> Option<bool> {
    if !managed() {
        return None;
    }
    platform::present()
}

/// Where the entry lives, for the message that names it.
pub fn location() -> String {
    platform::location()
}

/// Write (`on`) or remove the entry. The error names what failed, for the message the person sees.
pub fn apply(on: bool) -> Result<(), String> {
    if !managed() {
        return Ok(());
    }
    platform::apply(on)
}

#[cfg(target_os = "linux")]
mod platform {
    use super::*;

    pub fn present() -> Option<bool> {
        Some(entry_file().is_file())
    }

    pub fn location() -> String {
        entry_file().display().to_string()
    }

    pub fn apply(on: bool) -> Result<(), String> {
        let file = entry_file();
        if !on {
            return match std::fs::remove_file(&file) {
                Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(format!("{}: {e}", file.display())),
                _ => Ok(()),
            };
        }
        let program = program().ok_or_else(|| "this program's path is unknown".to_string())?;
        let write = || -> std::io::Result<()> {
            std::fs::create_dir_all(file.parent().expect("autostart folder"))?;
            let tmp = file.with_extension("desktop.tmp");
            std::fs::write(&tmp, desktop_entry(&program.to_string_lossy()))?;
            std::fs::rename(tmp, &file)
        };
        write().map_err(|e| format!("{}: {e}", file.display()))
    }
}

#[cfg(windows)]
mod platform {
    use super::*;

    pub fn present() -> Option<bool> {
        Some(reg(&reg_query_args()).is_ok())
    }

    pub fn location() -> String {
        format!("{RUN_KEY}, {RUN_VALUE}")
    }

    pub fn apply(on: bool) -> Result<(), String> {
        if !on {
            return if present() == Some(true) { reg(&reg_delete_args()) } else { Ok(()) };
        }
        let program = program().ok_or_else(|| "this program's path is unknown".to_string())?;
        reg(&reg_add_args(&program.to_string_lossy()))
    }
}

#[cfg(not(any(target_os = "linux", windows)))]
mod platform {
    pub fn present() -> Option<bool> {
        None
    }

    pub fn location() -> String {
        String::new()
    }

    pub fn apply(_on: bool) -> Result<(), String> {
        Ok(())
    }
}

#[cfg(windows)]
fn reg(args: &[String]) -> Result<(), String> {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let out = std::process::Command::new("reg.exe")
        .args(args)
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .map_err(|e| format!("reg.exe: {e}"))?;
    if out.status.success() {
        Ok(())
    } else {
        Err(format!("reg.exe {}: {}", args[0], String::from_utf8_lossy(&out.stderr).trim()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_first_launch_turns_it_on_and_a_kept_choice_is_respected() {
        assert_eq!(decide(None, Some(false)), Launch::EnableFirstRun);
        assert_eq!(decide(None, Some(true)), Launch::EnableFirstRun);
        assert_eq!(decide(Some(true), Some(true)), Launch::Refresh);
        assert_eq!(decide(Some(false), Some(false)), Launch::Nothing);
        assert_eq!(decide(Some(false), Some(true)), Launch::Remove);
    }

    #[test]
    fn an_entry_gone_while_the_choice_is_on_is_never_silent() {
        assert_eq!(decide(Some(true), Some(false)), Launch::AskMissing);
    }

    #[test]
    fn a_copy_that_manages_no_entry_does_nothing() {
        for stored in [None, Some(true), Some(false)] {
            assert_eq!(decide(stored, None), Launch::Nothing);
        }
        assert!(!managed() || !cfg!(debug_assertions), "a debug build never writes a login entry");
    }

    #[test]
    fn the_desktop_entry_starts_the_program_in_the_tray() {
        let text = desktop_entry("/opt/Project Observatory.AppImage");
        assert!(text.starts_with("[Desktop Entry]\n"));
        assert!(text.contains("\nType=Application\n"));
        assert!(text.contains("\nExec=\"/opt/Project Observatory.AppImage\" --background\n"), "{text}");
        assert!(text.contains("\nTerminal=false\n"));
    }

    #[test]
    fn exec_quoting_follows_the_desktop_entry_spec() {
        assert_eq!(exec_arg("/a b"), "\"/a b\"");
        assert_eq!(exec_arg("/a$b"), "\"/a\\\\$b\"");
        assert_eq!(exec_arg("/a\"b"), "\"/a\\\\\"b\"");
        assert_eq!(exec_arg("/a\\b"), "\"/a\\\\\\\\b\"");
        assert_eq!(exec_arg("/50%"), "\"/50%%\"");
    }

    #[test]
    fn the_run_value_quotes_the_program_and_starts_it_in_the_tray() {
        let args = reg_add_args(r"C:\Program Files\Project Observatory\project-observatory-app.exe");
        assert_eq!(args[..7], ["add", RUN_KEY, "/v", "Project Observatory", "/t", "REG_SZ", "/d"]);
        assert_eq!(args[7], r#""C:\Program Files\Project Observatory\project-observatory-app.exe" --background"#);
        assert_eq!(args[8], "/f");
        assert_eq!(reg_delete_args(), ["delete", RUN_KEY, "/v", "Project Observatory", "/f"]);
        assert_eq!(reg_query_args(), ["query", RUN_KEY, "/v", "Project Observatory"]);
    }

    #[test]
    fn the_autostart_folder_honours_an_absolute_xdg_config_home_only() {
        let home = Path::new("/srv/example-home");
        assert_eq!(autostart_dir(Some("/x/cfg"), home), Path::new("/x/cfg/autostart"));
        assert_eq!(autostart_dir(Some("rel"), home), Path::new("/srv/example-home/.config/autostart"));
        assert_eq!(autostart_dir(None, home), Path::new("/srv/example-home/.config/autostart"));
    }
}
