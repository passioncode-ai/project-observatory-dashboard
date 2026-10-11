//! The interface's words (L10N-01…06): English is the source and the key, the Russian dictionary
//! is `ui/i18n/ru.json`, shared with the app's own pages; a missing entry shows in English.

use std::collections::HashMap;
use std::sync::OnceLock;

const RU: &str = include_str!("../../ui/i18n/ru.json");

fn russian() -> &'static HashMap<String, String> {
    static DICT: OnceLock<HashMap<String, String>> = OnceLock::new();
    DICT.get_or_init(|| serde_json::from_str(RU).expect("ui/i18n/ru.json is a flat object of strings"))
}

pub fn dictionary(lang: &str) -> HashMap<String, String> {
    if lang == "ru" { russian().clone() } else { HashMap::new() }
}

/// `t("ru", "Start server", &[])`; values go in named `{placeholders}`, never concatenated.
pub fn t(lang: &str, english: &str, values: &[(&str, &str)]) -> String {
    let mut text = if lang == "ru" { russian().get(english).cloned().unwrap_or_else(|| english.to_string()) } else { english.to_string() };
    for (name, value) in values {
        text = text.replace(&format!("{{{name}}}"), value);
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_russian_entry_keeps_the_english_placeholders() {
        let placeholder = |s: &str| {
            let mut names: Vec<String> = s.split('{').skip(1).filter_map(|p| p.split('}').next()).map(str::to_string).collect();
            names.sort();
            names
        };
        for (english, russian) in russian() {
            assert_eq!(placeholder(english), placeholder(russian), "{english}");
            assert!(!russian.trim().is_empty(), "{english}");
        }
    }

    #[test]
    fn a_missing_entry_shows_in_english_with_its_values() {
        assert_eq!(t("ru", "No such words {n}", &[("n", "3")]), "No such words 3");
        assert_eq!(t("en", "Start server", &[]), "Start server");
        assert_ne!(t("ru", "Start server", &[]), "Start server");
    }
}
