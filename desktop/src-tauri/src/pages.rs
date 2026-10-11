//! The built dashboard pages, read from the workspace with no server (SCN-008): a custom protocol
//! confined to that folder. The pages are self-contained; nothing outside the folder is served, a
//! `..` or a link out of it is refused, and only the page types the builder writes are answered.

use std::path::{Component, Path, PathBuf};

/// The origin the pages are served from: Windows' WebView2 reaches a custom protocol as
/// `http://<scheme>.localhost`, the other systems as `<scheme>://localhost`.
pub const SCHEME: &str = "observatory";

pub fn origin() -> String {
    if cfg!(windows) {
        format!("http://{SCHEME}.localhost")
    } else {
        format!("{SCHEME}://localhost")
    }
}

pub fn content_type(path: &Path) -> Option<&'static str> {
    match path.extension()?.to_str()?.to_ascii_lowercase().as_str() {
        "html" => Some("text/html; charset=utf-8"),
        "js" => Some("text/javascript; charset=utf-8"),
        "css" => Some("text/css; charset=utf-8"),
        "json" => Some("application/json; charset=utf-8"),
        "svg" => Some("image/svg+xml"),
        "png" => Some("image/png"),
        "ico" => Some("image/x-icon"),
        "woff2" => Some("font/woff2"),
        _ => None,
    }
}

/// The file a request path names inside `root`, or None when it would leave it.
pub fn resolve(root: &Path, request_path: &str) -> Option<PathBuf> {
    let decoded = percent_encoding::percent_decode_str(request_path).decode_utf8().ok()?;
    let relative = decoded.trim_start_matches('/');
    let relative = if relative.is_empty() { "index.html" } else { relative };
    let candidate = Path::new(relative);
    if candidate.components().any(|c| !matches!(c, Component::Normal(_))) {
        return None; // `..`, a root, a drive or a prefix
    }
    let root = root.canonicalize().ok()?;
    let target = root.join(candidate).canonicalize().ok()?;
    (target.starts_with(&root) && target.is_file()).then_some(target)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn site() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("obs-pages-{}-{}", std::process::id(), rand_suffix()));
        std::fs::create_dir_all(dir.join("assets")).unwrap();
        std::fs::write(dir.join("index.html"), "<p>hi</p>").unwrap();
        std::fs::write(dir.join("assets/app.js"), "1").unwrap();
        dir
    }

    fn rand_suffix() -> u128 {
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
    }

    #[test]
    fn the_index_and_a_nested_file_are_served() {
        let root = site();
        assert!(resolve(&root, "/").unwrap().ends_with("index.html"));
        assert!(resolve(&root, "/assets/app.js").unwrap().ends_with("app.js"));
        assert!(resolve(&root, "/assets%2Fapp.js").is_some());
    }

    #[test]
    fn nothing_outside_the_folder_is_served() {
        let root = site();
        std::fs::write(root.parent().unwrap().join("secret.txt"), "x").ok();
        for bad in ["/../secret.txt", "/assets/../../secret.txt", "/%2e%2e/secret.txt", "/missing.html"] {
            assert!(resolve(&root, bad).is_none(), "{bad}");
        }
    }

    #[test]
    fn only_page_types_have_a_content_type() {
        assert_eq!(content_type(Path::new("a.html")), Some("text/html; charset=utf-8"));
        assert_eq!(content_type(Path::new("a.exe")), None);
    }
}
