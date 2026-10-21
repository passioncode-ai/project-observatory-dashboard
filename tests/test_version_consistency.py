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

    def test_release_headings_keep_one_format(self):
        # AGENTS.md prescribed the dash form `## X.Y.Z, dash, date` while 0.25.0 and later were written `## X.Y.Z (date)`, and
        # the version test accepted either. The parenthesised form is the one in use (and carries no em dash);
        # the older headings are history and keep theirs, so the dash form is accepted only up to 0.24.x.
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        wrong = []
        for line in re.findall(r"(?m)^## .*$", text):
            if line == "## Unreleased":
                continue
            m = re.match(r"## (\d+)\.(\d+)\.(\d+)(?: |$)", line)
            if not m:
                wrong.append(f"{line!r}: not `## Unreleased` and not a release heading")
                continue
            version = tuple(int(x) for x in m.groups())
            current = re.fullmatch(r"## \d+\.\d+\.\d+ \(\d{4}-\d{2}-\d{2}\)", line)
            # 0.1.0 has no date; 0.19.2 adds why it was not published.
            historical = re.fullmatch(r"## \d+\.\d+\.\d+(?: \u2014 \d{4}-\d{2}-\d{2}(?: \([^()]+\))?)?", line)
            if not (current or (version < (0, 25, 0) and historical)):
                wrong.append(f"{line!r}: write `## X.Y.Z (YYYY-MM-DD)`")
        self.assertEqual(wrong, [])

    def test_agents_md_prescribes_the_heading_in_use(self):
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue("`## X.Y.Z (YYYY-MM-DD)`" in agents, "AGENTS.md must prescribe `## X.Y.Z (YYYY-MM-DD)`")
        self.assertFalse("`## X.Y.Z \u2014 date`" in agents, "AGENTS.md still prescribes the dash form")

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
