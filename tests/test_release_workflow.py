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


if __name__ == "__main__":
    unittest.main()
