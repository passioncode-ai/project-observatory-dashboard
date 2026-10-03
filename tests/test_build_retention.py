"""Builds clean up after themselves (lifecycle LC-15), with synthetic build output only.

Nothing here builds a wheel or an app, or calls the real `lsregister`: the
release artefacts are empty files and folders with release-shaped names in a
temporary directory, and LaunchServices is a recorder.
"""
from __future__ import annotations
import importlib.util
import os
import plistlib
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("prune_builds", ROOT / "tools" / "prune_builds.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildRetention(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="observatory-retention-")
        self.root = Path(self.temp.name).resolve()
        self.dist = self.root / "dist"
        self.dist.mkdir()
        self.mod = load()

    def tearDown(self):
        self.temp.cleanup()

    def build(self, version: str) -> None:
        """What one release build leaves in dist/: a wheel and an sdist."""
        (self.dist / f"project_observatory-{version}-py3-none-any.whl").write_bytes(b"w")
        (self.dist / f"project_observatory-{version}.tar.gz").write_bytes(b"s")

    def test_at_most_two_releases_remain_after_builds(self):
        for version in ("0.11.0", "0.12.0", "0.13.0"):
            self.build(version)
            self.mod.prune_dist(self.dist)
        left = sorted(p.name for p in self.dist.iterdir())
        self.assertEqual(left, ["project_observatory-0.12.0-py3-none-any.whl", "project_observatory-0.12.0.tar.gz",
                                "project_observatory-0.13.0-py3-none-any.whl", "project_observatory-0.13.0.tar.gz"])

    def test_versions_sort_numerically(self):
        for version in ("0.9.1", "0.10.0", "0.10.1"):
            self.build(version)
        self.mod.prune_dist(self.dist)
        self.assertFalse(any("0.9.1" in p.name for p in self.dist.iterdir()))

    def test_files_that_are_not_releases_are_left_alone(self):
        self.build("0.13.0")
        (self.dist / "SHA256SUMS").write_text("x")
        (self.dist / "notes.txt").write_text("x")
        self.mod.prune_dist(self.dist, keep=1)
        self.assertTrue((self.dist / "SHA256SUMS").exists() and (self.dist / "notes.txt").exists())

    def test_an_app_bundle_it_replaces_is_unregistered_first(self):
        out = self.dist / "macos"
        current = out / "Project Observatory.app"
        older = out / "Project Observatory 2.app"
        foreign = out / "Someone Else.app"
        for app, ident in ((current, "ai.passioncode.observatory"), (older, "ai.passioncode.observatory"),
                           (foreign, "com.example.other")):
            (app / "Contents").mkdir(parents=True)
            (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": ident}))
        forgotten = []
        removed = self.mod.prune_apps(out, current, unregister=forgotten.append)
        self.assertEqual(removed, [older])
        self.assertEqual(forgotten, [older])
        self.assertTrue(current.exists())
        self.assertTrue(foreign.exists(), "a bundle of another identifier is never touched")
        self.assertFalse(older.exists())

    def test_caches_past_their_cap_are_reported_and_cleaned_on_request(self):
        cache = self.root / "macos" / ".build"
        cache.mkdir(parents=True)
        (cache / "blob").write_bytes(b"x" * 4096)
        caps = {cache: 1024}
        over = self.mod.caches_over_cap(caps)
        self.assertEqual([c["path"] for c in over], [cache])
        self.mod.clean_caches(over)
        self.assertFalse(cache.exists())
        self.assertEqual(self.mod.caches_over_cap(caps), [])

    def test_the_build_script_prunes_and_unregisters(self):
        script = (ROOT / "macos" / "scripts" / "build-app.sh").read_text()
        self.assertIn("tools/prune_builds.py", script)
        self.assertLess(script.index("-u \"$APP\""), script.index("rm -rf \"$APP\""),
                        "the old bundle is forgotten by LaunchServices before it is deleted")

    def test_agents_md_names_the_outputs_the_cap_and_the_clean_command(self):
        text = (ROOT / "AGENTS.md").read_text()
        section = text[text.index("## Lifecycle"):]
        for needle in ("dist/", "macos/.build", "python tools/prune_builds.py --clean-caches", "LC-15"):
            self.assertIn(needle, section)


if __name__ == "__main__":
    unittest.main()
