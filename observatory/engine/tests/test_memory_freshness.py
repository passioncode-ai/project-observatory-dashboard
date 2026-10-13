"""OBS-87: cheap memory measurements keep independent freshness and unknown values."""
from __future__ import annotations
import contextlib
import io
import json
import subprocess
import sys
from datetime import datetime, timezone
from unittest.mock import patch
from test_machine import Base, ROOT


class MemoryFreshness(Base):
    def light(self, stamp="2026-10-13T10:00:00Z", swap=0):
        return {"schema_version": 1, "kind": "machine-memory", "attempted_at": stamp,
                "memory": {"attempted_at": stamp, "measured_at": stamp, "total_mb": 8192,
                           "free_mb": 2048, "swap_used_mb": swap, "degraded": []},
                "processes": {"attempted_at": stamp, "measured_at": stamp, "count": 1,
                              "groups": [], "top": [], "projects": [], "degraded": []}}

    def test_reader_uses_light_receipt_without_freshening_disk(self):
        import machine_view
        old = {"measured_at": "2026-10-12T10:00:00Z", "memory": {"swap_used_mb": 4096},
               "processes": {}, "disk": {"volume": {"free_gb": 3}}}
        (self.home / "store/raw/machine.json").write_text(json.dumps(old))
        (self.home / "store/raw/machine-memory.json").write_text(json.dumps(self.light()))
        result = machine_view.summary()
        self.assertEqual(result["memory"]["swap_used_mb"], 0)
        self.assertEqual(result["memory"]["measured_at"], "2026-10-13T10:00:00Z")
        self.assertEqual(result["measuredAt"], old["measured_at"])

    def test_findings_and_assistant_use_new_memory_without_redating_disk(self):
        import machine_findings
        from agent import assistant
        old = {"measured_at": "2026-10-12T10:00:00Z", "memory": {"swap_used_mb": 8192},
               "processes": {}, "disk": {"volume": {"free_gb": 3}}}
        scratch = self.home / "store/raw"
        (scratch / "machine.json").write_text(json.dumps(old))
        for swap in (0, None):
            (scratch / "machine-memory.json").write_text(json.dumps(self.light(swap=swap)))
            rows = machine_findings.findings(scratch, self.home / "store/logs", {},
                      now=datetime(2026, 10, 13, 10, 1, tzinfo=timezone.utc), auto_cleanup=False)
            self.assertNotIn("machine.memory_pressure", [r["type"] for r in rows])
            evidence = assistant.evidence()
            memory = [r for r in evidence["items"] if r["source"] == "store/raw/machine-memory.json"]
            self.assertEqual(len(memory), 1)
            self.assertEqual(memory[0]["facts"]["swap_used_mb"], swap)
            self.assertEqual(memory[0]["measured_at"], "2026-10-13T10:00:00Z")
            disk = next(r for r in evidence["items"] if r["source"] == "store/raw/machine.json")
            self.assertEqual(disk["measured_at"], old["measured_at"])
            self.assertNotIn("swap_used_mb", disk["facts"])
        (scratch / "machine.json").unlink()
        (scratch / "machine-memory.json").write_text(json.dumps(self.light(swap=8192)))
        rows = machine_findings.findings(scratch, self.home / "store/logs", {},
                      now=datetime(2026, 10, 13, 10, 1, tzinfo=timezone.utc), auto_cleanup=False)
        self.assertIn("machine.memory_pressure", [r["type"] for r in rows])
        self.assertTrue(any(r["source"] == "store/raw/machine-memory.json" for r in assistant.evidence()["items"]))

    def test_invalid_process_identity_and_huge_numeric_are_not_consumed(self):
        import memory_snapshot as ms
        light = self.light()
        light["memory"]["swap_used_mb"] = 10 ** 1000
        light["processes"]["groups"] = [{"origin": None, "rss_mb": 1, "processes": 1},
                                         {"origin": "example", "rss_mb": 1, "processes": "many"}]
        result = ms.select_sources({}, light)
        self.assertIsNone(result["memory"]["swap_used_mb"])
        self.assertEqual(result["processes"]["groups"], [])
        self.assertTrue(result["processes"]["degraded"])

    def test_same_second_failed_light_attempt_wins_over_full_success(self):
        import memory_snapshot as ms
        light = self.light(swap=None)
        light["memory"].update(measured_at=None, degraded=[{"source": "swap", "reason": "command-timeout"}])
        full = {"measured_at": light["attempted_at"], "memory": {"swap_used_mb": 0}}
        result = ms.select_sources(full, light, at=datetime(2026,10,13,10,0,tzinfo=timezone.utc))["memory"]
        self.assertIsNone(result["swap_used_mb"])
        self.assertIsNone(result["stale"])
        self.assertTrue(result["degraded"])

    def test_future_attempt_cannot_be_fresh_even_with_current_measurement(self):
        import memory_snapshot as ms
        light = self.light()
        light["memory"]["attempted_at"] = "2099-01-01T00:00:00Z"
        result = ms.select_sources({}, light, at=datetime(2026,10,13,10,0,tzinfo=timezone.utc))["memory"]
        self.assertIsNone(result["stale"])
        self.assertTrue(result["degraded"])

    def test_guard_timeout_publishes_failed_new_attempt_for_every_reader(self):
        import memory_snapshot as ms, disk_guard
        out = self.home / "store/raw/machine-memory.json"
        out.write_text(json.dumps(self.light()))
        with patch.object(disk_guard.subprocess, "run", side_effect=subprocess.TimeoutExpired("synthetic", 1)):
            status = disk_guard.refresh_memory(1)
        self.assertIn("timeout", status)
        memory = ms.read_sources()["memory"]
        self.assertIsNone(memory["swap_used_mb"])
        self.assertIsNone(memory["measured_at"])
        self.assertTrue(memory["degraded"])

    def test_older_finish_cannot_overwrite_newer_started_attempt(self):
        import memory_snapshot as ms
        out = self.home / "store/raw/machine-memory.json"
        older = ms.begin_attempt(out)
        newer = ms.begin_attempt(out)
        self.assertFalse(ms.finish_attempt(out, older, self.light(swap=8192)))
        provisional = json.loads(out.read_text())
        self.assertEqual(provisional["attempt_id"], newer)
        self.assertIsNone(provisional["memory"]["swap_used_mb"])
        self.assertTrue(ms.finish_attempt(out, newer, self.light(swap=0)))
        self.assertEqual(json.loads(out.read_text())["memory"]["swap_used_mb"], 0)

    def test_two_collectors_finishing_out_of_order_keep_newer_failure(self):
        import memory_snapshot as ms
        import threading
        from concurrent.futures import ThreadPoolExecutor
        out = self.home / "store/raw/machine-memory.json"
        started, release = threading.Event(), threading.Event()
        def collect(seconds):
            if seconds == 1:
                started.set()
                self.assertTrue(release.wait(3))
                return self.light(swap=8192)
            return ms.failed_attempt("synthetic-failure")
        with patch.object(ms, "collect", side_effect=collect), ThreadPoolExecutor(max_workers=2) as pool:
            older = pool.submit(ms.write, out, 1)
            self.assertTrue(started.wait(3))
            newer = pool.submit(ms.write, out, 2)
            self.assertEqual(newer.result(timeout=3)["publication"], "published")
            release.set()
            self.assertEqual(older.result(timeout=3)["publication"], "superseded")
        memory = ms.read_sources()["memory"]
        self.assertIsNone(memory["swap_used_mb"])
        self.assertIsNone(memory["measured_at"])
        self.assertIn("synthetic-failure", [r["reason"] for r in memory["degraded"]])

    def test_cli_declares_memory_only(self):
        import workspace
        parsed = workspace.machine_parser("machine").parse_args(["--memory-only"])
        self.assertTrue(parsed.memory_only)

    def test_collector_is_bounded_and_never_asks_for_args_or_disk(self):
        import memory_snapshot as ms
        outputs = {"hw.memsize": "8589934592", "vm.swapusage": "total = 0.00M used = 0.00M free = 0.00M",
                   "vm_stat": "Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free: 20.\nPages inactive: 5.\n",
                   "ps": "7 1024 /usr/bin/python3\n8 2048 /usr/bin/python3\n"}
        seen = []
        def command(args, **kwargs):
            seen.append((args, kwargs["timeout"]))
            key = args[-1] if args[0].endswith("sysctl") else args[0]
            return subprocess.CompletedProcess(args, 0, outputs[key], "")
        with patch.object(ms.sys, "platform", "darwin"), patch.object(ms.subprocess, "run", side_effect=command):
            result = ms.collect(seconds=2)
        self.assertEqual(result["memory"]["swap_used_mb"], 0)
        self.assertEqual(result["processes"]["count"], 2)
        self.assertEqual(result["processes"]["groups"][0]["rss_mb"], 3)
        self.assertTrue(all(0 < timeout <= 2 for _, timeout in seen))
        self.assertEqual({args[0].split('/')[-1] for args, _ in seen}, {"sysctl", "vm_stat", "ps"})
        self.assertNotIn("args=", json.dumps(seen))
        self.assertNotIn("/usr/bin/python3", json.dumps(result))

    def test_failed_commands_are_null_and_errors_do_not_leak_bodies(self):
        import memory_snapshot as ms
        with patch.object(ms.sys, "platform", "darwin"), patch.object(ms.subprocess, "run", side_effect=OSError("PRIVATE BODY")):
            result = ms.collect(seconds=1)
        self.assertIsNone(result["memory"]["swap_used_mb"])
        self.assertIsNone(result["memory"]["total_mb"])
        self.assertIsNone(result["processes"]["count"])
        self.assertIsNone(result["memory"]["measured_at"])
        self.assertTrue(result["memory"]["degraded"])
        self.assertNotIn("PRIVATE BODY", json.dumps(result))

    def test_failed_new_attempt_overrides_old_success_without_redating_it(self):
        import memory_snapshot as ms
        old = {"measured_at": "2026-10-13T09:00:00Z", "memory": {"swap_used_mb": 4096}, "processes": {}}
        light = self.light(swap=None)
        light["memory"].update(measured_at=None, degraded=[{"source": "swap", "reason": "command-unavailable"}])
        selected = ms.select_sources(old, light, at=datetime(2026, 10, 13, 10, 1, tzinfo=timezone.utc))
        self.assertIsNone(selected["memory"]["swap_used_mb"])
        self.assertIsNone(selected["memory"]["measured_at"])
        self.assertEqual(selected["memory"]["source"], "machine-memory.json")
        self.assertIsNone(selected["memory"]["stale"])

    def test_malformed_receipt_and_future_time_are_explicit(self):
        import memory_snapshot as ms
        at = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
        for receipt in (None, [], {"schema_version": 1, "kind": "machine-memory", "memory": []}):
            result = ms.select_sources({}, receipt, at=at)
            self.assertIsNone(result["memory"].get("swap_used_mb"))
            self.assertTrue(result["memory"]["degraded"])
        future = ms.select_sources({}, self.light(), at=at)
        self.assertIsNone(future["memory"]["stale"])
        self.assertTrue(future["memory"]["degraded"])

    def test_cli_mcp_and_page_preserve_unknown_swap_and_freshness(self):
        import memory_snapshot as ms, workspace, machine_view
        import machine_page
        sys.path.insert(0, str(ROOT / "mcp"))
        import server
        light = self.light(swap=None)
        light["memory"]["degraded"] = [{"source": "swap", "reason": "command-failed"}]
        with patch.object(ms, "collect", return_value=light), contextlib.redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(workspace.machine_command("machine", ["--memory-only"]), 0)
        self.assertIsNone(json.loads(captured.getvalue())["memory"]["swap_used_mb"])
        self.assertFalse((self.home / "store/raw/machine.json").exists())
        result = server.observatory_machine()
        self.assertIsNone(result["memory"]["swap_used_mb"])
        self.assertEqual(result["processes"]["measured_at"], light["processes"]["measured_at"])
        html = machine_page.machine_html({"machine": machine_view.summary()})
        self.assertIn("incomplete measurement", html)
        self.assertNotIn("swap 0.0 GB", html)

    def test_shorter_parent_deadline_does_not_spawn_commands(self):
        import memory_snapshot as ms
        import os, time
        with patch.object(ms.sys, "platform", "darwin"), patch.dict(os.environ, {"OBSERVATORY_STEP_DEADLINE": str(time.time()-1)}), \
                patch.object(ms.subprocess, "run") as command:
            result = ms.collect(seconds=8)
        command.assert_not_called()
        self.assertIsNone(result["memory"]["swap_used_mb"])
        self.assertTrue(result["partial"])

    def test_process_failures_do_not_freshen_older_process_success(self):
        import memory_snapshot as ms
        light = self.light()
        light["processes"].update(measured_at=None, count=None, groups=[None],
                                   degraded=[{"source": "ps", "reason": "command-timeout"}])
        result = ms.select_sources({}, light)
        self.assertEqual(result["processes"]["groups"], [])
        self.assertIsNone(result["processes"]["count"])
        self.assertIsNone(result["processes"]["stale"])
        self.assertTrue(result["processes"]["degraded"])

    def test_each_source_selects_its_own_timestamp(self):
        import memory_snapshot as ms
        full = {"measured_at": "2026-10-13T09:00:00Z", "memory": {"swap_used_mb": 4096},
                "processes": {"count": 10, "attempted_at": "2026-10-13T11:00:00Z",
                              "measured_at": "2026-10-13T11:00:00Z", "groups": []}}
        result = ms.select_sources(full, self.light(), at=datetime(2026,10,13,11,1,tzinfo=timezone.utc))
        self.assertEqual(result["memory"]["source"], "machine-memory.json")
        self.assertTrue(result["memory"]["stale"])
        self.assertEqual(result["processes"]["source"], "machine.json")
        self.assertFalse(result["processes"]["stale"])
        self.assertEqual(result["processes"]["count"], 10)

    def test_actual_collector_child_with_spent_budget_emits_receipt(self):
        import os
        output = self.home / "store/raw/child-memory.json"
        result = subprocess.run([sys.executable, "-B", str(ROOT / "collectors/scan_memory.py"),
                                 str(output), "--budget-seconds", "0"],
                                capture_output=True, text=True, env=os.environ.copy(), timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(output.read_text())
        self.assertEqual(doc["kind"], "machine-memory")
        self.assertTrue(doc["partial"])
        self.assertIsNone(doc["memory"]["total_mb"])
        self.assertIsNone(doc["processes"]["count"])

    def test_malformed_measurements_do_not_become_zero_or_crash(self):
        import memory_snapshot as ms
        def command(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "9" * 5000, "")
        with patch.object(ms.sys, "platform", "darwin"), patch.object(ms.subprocess, "run", side_effect=command):
            doc = ms.collect(seconds=2)
        self.assertIsNone(doc["memory"]["total_mb"])
        self.assertIsNone(doc["memory"]["swap_used_mb"])
        self.assertIsNone(doc["processes"]["count"])
        self.assertTrue(doc["partial"])


from test_disk_guard import Base as GuardBase


class GuardMemoryFreshness(GuardBase):
    def test_memory_refresh_precedes_all_planners_when_auto_is_disabled(self):
        import paths
        events = []
        def refresh(script, out, hours, timeout, args=()):
            events.append(script)
            if script == "collectors/scan_memory.py":
                self.assertLessEqual(timeout, 8)
                out.write_text(json.dumps(MemoryFreshness.light(self, stamp=self.g.now(), swap=None)))
            return "refreshed"
        def planned(*args):
            self.assertEqual(events[0], "collectors/scan_memory.py")
            return [(name, lambda budget: []) for name in self.g.CLASSES]
        with patch.object(self.g, "refresh_memory", self.real_refresh_memory), \
                patch.object(self.g, "refresh", side_effect=refresh), \
                patch.object(self.g, "load_config", return_value=self.cfg()), \
                patch.object(self.g.configuration, "enabled", return_value=False), \
                patch.object(self.g, "planners", side_effect=planned), \
                patch.object(self.g.Holders, "take", return_value=self.free()), \
                patch.object(self.g, "large_files", return_value=None):
            doc = self.g.run([])
        self.assertFalse(doc["acting"])
        self.assertEqual(doc["surveys"]["memory"], "refreshed")
        self.assertIsNone(doc["memory"]["swap_gb"])
        self.assertEqual(doc["memory"]["source"], "machine-memory.json")

    def test_failed_refresh_remains_visible_in_guard_degradation(self):
        with patch.object(self.g, "refresh_memory", return_value="timeout after 8 s"), \
                patch.object(self.g, "refresh", return_value="refreshed"), \
                patch.object(self.g, "load_config", return_value=self.cfg()), \
                patch.object(self.g, "planners", return_value=[(name, lambda budget: []) for name in self.g.CLASSES]), \
                patch.object(self.g.Holders, "take", return_value=self.free()), \
                patch.object(self.g, "large_files", return_value=None):
            doc = self.g.run([])
        self.assertIn({"source": "memory-refresh", "reason": "timeout after 8 s"}, doc["degraded"])


if __name__ == "__main__":
    import unittest
    unittest.main()
