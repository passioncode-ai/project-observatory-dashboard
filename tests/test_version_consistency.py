"""Every place that states the application version states the same one."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def found(path: str, pattern: str) -> str:
    match = re.search(pattern, (ROOT / path).read_text(encoding="utf-8"), re.M)
    assert match, f"no version in {path}"
    return match.group(1)


class VersionConsistency(unittest.TestCase):
    def test_one_version_everywhere(self):
        versions = {
            "pyproject.toml": found("pyproject.toml", r'^version = "([\d.]+)"'),
            "observatory/__init__.py": found("observatory/__init__.py", r'^__version__ = "([\d.]+)"'),
            "engine configuration": found("observatory/engine/configuration.py", r'^VERSION = "([\d.]+)"'),
            "docs/COMPATIBILITY.md": found("docs/COMPATIBILITY.md", r"application release is \*\*([\d.]+)\*\*"),
            "engine COMPATIBILITY": found("observatory/engine/docs/COMPATIBILITY.md", r"application release is \*\*([\d.]+)\*\*"),
        }
        self.assertEqual(len(set(versions.values())), 1, versions)

    def test_no_other_module_states_its_own_version(self):
        # A second literal drifts: tools/serverd.py said 0.1.0 until 0.3.4.
        stray = [str(p.relative_to(ROOT)) for p in (ROOT / "observatory/engine").rglob("*.py")
                 if p.name != "configuration.py"
                 and re.search(r'(?m)^(?:VERSION|__version__) = "[\d.]+"', p.read_text(encoding="utf-8"))]
        self.assertEqual(stray, [])

    def test_changelog_names_the_current_version(self):
        version = found("pyproject.toml", r'^version = "([\d.]+)"')
        self.assertRegex((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), rf"(?m)^## {re.escape(version)} ")

    def test_no_release_heading_was_overwritten(self):
        # 0.19.4's version bump replaced the `## 0.19.3` line instead of adding one above it,
        # and 0.19.3's notes sat under 0.19.4 unnoticed: within a minor version the patch
        # headings run without a gap and without a repeat.
        import collections
        heads = re.findall(r"(?m)^## (\d+)\.(\d+)\.(\d+) ", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        by = collections.defaultdict(list)
        for major, minor, patch in heads:
            by[(int(major), int(minor))].append(int(patch))
        for minor, patches in by.items():
            self.assertEqual(sorted(patches), list(range(min(patches), max(patches) + 1)), minor)


if __name__ == "__main__":
    unittest.main()
