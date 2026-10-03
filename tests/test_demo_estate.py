"""tools/demo_estate.py builds the dashboard over a fictional estate without
reading the invoking user's workspace: its OBSERVATORY_HOME is its own."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DemoEstate(unittest.TestCase):
    def test_demo_uses_its_own_home_and_renders_both_languages(self):
        base = Path(tempfile.mkdtemp(prefix="observatory-demo-")).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        # A "real" workspace the demo must not read: a Russian locale it would pick up.
        real = base / "real-home"
        (real / "config").mkdir(parents=True)
        (real / "config/settings.json").write_text(
            '{"schema_version": 1, "sources": {}, "integrations": {}, "features": {}, '
            '"interface": {"locale": "ru"}}')
        env = {**os.environ, "OBSERVATORY_HOME": str(real)}
        env.pop("OBSERVATORY_LOCALE", None)
        for locale in ("en", "ru"):
            out = base / f"demo-{locale}"
            done = subprocess.run([sys.executable, str(ROOT / "tools/demo_estate.py"), str(out), "--locale", locale],
                                  env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(done.returncode, 0, done.stderr[-600:])
            page = (out / "pages/index.html").read_text(encoding="utf-8")
            self.assertIn(f'<html lang="{locale}"', page)
            projects = (out / "pages/projects.html").read_text(encoding="utf-8")
            self.assertIn("Atlas Billing", projects)
            self.assertIn("northwind-labs", projects)
            self.assertNotIn(str(real), page, "the demo read the invoking user's workspace")
        refused = subprocess.run([sys.executable, str(ROOT / "tools/demo_estate.py"), str(base / "demo-en")],
                                 env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(refused.returncode, 2, "a non-empty output directory is refused")


    def test_no_page_carries_the_builders_home_or_workspace(self):
        """The demo exists for screenshots, and its empty states printed the
        builder's machine: `configure sources mcp_config_root '<$HOME>'` on the
        MCP page and `OBSERVATORY_HOME='<OUT>/home'` in the Heroku, Traffic and
        MCP commands, against the module's own promise that nothing is read from
        the machine running it. Built here under a distinctive HOME, and every
        page is searched for it and for the output directory."""
        base = Path(tempfile.mkdtemp(prefix="observatory-demo-paths-")).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        home = base / "builder-home-q7zx"
        home.mkdir()
        out = base / "demo-out-k3wv"
        env = {**os.environ, "HOME": str(home), "OBSERVATORY_HOME": str(home / "workspace")}
        env.pop("OBSERVATORY_LOCALE", None)
        done = subprocess.run([sys.executable, str(ROOT / "tools/demo_estate.py"), str(out)],
                              env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr[-600:])
        pages = sorted((out / "pages").iterdir()) + [out / "page.html"]
        self.assertGreater(len(pages), 5)
        needles = {"builder-home-q7zx", "demo-out-k3wv", str(base)}
        for page in pages:
            text = page.read_text(encoding="utf-8", errors="replace")
            for needle in needles:
                self.assertNotIn(needle, text, f"{page.name} carries {needle!r}")
        mcp = (out / "pages/mcp.html").read_text(encoding="utf-8")
        self.assertIn("project-observatory full", mcp)


if __name__ == "__main__":
    unittest.main()
