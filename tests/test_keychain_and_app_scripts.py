"""Nothing this repository runs may raise a macOS Keychain dialog, and the app's
build script must run on the Python a Mac already has.

Two invariants over the tracked tree, each with a planted offender:

* Every launch of a Chromium-family browser passes `--use-mock-keychain` and
  `--password-store=basic`. Without them Chrome reads "Chrome Safe Storage"
  from the login keychain at start, and a browser started by a check, a test or
  a scheduled job puts a Keychain dialog in front of whoever is at the machine.
  Checked per launch (a file that flags one launch and not a second one is an
  offender), across puppeteer, pyppeteer, Playwright, Selenium and the binaries,
  in every tracked script: by suffix, `package.json` scripts, and files with no
  suffix that start with `#!`.
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
SCRIPT_SUFFIXES = {".py", ".js", ".cjs", ".mjs", ".ts", ".sh", ".bash", ".zsh", ".command",
                   ".swift", ".yml", ".yaml", ".json", ".toml"}
SCRIPT_NAMES = {"Makefile", "Dockerfile", "Brewfile"}
#: Every way a script here could start a Chromium-family browser: the libraries'
#: own launchers, Selenium's driver, the binaries, and the flags only a launched
#: browser takes.
LAUNCH = re.compile(
    r"\bpuppeteer\.launch\b|\bchromium\.launch\w*|launchPersistentContext|launch_persistent_context"
    r"|chrome-launcher|chromeLauncher\.launch|\bwebdriver\.Chrome\s*\(|\buc\.Chrome\s*\("
    r"|google-chrome|chromium-browser|Google Chrome\.app|Chromium\.app"
    r"|--remote-debugging-port|--headless\b")
#: A bare `launch(` is a browser launch where the file imports it from
#: puppeteer or pyppeteer (`import { launch } from 'puppeteer'`).
BARE_LAUNCH_SOURCE = re.compile(
    r"from\s+pyppeteer\s+import\s+[^\n]*\blaunch\b"
    r"|import\s*\{[^}]*\blaunch\b[^}]*\}\s*from\s*['\"](?:puppeteer|puppeteer-core)['\"]")
BARE_LAUNCH = re.compile(r"(?<![\w.])launch\s*\(")
REQUIRED = ("--use-mock-keychain", "--password-store=basic")
OPEN, CLOSE = "([{", ")]}"


def balanced(text: str, i: int) -> str:
    """From the first bracket at or after `i` on the same line to its match."""
    line_end = text.find("\n", i)
    line_end = len(text) if line_end < 0 else line_end
    j = next((k for k in range(i, line_end) if text[k] in OPEN), None)
    if j is None:
        return ""
    depth = 0
    for k in range(j, len(text)):
        if text[k] in OPEN:
            depth += 1
        elif text[k] in CLOSE:
            depth -= 1
            if depth == 0:
                return text[j:k + 1]
    return text[j:]


def logical_line(text: str, i: int) -> str:
    """The rest of the command line at `i`, through backslash continuations."""
    out, k = [], i
    while True:
        end = text.find("\n", k)
        line = text[k:] if end < 0 else text[k:end]
        out.append(line)
        if end < 0 or not line.rstrip().endswith("\\"):
            return "\n".join(out)
        k = end + 1


def launch_text(text: str, match: re.Match) -> str:
    """What one launch passes: its call's arguments, or its command line."""
    after = match.end()
    rest = text[after:after + 1].strip() or text[after:after + 2].strip()
    call = balanced(text, after) if (text[match.start():after].rstrip().endswith("(")
                                     or rest.startswith("(")) else ""
    return call or logical_line(text, match.start())


