#!/usr/bin/env python3
"""OBS-530: a command an agent session left reading an input nobody writes is found, named with its
session's folder, and only a leaf of the short list reading no file is stopped.

The process table, standard inputs, working folders and argument lists are injected; nothing reads
or signals a real process."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools"), str(ROOT / "collectors")]
import hung_commands as H

UID = 501
NOW = 1_800_000_000.0
CLAUDE = "/opt/agents/claude/versions/2.3.4"


def row(pid, ppid, exe, *, age=7200.0, cpu=0.01, uid=UID, pgid=None):
    return {"pid": pid, "ppid": ppid, "pgid": pgid if pgid is not None else pid, "uid": uid, "age_s": age, "cpu_s": cpu,
            "exe": exe}


def table(over=None):
    rows = {
        100: row(100, 1, CLAUDE, cpu=900.0),                 # the session
        200: row(200, 100, "/bin/zsh"),                      # its shell, waiting on 300
        300: row(300, 200, "wc", pgid=200),                  # the hung leaf, alone in its shell's group
        400: row(400, 100, "node", cpu=5.0),                 # a stdio MCP server: the agent's child
        410: row(410, 100, "npm", cpu=1.0),
        420: row(420, 410, "node"),                         # an MCP server started through npx
        500: row(500, 100, "sleep"),
        600: row(600, 100, "grep", age=600.0),               # too young
        700: row(700, 1, "cat"),                             # no agent above it
        800: row(800, 100, "python3", uid=502),              # another account's
    }
    rows.update(over or {})
    return rows


def previous(t, *, at=NOW - 900):
    return {"schema": H.SCHEMA, "at": __import__("datetime").datetime.fromtimestamp(at, __import__("datetime").timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "samples": {str(p): {"start": round(at - (r["age_s"] - (NOW - at))), "cpu_s": r["cpu_s"]} for p, r in t.items()}}


class Parsing(unittest.TestCase):
    def test_durations_in_every_ps_form(self):
        self.assertEqual(H.parse_duration("05:07"), 307)
        self.assertEqual(H.parse_duration("01:02:03"), 3723)
        self.assertEqual(H.parse_duration("1-01:02:03"), 90123)
        self.assertAlmostEqual(H.parse_duration("0:00.42"), 0.42)
        self.assertIsNone(H.parse_duration("x:1"))
        self.assertIsNone(H.parse_duration("1:2:3:4"))

    def test_the_table_keeps_the_full_command_name(self):
        out = H.parse_table("  300   200   200   501   1-09:00:00     0:00.01 /usr/bin/wc\n bad line\n")
        self.assertEqual((out[300]["exe"], out[300]["pgid"]), ("/usr/bin/wc", 200))
        self.assertEqual(out[300]["age_s"], 33 * 3600)

    def test_lsof_names_by_pid(self):
        self.assertEqual(H.parse_lsof("p300\nf0\nn/dev/ttys003\np301\nn->0x12\n"), {300: "/dev/ttys003", 301: "->0x12"})

    def test_an_input_is_classified_by_its_type(self):
        typed = H.parse_lsof_typed("p1\nf0\ntCHR\nn/dev/ttys003\np2\nf0\ntunix\nn->0xaa\np3\nf0\ntPIPE\nn->0xbb\n"
                                   "p4\nf0\ntCHR\nn/dev/null\n")
        self.assertEqual({p: H.classify(*v) for p, v in typed.items()},
                         {1: "terminal", 2: "socket", 3: "pipe", 4: "null"})
        self.assertEqual([H.classify("", n) for n in ("/dev/pts/2", "socket:[9]", "pipe:[9]", "/tmp/x")],
                         ["terminal", "socket", "pipe", "other"])

    def test_a_leaf_reads_no_file_only_when_nothing_could_be_one(self):
        self.assertTrue(H.reads_no_file("wc", ["-l"]))
        self.assertTrue(H.reads_no_file("cat", []))
        self.assertTrue(H.reads_no_file("grep", ["-i", "needle"]))
        self.assertTrue(H.reads_no_file("head", ["-n", "5"]))
        self.assertTrue(H.reads_no_file("grep", ["-e", "needle"]))
        self.assertFalse(H.reads_no_file("wc", ["-l", "notes.txt"]))
        self.assertFalse(H.reads_no_file("grep", ["needle", "file"]))
        self.assertFalse(H.reads_no_file("grep", ["-e", "needle", "file"]))


class AgentRule(unittest.TestCase):
    def test_the_agent_rule_is_scan_machines(self):
        try:
            import scan_machine
        except Exception as exc:  # noqa: BLE001 - a platform where the collector does not load
            self.skipTest(f"scan_machine does not load here ({type(exc).__name__})")
        for exe in (CLAUDE, "/usr/local/bin/claude", "/opt/agents/.codex/bin/codex", "codex",
                    "/Applications/Codex.app/Contents/Frameworks/Codex Framework.framework/x", "/bin/zsh", "wc", "node"):
            self.assertEqual(H._agent(exe), scan_machine.agent_of(exe), exe)


class Survey(unittest.TestCase):
    def survey(self, t, prev, stdin=None, args=None):
        with patch.object(H.os, "name", "posix"):
            return H.survey(prev, uid=UID, now=NOW, table=t,
                            stdin=stdin or (lambda pids: {p: "terminal" for p in pids}),
                            cwd=lambda pids: {p: "/srv/work/some-project" for p in pids},
                            args=args or (lambda pid: ["mcp-server-x", "--stdio"] if pid == 420 else ["-l"]))

    def test_the_rule_finds_only_the_hung_leaf(self):
        t = table()
        receipt = self.survey(t, previous(t))
        self.assertEqual([h["pid"] for h in receipt["hung"]], [300])
        h = receipt["hung"][0]
        self.assertEqual((h["name"], h["agent"], h["folder"], h["stdin"], h["leaf"]),
                         ("wc", "claude-code", "some-project", "terminal", True))
        self.assertEqual(set(receipt["samples"]), {"300", "420"}, "only this account's leaves below an agent's child are sampled")
        self.assertNotIn("/srv/work", json.dumps(receipt["hung"]), "a finding names the folder, never its path")

    def test_one_sample_is_not_enough(self):
        self.assertEqual(self.survey(table(), None)["hung"], [])

    def test_samples_too_close_together_say_nothing(self):
        t = table()
        self.assertEqual(self.survey(t, previous(t, at=NOW - 60))["hung"], [])

    def test_cpu_that_grew_is_not_hung(self):
        t = table()
        prev = previous(t)
        t[300] = dict(t[300], cpu_s=0.5)
        self.assertEqual(self.survey(t, prev)["hung"], [])

    def test_a_reused_pid_is_a_new_process(self):
        t = table()
        prev = previous(t)
        prev["samples"]["300"]["start"] -= 3600
        self.assertEqual(self.survey(t, prev)["hung"], [])

    def test_input_from_dev_null_is_not_hung(self):
        t = table()
        receipt = self.survey(t, previous(t), stdin=lambda pids: {p: "null" for p in pids})
        self.assertEqual(receipt["hung"], [])

    def test_unreadable_input_is_degraded_never_a_guess(self):
        t = table()
        receipt = self.survey(t, previous(t), stdin=lambda pids: None)
        self.assertEqual(receipt["hung"], [])
        self.assertEqual(receipt["degraded"][0]["source"], "lsof")

    def test_a_leaf_reading_a_pipe_is_reported_and_never_a_stop_candidate(self):
        """A watcher's `tail -f log | grep X | head -1` waits by design (measured 2026-11-13)."""
        t = table()
        receipt = self.survey(t, previous(t), stdin=lambda pids: {p: "pipe" for p in pids})
        self.assertEqual([(h["pid"], h["stdin"], h["leaf"]) for h in receipt["hung"]], [(300, "pipe", False)])

    def test_a_socket_from_the_agent_is_a_stop_candidate_when_nothing_else_lives_in_the_group(self):
        """The 2026-11-12 case: a background `wc -l` whose stdin is the agent's unix socket."""
        t = table()
        receipt = self.survey(t, previous(t), stdin=lambda pids: {p: "socket" for p in pids})
        self.assertEqual([(h["pid"], h["stdin"], h["alone"], h["leaf"]) for h in receipt["hung"]],
                         [(300, "socket", True, True)])

    def test_a_live_neighbour_in_the_group_makes_it_a_watcher(self):
        t = table({310: row(310, 200, "ugrep", cpu=0.3, pgid=200)})   # `... | ugrep | wc` in the same group
        receipt = self.survey(t, previous(t), stdin=lambda pids: {p: "socket" for p in pids})
        self.assertEqual([(h["pid"], h["alone"], h["leaf"]) for h in receipt["hung"]],
                         [(300, False, False), (310, False, False)], "both are named, neither is stopped")

    def test_a_leaf_with_a_file_is_reported_but_not_a_stop_candidate(self):
        t = table()
        receipt = self.survey(t, previous(t), args=lambda pid: ["-l", "big.log"])
        self.assertFalse(receipt["hung"][0]["leaf"])

    def test_another_command_is_reported_and_never_a_leaf(self):
        t = table({300: row(300, 200, "/opt/homebrew/bin/jq")})
        receipt = self.survey(t, previous(t))
        self.assertEqual([(h["name"], h["leaf"]) for h in receipt["hung"]], [("jq", False)])

    def test_an_unreadable_table_is_degraded(self):
        with patch.object(H, "read_table", lambda: None), patch.object(H.os, "name", "posix"):
            receipt = H.survey(None, uid=UID, now=NOW)
        self.assertEqual((receipt["hung"], receipt["degraded"][0]["source"]), ([], "ps"))


