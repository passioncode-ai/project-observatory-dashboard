#!/usr/bin/env python3
"""A whole tick runs through `tools/tick.py` on every OS (docs/design/WINDOWS-LINUX.md, W3).

The tick was a bash script; it is Python so that Windows runs it too. These cases run the REAL
tick — the supervisor, the lease, every step, the report — in a workspace of their own, and the
Windows CI job runs the same tick there. The wrapper `tools/tick.sh` still hands over to it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class WholeTick(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="observatory-tick-")
        cls.addClassCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        cls.home = base / "runtime"
        projects = base / "projects" / "example"
        projects.mkdir(parents=True)
        (projects / "package.json").write_text('{"name":"example"}\n', encoding="utf-8")
        cls.env = {**os.environ, "OBSERVATORY_HOME": str(cls.home), "OBSERVATORY_SYSTEM_SETUP": "0",
                   "HOME": str(base / "user"), "USERPROFILE": str(base / "user"),
                   "OBSERVATORY_TICK_CEILING_SECONDS": "900"}
        for key in ("OBSERVATORY_TICK_SUPERVISOR_PID", "OBSERVATORY_TICK_DEADLINE", "AGENT_SYNC_RUN_ID",
                    "OBSERVATORY_SCRATCH", "OBSERVATORY_STATE", "OBSERVATORY_REGISTRY", "OBSERVATORY_DB"):
            cls.env.pop(key, None)
        (base / "user").mkdir()
        cls.run_engine(sys.executable, "observatory.py", "init")
        settings = cls.home / "config/settings.json"
        doc = json.loads(settings.read_text(encoding="utf-8"))
        doc["sources"]["projects"] = str(projects.parent)
        doc.setdefault("features", {})["scheduler"] = True
        settings.write_text(json.dumps(doc), encoding="utf-8")
        cls.tick = cls.run_engine(sys.executable, "tools/tick.py", timeout=900)

    @classmethod
    def run_engine(cls, *argv, timeout=120) -> subprocess.CompletedProcess:
        p = subprocess.run(argv, cwd=ROOT, env=cls.env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        if argv[1] != "tools/tick.py" and p.returncode:
            raise RuntimeError(f"{argv[1]} failed: {(p.stdout + p.stderr)[-600:]}")
        return p

    def test_the_tick_finishes_and_exits_zero(self):
        out = self.tick.stdout + self.tick.stderr
        self.assertEqual(self.tick.returncode, 0, out[-1500:])
        lines = [line[21:] for line in self.tick.stdout.splitlines()]
        self.assertIn("tick start", lines, out[-1500:])
        self.assertIn("tick done", lines, out[-1500:])

    def test_it_ran_under_the_supervisor(self):
        # Started directly, it re-entered through `tick_lease.py run`, which is what takes the
        # workspace's tick lock and sets the ceiling; unsupervised it would not reach a step.
        self.assertIn("integrity: ", self.tick.stdout, self.tick.stdout[-800:])

    def test_every_line_carries_a_utc_stamp(self):
        for line in self.tick.stdout.splitlines():
            self.assertRegex(line, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ", line)

    def test_the_report_says_no_step_failed(self):
        report = json.loads((self.home / "store/raw/tick.json").read_text(encoding="utf-8"))
        self.assertEqual(report["failed_steps"], [], self.tick.stdout[-1500:])
        self.assertTrue(report["finished_at"].startswith("20"))

    def test_the_tail_built_the_page(self):
        self.assertIn("findings: ", self.tick.stdout)
        self.assertIn("dashboard: ", self.tick.stdout)


class Bail(unittest.TestCase):
    def test_a_bail_still_releases_the_lease(self):
        # `steps` stops through `bail` (SystemExit); the release sits in `run`'s `finally`.
        import importlib.util
        spec = importlib.util.spec_from_file_location("tick_under_test", ROOT / "tools/tick.py")
        tick = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tick)
        calls = []
        tick.lease = lambda verb: (calls.append(verb), (0, ""))[1]

        def stop(t, scratch, dashboard):
            t.bail("emit", 1, "emit REFUSED a wholesale change")
        tick.steps = stop
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit) as stopped:
            tick.run(tick.Tick(Path(tmp)), Path(tmp), Path(tmp) / "page.html")
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(calls, ["acquire", "release"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