def names_in(fragment: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", fragment))


def is_keychain_free(text: str, match: re.Match) -> bool:
    """Both flags in this launch's own arguments, or in what a name it passes
    holds: `args: ARGS` with `const ARGS = [...]`, or Selenium's
    `options=opts` with `opts.add_argument(...)` lines."""
    passed = launch_text(text, match)
    found = set(flag for flag in REQUIRED if flag in passed)
    for name in names_in(passed):
        for use in re.finditer(r"\b" + re.escape(name) + r"\b", text):
            if use.start() == match.start():
                continue
            found |= {flag for flag in REQUIRED if flag in logical_line(text, use.start())
                      or flag in balanced(text, use.end())}
    return found == set(REQUIRED)


def launches(text: str) -> list[re.Match]:
    found = list(LAUNCH.finditer(text))
    if BARE_LAUNCH_SOURCE.search(text):
        found += list(BARE_LAUNCH.finditer(text))
    return found


def browser_launch_offenders(files: dict[str, str]) -> list[str]:
    """Paths with at least one browser launch that lacks a keychain-free flag.

    Checked per launch, not per file: a file that passes the flags to one
    launch and starts a second browser without them is an offender."""
    return sorted(path for path, text in files.items()
                  if any(not is_keychain_free(text, m) for m in launches(text)))


def is_script(root: Path, name: str) -> bool:
    path = root / name
    if not path.is_file():
        return False
    if path.suffix in SCRIPT_SUFFIXES or path.name in SCRIPT_NAMES:
        return True
    if path.suffix:
        return False
    try:
        with path.open("rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def scripts_in(root: Path, names: list[str]) -> dict[str, str]:
    """The files among `names` that can run something: by suffix, by name, or
    by a `#!` line for a file with no suffix (a `bin/` script)."""
    return {name: (root / name).read_text(encoding="utf-8", errors="replace")
            for name in names if name and is_script(root, name)}


def tracked_scripts() -> dict[str, str]:
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True)
    names = [n for n in listed.stdout.decode().split("\0")
             if n and (ROOT / n).resolve() != Path(__file__).resolve()]
    return scripts_in(ROOT, names)


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

    def test_every_launch_is_checked_not_the_file(self):
        flags = "['--use-mock-keychain', '--password-store=basic']"
        planted = {
            # Flags once, then a second launch without them: the file passing as
            # a whole is how the second one slipped through.
            "tools/twice.cjs": f"await puppeteer.launch({{args: {flags}}});\n"
                               "const other = await puppeteer.launch({headless: true});\n",
            "tools/both.cjs": f"await puppeteer.launch({{args: {flags}}});\n"
                              f"await puppeteer.launch({{headless: true, args: {flags}}});\n",
        }
        self.assertEqual(browser_launch_offenders(planted), ["tools/twice.cjs"])

    def test_the_launchers_the_old_pattern_missed(self):
        planted = {
            "tools/pyppeteer_bad.py": "from pyppeteer import launch\nbrowser = await launch(headless=True)\n",
            "tools/selenium_bad.py": "from selenium import webdriver\nd = webdriver.Chrome()\n",
            "tools/esm_bad.mjs": "import { launch } from 'puppeteer';\nconst b = await launch({});\n",
            "tools/playwright_bad.py": "b = p.chromium.launch_persistent_context('/srv/example-ws/profile')\n",
            "tools/selenium_good.py": ("from selenium import webdriver\nopts = webdriver.ChromeOptions()\n"
                                       "opts.add_argument('--use-mock-keychain')\n"
                                       "opts.add_argument('--password-store=basic')\n"
                                       "d = webdriver.Chrome(options=opts)\n"),
            "tools/esm_good.mjs": ("import { launch } from 'puppeteer';\nconst ARGS = [\n  '--use-mock-keychain',\n"
                                   "  '--password-store=basic',\n];\nconst b = await launch({args: ARGS});\n"),
        }
        self.assertEqual(browser_launch_offenders(planted),
                         ["tools/esm_bad.mjs", "tools/playwright_bad.py", "tools/pyppeteer_bad.py",
                          "tools/selenium_bad.py"])

    def test_scripts_without_a_suffix_and_package_scripts_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "bin" / "shot").write_text("#!/bin/sh\nchromium-browser --headless https://example.invalid\n")
            (root / "package.json").write_text('{"scripts": {"shot": "google-chrome --headless https://example.invalid"}}\n')
            (root / "notes").write_text("no shebang, not a script: google-chrome --headless\n")
            found = scripts_in(root, ["bin/shot", "package.json", "notes"])
        self.assertEqual(sorted(found), ["bin/shot", "package.json"])
        self.assertEqual(browser_launch_offenders(found), ["bin/shot", "package.json"])


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


class TheAppIsUniversal(unittest.TestCase):
    """fabric-workspace platforms.md PL-01: one app for Apple silicon and Intel Macs. Up to 0.21.0
    the release built the runner's architecture only (arm64), which does not open on an Intel Mac."""

    def script(self) -> str:
        return (ROOT / "macos/scripts/build-app.sh").read_text(encoding="utf-8")

    def test_both_architectures_are_built_by_default(self):
        self.assertIn('ARCHS=${OBSERVATORY_SWIFT_ARCHS:-"arm64 x86_64"}', self.script())
        builds = [line for line in self.script().splitlines() if line.lstrip().startswith(("swift build", "BIN=$(swift build"))]
        self.assertEqual(len(builds), 2, builds)
        for line in builds:
            self.assertIn('"${ARCH_ARGS[@]}"', line)

    def test_a_missing_slice_stops_the_build(self):
        text = self.script()
        self.assertIn('lipo -archs "$APP/Contents/MacOS/ProjectObservatory"', text)
        self.assertIn("missing $arch (PL-01)", text)
        self.assertLess(text.index("lipo -archs"), text.index("codesign --force"))


