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

    def test_model_text_is_never_html(self):
        # docs/macos/SPEC.md: model text renders as text, without executable HTML.
        for script in (DESKTOP / "ui").glob("*.js"):
            text = script.read_text(encoding="utf-8")
            for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
                self.assertNotIn(sink, text, script.name)

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
        self.assertEqual(sorted(caps["windows"]), ["assistant", "main", "settings"])
        self.assertEqual(sorted(caps["permissions"]), ["core:default", "dialog:allow-open"])
        self.assertNotIn("remote", caps)


class DesktopRelease(unittest.TestCase):
    """tools/desktop_release.py: the release workflow's names, receipts and the unsigned-notes rule."""

    def setUp(self):
        import importlib.util
        import tempfile
        spec = importlib.util.spec_from_file_location("desktop_release", ROOT / "tools/desktop_release.py")
        self.dr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.dr)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.version = self.dr.version()

    def test_a_tag_must_name_the_version(self):
        self.assertTrue(any("does not name" in p for p in self.dr.preflight("v0.0.1", True)))
        problems = self.dr.preflight(f"v{self.version}-rc.1", True)
        self.assertFalse(any("does not name" in p for p in problems), problems)

    def test_an_unsigned_release_must_say_so(self):
        from unittest import mock
        section = f"## {self.version} — today\n\nNothing about signing.\n"
        with mock.patch.object(self.dr, "changelog_section", return_value=section):
            self.assertTrue(any("must say" in p for p in self.dr.preflight(f"v{self.version}", False)))
            self.assertEqual(self.dr.preflight(f"v{self.version}", True), [])
        with mock.patch.object(self.dr, "changelog_section", return_value=section + self.dr.UNSIGNED_LINE):
            self.assertEqual(self.dr.preflight(f"v{self.version}", False), [])

    def test_packages_get_our_names_and_a_receipt(self):
        bundle = self.tmp / "bundle"
        (bundle / "nsis").mkdir(parents=True)
        (bundle / "nsis" / "Project Observatory_1.0.0_x64-setup.exe").write_bytes(b"MZ")
        out = self.tmp / "out"
        r = self.dr.package("windows", "arm64", False, bundle, out, None, {"install": "PASS"})
        name = f"ProjectObservatory-{self.version}-windows-arm64-setup.exe"
        self.assertEqual(list(r["files"]), [name])
        self.assertEqual(r["windows_authenticode"], "NOT_SIGNED")
        receipt = json.loads((out / f"ProjectObservatory-{self.version}-windows-arm64-receipt.json").read_text())
        for key in ("version", "commit", "arch", "windows_authenticode", "checks"):
            self.assertIn(key, receipt)
        (bundle / "appimage").mkdir()
        (bundle / "deb").mkdir()
        (bundle / "appimage" / "Project Observatory_1.0.0_amd64.AppImage").write_bytes(b"x")
        (bundle / "deb" / "Project Observatory_1.0.0_amd64.deb").write_bytes(b"y")
        r = self.dr.package("linux", "x64", False, bundle, out, None, {})
        self.assertEqual(sorted(r["files"]), [f"ProjectObservatory-{self.version}-linux-x64.AppImage",
                                              f"ProjectObservatory-{self.version}-linux-x64.deb"])
        self.assertNotIn("windows_authenticode", r)

    def test_the_feed_names_every_shipped_platform_with_its_own_signature(self):
        folder = self.tmp / "release"
        folder.mkdir()
        tag = f"v{self.version}"
        for suffix in self.dr.FEED.values():
            name = f"ProjectObservatory-{self.version}-{suffix}"
            (folder / name).write_bytes(b"pkg")
            (folder / f"{name}.sig").write_text(f"sig-of-{suffix}\n")
        doc = self.dr.feed(tag, folder)
        self.assertEqual(self.dr.check_feed(doc, tag, folder), [])
        self.assertEqual(doc["platforms"]["windows-aarch64"]["signature"], "sig-of-windows-arm64-setup.exe")
        self.assertTrue(doc["platforms"]["linux-x86_64"]["url"].endswith(f"/download/{tag}/ProjectObservatory-{self.version}-linux-x64.AppImage"))

    def test_a_feed_missing_a_platform_or_another_signature_is_refused(self):
        folder = self.tmp / "release"
        folder.mkdir()
        tag = f"v{self.version}"
        for suffix in list(self.dr.FEED.values())[:3]:
            name = f"ProjectObservatory-{self.version}-{suffix}"
            (folder / name).write_bytes(b"pkg")
            (folder / f"{name}.sig").write_text("sig\n")
        doc = self.dr.feed(tag, folder)
        self.assertTrue(any("linux-aarch64" in p for p in self.dr.check_feed(doc, tag, folder)))
        doc["platforms"]["windows-x86_64"]["signature"] = "forged"
        self.assertTrue(any("signature" in p for p in self.dr.check_feed(doc, tag, folder)))


if __name__ == "__main__":
    unittest.main()