class Stopping(unittest.TestCase):
    def receipt(self, t, leaf=True):
        return {"samples": {"300": {"start": round(NOW - t[300]["age_s"])}},
                "hung": [{"pid": 300, "name": "wc", "leaf": leaf, "folder": "some-project"}]}

    def test_only_a_leaf_is_signalled_and_only_with_sigterm(self):
        t, sent = table(), []
        with patch.object(H.os, "name", "posix"):
            out = H.stop_leaves(self.receipt(t), table=t, kill=lambda pid, sig: sent.append((pid, sig)), now=NOW)
            self.assertEqual(sent, [(300, signal.SIGTERM)])
            self.assertEqual(out[0]["result"], "stopped")
            sent.clear()
            self.assertEqual(H.stop_leaves(self.receipt(t, leaf=False), table=t, kill=lambda *a: sent.append(a), now=NOW), [])
            self.assertEqual(sent, [])

    def test_a_pid_now_held_by_another_process_is_left_alone(self):
        t, sent = table(), []
        receipt = self.receipt(t)
        t[300] = row(300, 200, "wc", age=60.0)
        with patch.object(H.os, "name", "posix"):
            out = H.stop_leaves(receipt, table=t, kill=lambda *a: sent.append(a), now=NOW)
        self.assertEqual((sent, out[0]["result"]), ([], "gone"))

    def test_run_journals_a_stop_and_keeps_the_receipt_for_the_next_sample(self):
        with tempfile.TemporaryDirectory() as d:
            receipt_path, journal = Path(d) / "raw" / "hung.json", Path(d) / "logs" / "hung.jsonl"
            fake = {"schema": H.SCHEMA, "at": "2026-11-13T00:00:00Z", "samples": {}, "degraded": [],
                    "hung": [{"pid": 300, "name": "wc", "leaf": True}]}
            with patch.object(H, "survey", lambda prev: dict(fake)), \
                    patch.object(H, "stop_leaves", lambda r: [{"pid": 300, "name": "wc", "result": "stopped"}]):
                H.run(receipt_path, journal, apply=True)
                self.assertEqual(json.loads(journal.read_text())["result"], "stopped")
                H.run(receipt_path, journal, apply=False)
            self.assertEqual(len(journal.read_text().splitlines()), 1, "without --apply nothing is stopped or journalled")
            self.assertEqual(json.loads(receipt_path.read_text())["schema"], H.SCHEMA)


