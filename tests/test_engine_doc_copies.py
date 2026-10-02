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

    def test_the_deps_step_installs_the_sdk_the_package_pins(self):
        pinned = re.search(r'"mcp==([\d.]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8")).group(1)
        step = (ROOT / "observatory/engine/observatory.py").read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r"mcp==([\d.]+)", step)), {pinned})


if __name__ == "__main__":
    unittest.main()
