"""The lock follows the pins (OBS-17): a Dependabot bump of `pyproject.toml` that leaves
`requirements-full.lock` behind fails at `pip install -c`, on every CI row, with a resolver
error that does not say which file is stale. `tools/refresh_lock.py --check` says it first."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import refresh_lock  # noqa: E402

PYPROJECT = """
[project]
name = "example"
dependencies = []
[project.optional-dependencies]
full = ["mcp==2.2.0", "Google_Auth==2.58.1", "loose>=1"]
"""


class CheckTests(unittest.TestCase):
    def test_a_lock_in_step_with_the_pins_passes(self):
        lock = "# header\nmcp==2.2.0\ngoogle-auth==2.58.1\nanyio==4.15.1\n"
        self.assertEqual(refresh_lock.mismatches(PYPROJECT, lock), [])

    def test_a_bumped_pin_names_the_package_and_both_versions(self):
        lock = "mcp==2.1.0\ngoogle-auth==2.58.1\n"
        self.assertEqual(refresh_lock.mismatches(PYPROJECT, lock),
                         ["mcp: pyproject pins 2.2.0, requirements-full.lock has 2.1.0"])

    def test_a_pin_missing_from_the_lock_is_named(self):
        self.assertEqual(refresh_lock.mismatches(PYPROJECT, "google-auth==2.58.1\n"),
                         ["mcp: pyproject pins 2.2.0, requirements-full.lock has nothing"])

    def test_the_repository_lock_matches_its_pins(self):
        problems = refresh_lock.mismatches((ROOT / "pyproject.toml").read_text(encoding="utf-8"),
                                           (ROOT / "requirements-full.lock").read_text(encoding="utf-8"))
        self.assertEqual(problems, [], "run `python tools/refresh_lock.py`")


if __name__ == "__main__":
    unittest.main()