class TheAppCarriesTheLifecycleHelper(unittest.TestCase):
    """OBS-79 L2r: the release build of the lifecycle broker's helper lives inside the signed app, built for every
    architecture the app is, signed as nested code before the app, and refused when it does not answer the version
    of the source it was built from. The release run proves the signature on the notarized bundle."""

    def script(self) -> str:
        return (ROOT / "macos/scripts/build-app.sh").read_text(encoding="utf-8")

    def test_the_helper_is_compiled_from_the_engines_source_for_every_architecture(self):
        text = self.script()
        self.assertIn('HELPER_SRC="$ROOT/observatory/engine/node/lifecycle/native_app.swift"', text)
        self.assertTrue((ROOT / "observatory/engine/node/lifecycle/native_app.swift").is_file())
        self.assertIn('-target "$arch-apple-macos14"', text)
        self.assertIn('for arch in $ARCHS; do', text[text.index("HELPER_SRC="):])
        self.assertIn('lipo -create "${SLICES[@]}" -output "$HELPER"', text)
        self.assertIn('the lifecycle helper has slices', text)
        self.assertIn('HELPER="$APP/Contents/Helpers/lifecycle-native"', text)

    def test_the_helper_must_answer_its_sources_version(self):
        text = self.script()
        self.assertIn('"$HELPER" --version', text)
        self.assertIn("let helperVersion", text)
        source = (ROOT / "observatory/engine/node/lifecycle/native_app.swift").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r'^let helperVersion = "[0-9a-f]+"$', source, re.M)), 1)

    def test_nested_code_is_signed_first_with_the_hardened_runtime_and_its_identifier(self):
        text = self.script()
        signed = text[text.index("# The helper is nested code"):]
        release_helper = signed.index('--identifier "$HELPER_ID" --sign "$OBSERVATORY_SIGN_IDENTITY" "$HELPER"')
        release_app = signed.index('--sign "$OBSERVATORY_SIGN_IDENTITY" "$APP"')
        self.assertLess(release_helper, release_app)
        self.assertIn("--options runtime --timestamp --identifier", signed)
        self.assertLess(signed.index('--identifier "$HELPER_ID" --sign - "$HELPER"'), signed.index('--sign - "$APP"'))
        self.assertIn('codesign --verify --strict --deep "$APP"', signed)
        self.assertLess(text.index("lipo -create"), text.index("codesign --force"))

    def test_the_script_and_the_release_name_what_the_adapter_checks(self):
        adapter = (ROOT / "observatory/engine/node/lifecycle/macos.py").read_text(encoding="utf-8")
        identifier = re.search(r"^APP_HELPER_IDENTIFIER = '([^']+)'$", adapter, re.M).group(1)
        self.assertIn("APP_HELPER = Path('Contents/Helpers') / HELPER_NAME", adapter)
        self.assertIn("HELPER_NAME = 'lifecycle-native'", adapter)
        self.assertIn(f"HELPER_ID={identifier}\n", self.script())
        release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn(f'identifier \\"{identifier}\\"', release)
        self.assertIn('identifier \\"ai.passioncode.observatory\\"', release)
        self.assertIn('"$app/Contents/Helpers/lifecycle-native"', release)
        # release.yml reads the team app_update.py pins with this very pattern.
        pinned = (ROOT / "observatory/engine/app_update.py").read_text(encoding="utf-8")
        self.assertRegex(pinned, re.compile(r'^TEAM_ID = "[A-Z0-9]{10}"$', re.M))
        self.assertIn(r's/^TEAM_ID = "\([A-Z0-9]\{10\}\)"$/\1/p', release)

    def test_the_release_checks_both_requirements_on_the_notarized_bundle(self):
        release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        macos_job = release[release.index("  macos:"):release.index("  windows:")]
        check = macos_job.index("The app and its lifecycle helper satisfy")
        self.assertLess(macos_job.index("actions/notarize@"), check)
        self.assertLess(check, macos_job.index("Package the stapled app"))
        self.assertIn('-R "=anchor apple generic and identifier \\"ai.passioncode.observatory.lifecycle-native\\"',
                      macos_job)


if __name__ == "__main__":
    unittest.main()
