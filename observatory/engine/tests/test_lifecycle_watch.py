#!/usr/bin/env python3
"""Watching the lifecycle contract hold: the collector and its findings.

The contract's "Watching it hold" section: orphaned product processes, per-session
servers running replaced code, launchd jobs running longer than their interval and
logs past their cap, each reported against the product that owns it.

Every input is a fixture: an invented process table, an invented launchd map,
plists and logs written into a temporary HOME. `snapshot()` (the only function
that runs `ps` and `launchctl`) is never called here.
"""
from __future__ import annotations

import importlib
import json
import os
import plistlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for sub in ("collectors", "tools", "dashboard"):
    sys.path.append(str(ROOT / sub))

NOW = 2_000_000_000.0
LABEL_TICK = "org.project-observatory.0123456789abcdef.tick"
LABEL_SERVER = "org.project-observatory.0123456789abcdef.server"


class Watch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "workspace"
        self.user = self.base / "user"
        self.user.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OBSERVATORY_", "FABRIC_"))}
        env.update(OBSERVATORY_HOME=str(self.home), HOME=str(self.user),
                   FABRIC_SERVICES_DIR=str(self.base / "services"))
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        import workspace
        workspace.initialize(self.home)
        import paths
        self.paths = importlib.reload(paths)
        import scan_lifecycle
        self.sl = importlib.reload(scan_lifecycle)
        import lifecycle_findings
        self.lf = importlib.reload(lifecycle_findings)
        self.engine = str(ROOT)
        self.agents = self.user / "Library/LaunchAgents"
        self.agents.mkdir(parents=True)
        self.logs = self.paths.STATE / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def plist(self, label: str, **keys) -> None:
        (self.agents / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label, **keys}))

    def proc(self, pid, ppid, exe, script="", elapsed=60):
        return {"pid": pid, "ppid": ppid, "elapsed_s": elapsed, "exe": exe, "script": script}

    def evaluate(self, processes, launchd, config=None, descriptors=()):
        catalogue = self.sl.catalogue(config if config is not None else self.sl.load_config(),
                                      list(descriptors))
        snap = {"now": NOW, "processes": processes, "launchd": launchd}
        return self.sl.evaluate(snap, catalogue)

    def kinds(self, doc):
        return sorted((o["kind"], o["product"]) for o in doc["observations"])


class Orphans(Watch):
    def test_a_product_child_whose_parent_died_is_an_orphan(self):
        doc = self.evaluate([
            self.proc(10, 1, "/usr/bin/python3", f"{self.engine}/collectors/scan_fs.py"),
            self.proc(11, 7, "/usr/bin/python3", f"{self.engine}/collectors/scan_gh.py"),
        ], {})
        self.assertEqual(self.kinds(doc), [("orphan", "Project Observatory")])
        self.assertEqual(doc["observations"][0]["pid"], 10)

    def test_a_launchd_job_is_not_an_orphan(self):
        doc = self.evaluate([self.proc(20, 1, "/usr/bin/python3", f"{self.engine}/tools/serverd.py")],
                            {20: LABEL_SERVER})
        self.assertEqual(doc["observations"], [])

    def test_a_process_of_no_product_is_not_ours_to_report(self):
        doc = self.evaluate([self.proc(30, 1, "/usr/local/bin/node", "/opt/other/server.js")], {})
        self.assertEqual(doc["observations"], [])

    def test_a_session_server_left_by_its_session_is_an_orphan(self):
        doc = self.evaluate([self.proc(40, 1, "/usr/bin/python3", f"{self.engine}/mcp/server.py")], {})
        self.assertIn(("orphan", "Project Observatory"), self.kinds(doc))