class Finding(unittest.TestCase):
    def test_hung_commands_are_a_finding_with_their_folder(self):
        import machine_findings as F
        receipt = {"hung": [{"pid": 300, "name": "wc", "age_hours": 33.0, "agent": "claude-code", "folder": "some-project"}]}
        found = F.hung_findings(receipt)
        self.assertEqual([(f["type"], f["severity"]) for f in found], [("machine.hung_commands", "warning")])
        self.assertIn("some-project", found[0]["detail"])
        receipt["stopped"] = [{"pid": 300, "result": "stopped"}]
        self.assertEqual(F.hung_findings(receipt)[0]["severity"], "info")
        piped = {"hung": [{"pid": 9, "name": "head", "stdin": "pipe", "folder": "some-project"}]}
        crowded = {"hung": [{"pid": 9, "name": "head", "stdin": "socket", "alone": False}]}
        self.assertEqual(F.hung_findings(crowded)[0]["severity"], "info")
        self.assertEqual(F.hung_findings(piped)[0]["severity"], "info", "a pipe reader may be a watcher")
        self.assertIn("never stopped", F.hung_findings(piped)[0]["detail"])
        self.assertEqual(F.hung_findings(None), [])
        self.assertEqual(F.hung_findings({"hung": ["x", {}]}), [])

    def test_the_type_has_a_label(self):
        sys.path.insert(0, str(ROOT / "dashboard"))
        import finding_types
        self.assertIn("machine.hung_commands", finding_types.LABELS)


class Maintenance(unittest.TestCase):
    def test_the_pass_stops_only_with_auto_cleanup(self):
        import maintenance
        calls = []

        def survey(receipt, journal, apply):
            calls.append(apply)
            return {"hung": [{"pid": 1}], "stopped": [{"result": "stopped"}] if apply else []}
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            out = maintenance.step_hung(base, None, survey)
            self.assertEqual((out["result"], out["hung"], out["stopped"], calls), ("ran", 1, 0, [False]))
            (base / "config").mkdir()
            (base / "config" / "settings.json").write_text(json.dumps({"features": {"auto_cleanup": True}}))
            self.assertEqual(maintenance.step_hung(base, None, survey)["stopped"], 1)
            self.assertEqual(calls, [False, True])
            self.assertEqual(maintenance.step_hung(base, None, lambda *a: 1 / 0)["result"], "error")


if __name__ == "__main__":
    unittest.main()
