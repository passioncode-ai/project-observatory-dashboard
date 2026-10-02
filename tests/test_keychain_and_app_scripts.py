"""Nothing this repository runs may raise a macOS Keychain dialog, and the app's
build script must run on the Python a Mac already has.

Two invariants over the tracked tree, each with a planted offender:

* A script that launches a Chromium-family browser passes `--use-mock-keychain`
  and `--password-store=basic`. Without them Chrome reads "Chrome Safe Storage"
  from the login keychain at start, and a browser started by a check, a test or
  a scheduled job puts a Keychain dialog in front of whoever is at the machine.
* `macos/scripts/build-app.sh` writes Info.plist with any `python3`, the macOS
  3.9 one included: no `tomllib` (3.11+) and no syntax newer than 3.9.
"""
from __future__ import annotations

import ast
import plistlib
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_SUFFIXES = {".py", ".js", ".cjs", ".mjs", ".ts", ".sh", ".swift", ".yml", ".yaml"}
LAUNCH = re.compile(
    r"puppeteer\.launch|chromium\.launch|launchPersistentContext|chrome-launcher"
    r"|google-chrome|chromium-browser|Google Chrome\.app|Chromium\.app"
    r"|--remote-debugging-port|--headless\b")
REQUIRED = ("--use-mock-keychain", "--password-store=basic")


def browser_launch_offenders(files: dict[str, str]) -> list[str]:
    """Paths that start a Chromium browser without both keychain-free flags."""
    return sorted(path for path, text in files.items()
                  if LAUNCH.search(text) and not all(flag in text for flag in REQUIRED))


def tracked_scripts() -> dict[str, str]:
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True)
    out = {}
    for name in listed.stdout.decode().split("\0"):
        path = ROOT / name
        if name and path.suffix in SCRIPT_SUFFIXES and path.is_file() and path != Path(__file__).resolve():
            out[name] = path.read_text(encoding="utf-8", errors="replace")
    return out


def plist_program() -> str:
    text = (ROOT / "macos/scripts/build-app.sh").read_text(encoding="utf-8")
    found = re.search(r"python3 - [^\n]*<<'PY'\n(.*?)\nPY\n", text, re.S)
    if not found:
        raise AssertionError("build-app.sh no longer writes Info.plist with an inline python3 program")
    return found.group(1)


class BrowserLaunchesNeverTouchTheKeychain(unittest.TestCase):
    def test_every_tracked_browser_launch_is_keychain_free(self):
        self.assertEqual(browser_launch_offenders(tracked_scripts()), [])

    def test_the_rule_catches_a_planted_launch(self):
        planted = {
            "tools/bad.cjs": "const b = await puppeteer.launch({args: ['--headless']});",
            "tools/good.cjs": "puppeteer.launch({args: ['--use-mock-keychain', '--password-store=basic']})",
            "tools/half.sh": "'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' --use-mock-keychain",
            "tools/unrelated.py": "print('no browser here')",
        }
        self.assertEqual(browser_launch_offenders(planted), ["tools/bad.cjs", "tools/half.sh"])


class AppBuildScriptRunsOnTheSystemPython(unittest.TestCase):
    def test_plist_program_is_python_3_9_without_tomllib(self):
        program = plist_program()
        tree = ast.parse(program, feature_version=(3, 9))
        imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in getattr(node, "names", [])}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom) and node.module}
        self.assertNotIn("tomllib", imported)

    def test_plist_carries_the_release_version_and_bundle_contract(self):
        version = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Example.app"
            (app / "Contents").mkdir(parents=True)
            subprocess.run([sys.executable, "-", str(ROOT), str(app), "42"], input=plist_program(),
                           text=True, check=True, capture_output=True)
            doc = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        self.assertEqual(doc["CFBundleShortVersionString"], version)
        self.assertEqual(doc["CFBundleVersion"], "42")
        self.assertEqual(doc["CFBundleIdentifier"], "ai.passioncode.observatory")
        self.assertEqual(doc["CFBundleIconFile"], "AppIcon")
        self.assertEqual(doc["NSAppTransportSecurity"], {"NSAllowsLocalNetworking": True})


if __name__ == "__main__":
    unittest.main()
