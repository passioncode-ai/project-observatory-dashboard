"""The engine's own copies of the user documents are derived from docs/, and the
engine's maintenance step installs the SDK the package pins.

A wheel user has no checkout: `full onboard` prints the engine's
AGENT-ONBOARDING.md and points at the engine's ONBOARDING.md, so a stale engine
copy is what a newcomer reads. On 2026-10-03 the engine ONBOARDING was 314 lines
against 611 in docs/, months behind. The copy is docs/ with two mechanical
changes: test paths are engine-relative (`observatory/engine/` dropped), and a
relative link to a file the engine does not ship points at the repository.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DOCS = ROOT / "observatory/engine/docs"
REPO_DOCS = "https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/"
COPIES = ("ONBOARDING.md", "COMPATIBILITY.md", "AGENT-ONBOARDING.md")
#: Byte-for-byte copies the engine ships of root files a wheel user has no other way to get.
#: The lock is the dependency set CI tests; `full update` and the `[full]` advice install from it.
FILE_COPIES = {"requirements-full.lock": ROOT / "observatory/engine/requirements-full.lock"}
LINK = re.compile(r"\]\(([^)#\s]+)(#[^)\s]*)?\)")


def derived(name: str) -> str:
    text = (ROOT / "docs" / name).read_text(encoding="utf-8").replace("observatory/engine/", "")

    def relink(m: re.Match) -> str:
        target, anchor = m.group(1), m.group(2) or ""
        if re.match(r"[a-z]+:", target) or (ENGINE_DOCS / target).exists():
            return m.group(0)
        return f"]({REPO_DOCS}{target}{anchor})"
    return LINK.sub(relink, text)


class EngineDocCopies(unittest.TestCase):
    def test_each_engine_copy_is_derived_from_docs(self):
        for name in COPIES:
            with self.subTest(name):
                self.assertEqual((ENGINE_DOCS / name).read_text(encoding="utf-8"), derived(name),
                                 f"observatory/engine/docs/{name} drifted from docs/{name}")

    def test_the_sync_tool_parses_its_arguments_and_help_writes_nothing(self):
        # `--help` used to be ignored: the tool ran in write mode and printed
        # "written: none". Help must print usage and touch no copy; an unknown
        # argument is refused rather than read as "write".
        import subprocess
        import sys
        tool = ROOT / "tools/sync_engine_docs.py"
        before = {name: (ENGINE_DOCS / name).stat().st_mtime_ns for name in COPIES}
        shown = subprocess.run([sys.executable, str(tool), "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("usage: sync_engine_docs.py", shown.stdout)
        self.assertIn("--check", shown.stdout)
        self.assertNotIn("written", shown.stdout)
        refused = subprocess.run([sys.executable, str(tool), "--chek"], capture_output=True, text=True, timeout=60)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("unrecognized arguments", refused.stderr)
        self.assertEqual({name: (ENGINE_DOCS / name).stat().st_mtime_ns for name in COPIES}, before)
    def test_the_engine_ships_the_tested_lock_unchanged(self):
        for name, copy in FILE_COPIES.items():
            with self.subTest(name):
                self.assertEqual(copy.read_bytes(), (ROOT / name).read_bytes(),
                                 f"{copy.relative_to(ROOT)} drifted from {name}; run tools/sync_engine_docs.py")

    def test_the_runtime_check_knows_every_full_dependency(self):
        import tomllib
        extra = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "optional-dependencies"]["full"]
        names = {re.split(r"[=<>!~ ;\[]", requirement, maxsplit=1)[0].lower() for requirement in extra}
        source = (ROOT / "observatory/engine/workspace.py").read_text(encoding="utf-8")
        declared = re.search(r"FULL_MODULES = \{(.*?)\}", source, re.S).group(1)
        self.assertEqual(set(re.findall(r'"([a-z0-9-]+)":', declared)), names)
        lock = (ROOT / "requirements-full.lock").read_text(encoding="utf-8").lower()
        for requirement in extra:
            with self.subTest(requirement):
                self.assertIn(requirement.lower() + "\n", lock, "every [full] pin is in the lock at the same version")

    def test_the_deps_step_installs_the_sdk_the_package_pins(self):
        pinned = re.search(r'"mcp==([\d.]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8")).group(1)
        step = (ROOT / "observatory/engine/observatory.py").read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r"mcp==([\d.]+)", step)), {pinned})


if __name__ == "__main__":
    unittest.main()