class StaleSessionServers(Watch):
    def config_with_marker(self, marker: Path) -> dict:
        return {"products": [{"product": "Alpha Agent", "session_servers": [
            {"match": "/alpha/mcp/server.js", "code": str(marker)}]}]}

    def test_a_server_started_before_its_code_was_replaced_is_stale(self):
        marker = self.base / "alpha-version"
        marker.write_text("2")
        os.utime(marker, (NOW - 30, NOW - 30))
        doc = self.evaluate([self.proc(50, 9, "/usr/local/bin/node", "/opt/alpha/mcp/server.js", elapsed=600),
                             self.proc(51, 9, "/usr/local/bin/node", "/opt/alpha/mcp/server.js", elapsed=10)],
                            {}, config=self.config_with_marker(marker))
        stale = [o for o in doc["observations"] if o["kind"] == "stale-code"]
        self.assertEqual([o["pid"] for o in stale], [50])
        self.assertEqual(stale[0]["product"], "Alpha Agent")

    def test_a_server_whose_code_is_gone_is_stale(self):
        doc = self.evaluate([self.proc(52, 9, "/usr/local/bin/node", "/opt/alpha/mcp/server.js")],
                            {}, config=self.config_with_marker(self.base / "missing"))
        self.assertEqual([o["kind"] for o in doc["observations"]], ["stale-code"])


class Overruns(Watch):
    def test_a_job_running_longer_than_its_interval_is_reported(self):
        self.plist(LABEL_TICK, StartInterval=1800)
        doc = self.evaluate([self.proc(60, 1, "/usr/bin/python3", f"{self.engine}/tools/tick_lease.py",
                                       elapsed=2400)], {60: LABEL_TICK})
        self.assertEqual(self.kinds(doc), [("overrun", "Project Observatory")])
        o = doc["observations"][0]
        self.assertEqual((o["interval_s"], o["elapsed_s"], o["label"]), (1800, 2400, LABEL_TICK))

    def test_a_job_inside_its_interval_is_quiet(self):
        self.plist(LABEL_TICK, StartInterval=1800)
        doc = self.evaluate([self.proc(61, 1, "/usr/bin/python3", f"{self.engine}/tools/tick_lease.py",
                                       elapsed=600)], {61: LABEL_TICK})
        self.assertEqual(doc["observations"], [])

    def test_a_keepalive_job_has_no_interval_to_overrun(self):
        self.plist(LABEL_SERVER, KeepAlive=True)
        doc = self.evaluate([self.proc(62, 1, "/usr/bin/python3", f"{self.engine}/tools/serverd.py",
                                       elapsed=90000)], {62: LABEL_SERVER})
        self.assertEqual(doc["observations"], [])


class Logs(Watch):
    def test_a_log_past_its_cap_and_a_readable_log_are_reported(self):
        big = self.logs / "serverd.err"
        big.write_bytes(b"x" * 2048)
        big.chmod(0o600)
        open_log = self.logs / "gate.log"
        open_log.write_text("x")
        open_log.chmod(0o644)
        doc = self.evaluate([], {}, config={"log_cap_bytes": 1024})
        got = sorted((o["kind"], Path(o["file"]).name) for o in doc["observations"])
        self.assertEqual(got, [("log-over-cap", "serverd.err"), ("log-readable", "gate.log")])

    def test_a_service_descriptor_brings_its_own_logs(self):
        other = self.base / "beta-logs"
        other.mkdir()
        (other / "beta.log").write_bytes(b"y" * 4096)
        (other / "beta.log").chmod(0o600)
        desc = {"id": "example.beta", "name": "Beta Service",
                "lifecycle": {"manager": "launchd", "label": "com.example.beta"},
                "paths": {"logs": [str(other / "beta.log")]}}
        doc = self.evaluate([], {}, config={"log_cap_bytes": 1024}, descriptors=[desc])
        self.assertEqual(self.kinds(doc), [("log-over-cap", "Beta Service")])


