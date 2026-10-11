"""The Windows and Linux app (desktop/, docs/desktop/SCENARIOS.md, W8a): what can be checked without
building it. The Rust side has its own tests (`cargo test` in desktop/src-tauri, run by desktop.yml).

* Its colours are the dashboard's design tokens, value for value (one PassionCode product).
* Its pages carry no inline script: the window's CSP is `script-src 'self'`.
* Its versions move with the engine's.
* Every English string its pages and Rust code show has a Russian entry, and no entry is stale.
* Its window permissions stay minimal: no shell, no file system, only the system file picker.
"""
from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"
TOKENS = ROOT / "observatory/engine/dashboard/brand/passioncode-tokens.css"


def dark_tokens() -> dict[str, str]:
    """The first (dark) block of the vendored tokens."""
    text = TOKENS.read_text(encoding="utf-8")
    first = text[text.index("{") + 1:text.index("}")]
    return dict(re.findall(r"(--pc-[a-z-]+):\s*([^;]+);", first))


class DesktopApp(unittest.TestCase):
    def test_its_colours_are_the_dashboards_tokens(self):
        css = (DESKTOP / "ui/app.css").read_text(encoding="utf-8")
        declared = dict(re.findall(r"(--pc-[a-z-]+):\s*(#[0-9a-fA-F]{3,8})", css[:css.index("}")]))
        self.assertGreaterEqual(len(declared), 10)
        tokens = dark_tokens()
        for name, value in declared.items():
            self.assertEqual(value.lower(), tokens[name].strip().lower(), name)

    def test_no_page_carries_an_inline_script(self):
        for page in (DESKTOP / "ui").glob("*.html"):
            html = page.read_text(encoding="utf-8")
            self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>", page.name)
            self.assertNotRegex(html, r"\son[a-z]+=", page.name)

    def test_its_versions_move_with_the_engines(self):
        engine = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        self.assertEqual(json.loads((DESKTOP / "package.json").read_text())["version"], engine)
        self.assertEqual(json.loads((DESKTOP / "src-tauri/tauri.conf.json").read_text())["version"], engine)
        self.assertEqual(tomllib.loads((DESKTOP / "src-tauri/Cargo.toml").read_text())["package"]["version"], engine)

    def test_every_shown_string_has_its_russian_and_none_is_stale(self):
        russian = json.loads((DESKTOP / "ui/i18n/ru.json").read_text(encoding="utf-8"))
        used = set()
        for page in (DESKTOP / "ui").glob("*.html"):
            used |= set(re.findall(r'data-t="([^"]+)"', page.read_text(encoding="utf-8")))
        for script in (DESKTOP / "ui").glob("*.js"):
            used |= set(re.findall(r'\bt\("([^"]+)"', script.read_text(encoding="utf-8")))
            used |= set(re.findall(r'\[\s*"([A-Z][^"]+)",', script.read_text(encoding="utf-8")))
        for source in (DESKTOP / "src-tauri/src").glob("*.rs"):
            text = source.read_text(encoding="utf-8")
            if source.name == "l10n.rs":
                continue
            used |= set(re.findall(r'\bt\([a-z]+, "([^"]+)"', text))
            used |= set(re.findall(r'\bt\("([^"]+)"\)', text))   # the menu's closure
            used |= set(re.findall(r'l10n::t\([a-z&]+, "([^"]+)"', text))
            used |= set(re.findall(r'run_action\([a-z&]+, "[a-z]+", "([^"]+)", "([^"]+)"\)', text) and
                        [s for pair in re.findall(r'run_action\([a-z&]+, "[a-z]+", "([^"]+)", "([^"]+)"\)', text) for s in pair])
        used.discard("Project Observatory")
        missing = sorted(u for u in used if u not in russian)
        self.assertEqual(missing, [], "strings shown without a Russian entry")
        stale = sorted(k for k in russian if k not in used and k != "Project Observatory")
        self.assertEqual(stale, [], "Russian entries no screen uses")

    def test_its_windows_may_only_pick_a_file(self):
        caps = json.loads((DESKTOP / "src-tauri/capabilities/main.json").read_text())
        self.assertEqual(sorted(caps["windows"]), ["main", "settings"])
        self.assertEqual(sorted(caps["permissions"]), ["core:default", "dialog:allow-open"])
        self.assertNotIn("remote", caps)


if __name__ == "__main__":
    unittest.main()
