#!/usr/bin/env python3
"""Messages name the configuration file where a workspace actually keeps it.

The engine reads curated files through `paths.config_file(name)`, which is
`<workspace>/config/<name>`. The predecessor kept them in `collectors/` and a
GA4 mapping in `plugins/config/`, and messages written then still told the
operator to edit files that do not exist in an installed engine. These checks
run against a synthetic workspace initialised in a temporary directory.
"""
from __future__ import annotations
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

#: A curated file named at a location an installed engine does not have.
STALE = re.compile(r"collectors/(?:…\s*)?[a-z_]+\.json|plugins/config/[a-z_.]+\.json"
                   r"|plugins/ga4_properties\.json")

#: Files that name the old layout on purpose, each with its reason.
ALLOWED = {
    # `migrate-local` reads an ORIGINAL installation, which has that layout.
    "workspace.py": "reads a legacy installation's own files during migration",
    # A development tracer that maps the predecessor's paths to trace them.
    "tools/trace_opens.py": "maps legacy file opens for the tracer; not an operator message",
}

PROBE = r"""
import json, sys
sys.path.insert(0, 'tools')
import paths, build_findings
projects = [{"name": "alpha-web", "lifecycle": "active", "activity_tier": "dormant"}]
rows = build_findings.declared_alive_measured_dead(projects)
print(json.dumps({"label": paths.config_label("project_overrides.json"),
                  "file": str(paths.config_file("project_overrides.json")),
                  "home": str(paths.HOME), "action": rows[0]["action"],
                  "mcp": build_findings.mcp_registration_command(),
                  "python": sys.executable}))
"""


def _strings_outside_docstrings(tree: ast.AST) -> list[ast.Constant]:
    docs = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)):
            docs.add(id(body[0].value))
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


class ConfigLocations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="observatory-config-locations-")
        base = Path(cls.tmp.name).resolve()
        (base / "user").mkdir()
        cls.home = base / "home"
        cls.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(base / "user"),
                   "LANG": "C.UTF-8", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1",
                   "OBSERVATORY_HOME": str(cls.home)}
        init = subprocess.run([sys.executable, "observatory.py", "init"], cwd=ROOT, env=cls.env,
                              capture_output=True, text=True, timeout=120)
        if init.returncode:
            raise AssertionError("synthetic workspace did not initialise: " + init.stderr[-300:])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def probe(self) -> dict:
        p = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr[-500:])
        return json.loads(p.stdout)

    def test_the_label_is_the_config_file_relative_to_the_workspace(self):
        got = self.probe()
        self.assertEqual(got["label"], "config/project_overrides.json")
        self.assertEqual(Path(got["file"]), Path(got["home"]) / got["label"])
        self.assertTrue((self.home / got["label"]).is_file(), "init seeds the file the label names")

    def test_a_finding_action_names_the_real_location(self):
        action = self.probe()["action"]
        self.assertIn("config/project_overrides.json", action)
        self.assertNotIn("collectors/", action)

    def test_the_mcp_registration_line_runs_as_written(self):
        # The first scan raises `mcp.own_unregistered`; its action is the line a
        # person pastes, so it names this interpreter, this engine and this
        # workspace rather than a checkout layout an installed engine lacks.
        import shlex
        got = self.probe()
        argv = shlex.split(got["mcp"])
        self.assertEqual(argv[:6], ["claude", "mcp", "add", "observatory", "--scope", "user"])
        self.assertEqual(argv[6:8], ["-e", "OBSERVATORY_HOME=" + got["home"]])
        self.assertEqual(argv[8], "--")
        self.assertEqual(argv[9], got["python"])
        self.assertEqual(Path(argv[10]), ROOT / "mcp/server.py")
        self.assertTrue(Path(argv[10]).is_file())

    def test_no_operator_message_names_the_predecessor_layout(self):
        hits = []
        for path in sorted(ROOT.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith("tests/") or "__pycache__" in path.parts or rel in ALLOWED:
                continue
            for node in _strings_outside_docstrings(ast.parse(path.read_text(encoding="utf-8"))):
                if STALE.search(node.value):
                    hits.append(f"{rel}:{node.lineno}: {STALE.search(node.value).group(0)}")
        self.assertEqual(hits, [])

    def test_the_ga4_plugin_points_at_a_file_that_exists(self):
        source = (ROOT / "plugins/ga4_analytics.py").read_text(encoding="utf-8")
        doc = ast.get_docstring(ast.parse(source)) or ""
        self.assertNotIn("ga4_properties.example.json", doc)
        self.assertNotIn("plugins/config/", doc)
        self.assertIn("config/ga4_properties.json", doc)
        self.assertIn("defaults/ga4_properties.json", doc)
        self.assertTrue((ROOT / "defaults/ga4_properties.json").is_file())
        self.assertTrue((self.home / "config/ga4_properties.json").is_file(),
                        "init seeds the mapping the plugin reads")
        self.assertIn('paths.config_file("ga4_properties.json")', source)


if __name__ == "__main__":
    unittest.main()
