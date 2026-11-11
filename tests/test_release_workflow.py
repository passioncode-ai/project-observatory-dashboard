"""The release workflow publishes only after the manifest URL check (ENG-018).

`observatory/engine/tools/check_manifest_urls.py` is worth nothing if `publish` can run
without it: this pins the wiring, read as text (the root tests carry no YAML parser).
"""
from __future__ import annotations

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release.yml"


def job(text: str, name: str) -> str:
    """The body of one top-level job: from `  name:` to the next two-space key."""
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", text, re.M | re.S)
    return m.group(1) if m else ""


class ReleaseWorkflow(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_publish_waits_for_the_manifest_job(self):
        body = job(self.text, "publish")
        m = re.search(r"^    needs:\s*\[([^\]]*)\]", body, re.M)
        self.assertIsNotNone(m, "publish declares no needs list")
        self.assertIn("manifest", [x.strip() for x in m.group(1).split(",")])

    def test_the_manifest_job_runs_the_check_and_resolves_when_publishing(self):
        body = job(self.text, "manifest")
        self.assertIn("observatory/engine/tools/check_manifest_urls.py", body)
        self.assertIn('--release "$release"', body)
        self.assertIn("--resolve", body)


class WindowsHolderWorkflow(unittest.TestCase):
    def setUp(self):
        self.body = job((ROOT / ".github/workflows/check.yml").read_text(encoding="utf-8"), "windows")
        self.assertTrue(self.body)

    def step(self, name):
        match = re.search(rf"^      - id: {re.escape(name)}\n(.*?)(?=^      - |\Z)", self.body, re.M | re.S)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def test_holder_failure_is_not_suppressed_at_job_or_step_level(self):
        self.assertIsNone(re.search(r"^    continue-on-error:\s*true\s*$", self.body, re.M))
        strict = self.step("holder_safety")
        self.assertIsNone(re.search(r"^        continue-on-error:\s*true\s*$", strict, re.M))
        self.assertIn("if: always()", strict)
        self.assertIn("python tests/test_disk_guard.py -v WindowsHolderUnknown", strict)

    def test_organization_enrollment_is_enforced_on_windows(self):
        # OBS-201: the organization suite fails the job on Windows, and cannot pass by skipping
        # every case for want of the `full` extra.
        strict = self.step("org")
        self.assertIsNone(re.search(r"^        continue-on-error:\s*true\s*$", strict, re.M))
        self.assertIn("if: always()", strict)
        self.assertLess(strict.index('python -c "import cryptography"'), strict.index("python tests/test_node_org.py -v"))
        self.assertIn("steps.org.outcome", self.body)

    def test_no_windows_step_may_fail_unseen(self):
        # OBS-277: every step of the windows job is enforced; none is informative any more.
        self.assertIsNone(re.search(r"continue-on-error", self.body))
        for name in ("tests", "compile", "oslocks", "osproc", "osprivacy", "osschedule", "tick", "jobs", "portable"):
            self.assertIn("if: always()", self.step(name), name)
        self.assertIn("steps.holder_safety.outcome", self.body)

    def test_the_portable_set_is_held_to_the_known_list(self):
        portable = self.step("portable")
        self.assertIn("python tests/run_portable.py", portable)
        self.assertIn("python tools/check_windows_suites.py", portable)
        self.assertNotIn("|| true", portable)
        self.assertIn("steps.portable.outcome", self.body)


class DesktopReleaseGate(unittest.TestCase):
    """OBS-506: the release ships the desktop app, so the guard waits for its tests too."""

    def setUp(self):
        self.release = WORKFLOW.read_text(encoding="utf-8")
        self.desktop = (ROOT / ".github/workflows/desktop.yml").read_text(encoding="utf-8")

    def guard_workflows(self):
        body = job(self.release, "guard")
        m = re.search(r"^          workflows: \|\n((?:^            \S.*\n)+)", body, re.M)
        self.assertIsNotNone(m, "the guard's workflows input is not a block list")
        return [line.strip() for line in m.group(1).splitlines()]

    def test_the_guard_requires_check_and_desktop(self):
        self.assertEqual(self.guard_workflows(), ["check.yml", "desktop.yml"])

    def test_desktop_runs_on_the_release_tag(self):
        # Paths filters are not evaluated for tag pushes, so the tag starts desktop.yml on the
        # tagged commit beside release.yml; workflow_dispatch stays for the guard's fallback.
        on = re.search(r"^on:\n(.*?)(?=^\S)", self.desktop, re.M | re.S).group(1)
        self.assertRegex(on, r"(?m)^  push:\n    tags: \[\"v\[0-9\]\+\.\[0-9\]\+\.\[0-9\]\+\"\]$")
        self.assertRegex(on, r"(?m)^  workflow_dispatch:")
        release_on = re.search(r"^on:\n(.*?)(?=^\S)", self.release, re.M | re.S).group(1)
        tags = re.compile(r"tags: (\[.*\])")
        self.assertEqual(tags.search(on).group(1), tags.search(release_on).group(1))

    def test_desktop_builds_every_runner_the_release_ships(self):
        built = set(re.findall(r"^          - os: (\S+)$", self.desktop, re.M))
        for name in ("windows", "linux"):
            for runner in re.findall(r"runner: ([\w.-]+) \}", job(self.release, name)):
                self.assertIn(runner, built, f"{name}: {runner}")

    def test_desktop_artifacts_do_not_collide_across_architectures(self):
        self.assertIn("name: desktop-${{ runner.os }}-${{ runner.arch }}", self.desktop)


if __name__ == "__main__":
    unittest.main()