class Findings(Watch):
    def test_each_kind_becomes_one_finding_per_product(self):
        doc = {"measured_at": "2033-05-18T03:33:20Z", "observations": [
            {"kind": "orphan", "product": "Project Observatory", "slug": "project-observatory",
             "pid": 10, "process": "scan_fs.py", "elapsed_s": 60},
            {"kind": "orphan", "product": "Project Observatory", "slug": "project-observatory",
             "pid": 11, "process": "scan_gh.py", "elapsed_s": 60},
            {"kind": "stale-code", "product": "Alpha Agent", "slug": "alpha-agent", "pid": 50,
             "process": "server.js", "started_at": "x", "code_changed_at": "y"},
            {"kind": "overrun", "product": "Project Observatory", "slug": "project-observatory",
             "label": LABEL_TICK, "pid": 60, "elapsed_s": 2400, "interval_s": 1800},
            {"kind": "log-over-cap", "product": "Beta Service", "slug": "beta-service",
             "file": "~/x/beta.log", "bytes": 4096, "cap": 1024},
            {"kind": "log-readable", "product": "Beta Service", "slug": "beta-service",
             "file": "~/x/gate.log", "mode": "0644"},
        ]}
        out = self.lf.findings(doc)
        got = sorted((f["type"], f["subject"]) for f in out)
        self.assertEqual(got, [
            ("lifecycle.log_readable", "product:beta-service"),
            ("lifecycle.logs_over_cap", "product:beta-service"),
            ("lifecycle.orphans", "product:project-observatory"),
            ("lifecycle.overrun", "product:project-observatory"),
            ("lifecycle.stale_servers", "product:alpha-agent"),
        ])
        orphans = next(f for f in out if f["type"] == "lifecycle.orphans")
        self.assertEqual(orphans["title_args"]["n"], 2)
        for f in out:
            self.assertTrue(f["title"] and f["action"] and f["severity"] in ("info", "warning", "critical"))

    def test_every_type_has_a_label_and_every_title_a_translation(self):
        import finding_types
        import i18n
        for t in self.lf.TYPES:
            self.assertIn(t, finding_types.LABELS)
        ru = json.loads((ROOT / "dashboard/locales/ru.json").read_text())
        for msgid in self.lf.TITLES:
            self.assertIn(msgid, ru, msgid)
            self.assertNotIn("{", i18n.translate(msgid, "en", n=2, product="x"))
            self.assertNotIn("{", i18n.translate(msgid, "ru", n=2, product="x"))

    def test_the_builder_reads_the_collectors_file(self):
        src = (ROOT / "tools/build_findings.py").read_text()
        self.assertIn("lifecycle_findings", src)
        self.assertIn('"lifecycle.json"', src)

    def test_the_tick_runs_the_collector(self):
        body = (ROOT / "tools/tick.py").read_text()
        self.assertIn("collectors/scan_lifecycle.py", body)


class Catalogue(Watch):
    def test_the_engine_names_itself_from_its_own_paths(self):
        cat = self.sl.catalogue(self.sl.load_config(), [])
        own = next(p for p in cat if p["product"] == "Project Observatory")
        self.assertTrue(any(c.startswith(self.engine) for c in own["children"]))
        self.assertIn(str(self.paths.STATE / "logs"), own["logs"])

    def test_the_collector_writes_no_command_line(self):
        doc = self.evaluate([self.proc(10, 1, "/usr/bin/python3", f"{self.engine}/collectors/scan_fs.py")], {})
        text = json.dumps(doc)
        self.assertNotIn("--token", text)
        self.assertNotIn(str(self.user), text, "a home path reached the document")

    def test_redaction_keeps_the_script_and_drops_the_arguments(self):
        exe, script = self.sl.redact("/usr/bin/python3 /opt/x/tool.py --api-key=abc123 more")
        self.assertEqual((exe, script), ("/usr/bin/python3", "/opt/x/tool.py"))
        exe, script = self.sl.redact("/usr/local/bin/node --token sekrit /opt/y/server.js")
        self.assertEqual(script, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
