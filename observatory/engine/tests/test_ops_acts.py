#!/usr/bin/env python3
"""OBS-559, next wave: the operations verbs (backup, disk, update, maintenance) as one MCP tool, `observatory_ops`.

Each act reaches an agent through `ops_acts.py`, which builds the CLI's own argument list and runs the CLI's own
function (`workspace.main`, `workspace.machine_command`, or for an update the engine's CLI in a process of its own) in
a detached job, never a second implementation. The tests drive every act with an injected job start and an injected
CLI, so nothing here takes a backup, frees disk, installs a release or touches the scheduler. They check: the argument
list each act builds; acts that delete or are hard to undo (backups migrate and decrypt, cleanup and disk guard
applied, an update) need the operator's words in contract DEC-0121's shape; a decrypted backup is written only inside
the workspace; refusals are typed; the journal row names the agent and the decision; the job's answer carries the
CLI's JSON scrubbed, and a fake key value never appears in an answer, a refusal or the journal.
"""
from __future__ import annotations
import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

FAKE_KEY = "sk-or-v1-" + "FAKE" * 16


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def words(action: str, target: str, **extra) -> dict:
    return {"by": "operator", "via": "relayed-instruction", "action": action, "target": target,
            "instruction": "do it", "at": now(), **extra}


class Starter:
    """`jobs.start` as the tool sees it: records the capability and arguments, runs `prepared`, never spawns."""

    def __init__(self, busy: bool = False, status: str = "working"):
        self.calls: list[tuple] = []
        self.busy, self.status = busy, status

    def __call__(self, capability, span_record, *, arguments=None, prepared=None, exclusive=True):
        import jobs
        if self.busy:
            raise jobs.JobBusy("busy")
        doc = {"id": "job-" + "a" * 32, "capability": capability, "status": self.status,
               "arguments": arguments, "trace": span_record}
        if self.status != "working":
            doc["error"] = {"code": "runner-failed", "message": "OSError"}
        if prepared:
            prepared(doc)
        self.calls.append((capability, arguments, exclusive))
        return doc, False


class OpsActs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        (self.home / "restore").mkdir(parents=True)
        import ops_acts
        self.ops = ops_acts
        self.start = Starter()
        self.journal_file = self.base / "logs" / "ops-acts.jsonl"
        self.patches = [patch.object(ops_acts, "_home", lambda: self.home),
                        patch.object(ops_acts, "journal_path", lambda: self.journal_file),
                        patch.dict(os.environ, {}, clear=False)]
        for p in self.patches:
            p.start()
        os.environ.pop("OBSERVATORY_SYSTEM_SETUP", None)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def act(self, action, **kw):
        return self.ops.act(action, start=self.start, **kw)

    def journal(self) -> list[dict]:
        if not self.journal_file.exists():
            return []
        return [json.loads(line) for line in self.journal_file.read_text(encoding="utf-8").splitlines() if line]

    def code(self, answer: dict) -> str:
        self.assertEqual(answer.get("degraded"), [], answer)
        return (answer.get("error") or {}).get("code")

    def clean(self, *answers):
        blob = json.dumps(answers) + (self.journal_file.read_text(encoding="utf-8")
                                      if self.journal_file.exists() else "")
        self.assertNotIn(FAKE_KEY, blob)
        self.assertNotIn("FAKEFAKE", blob)

    # --- the argument lists: the CLI's own verbs ----------------------------------------------------------------

    def test_every_act_builds_the_clis_own_argument_list(self):
        cases = {
            ("workspace-backup", ()): ["workspace-backup", "--pause-writers", "--json"],
            ("machine-backup", ()): ["machine-backup", "now"],
            ("backups-migrate", ()): ["backups", "migrate"],
            ("cleanup-plan", (("level", "low"),)): ["cleanup", "--level", "low"],
            ("cleanup", (("include", "manual"),)): ["cleanup", "--apply", "--include", "manual"],
            ("disk-guard-plan", (("largeFiles", True),)): ["disk-guard", "--large-files"],
            ("disk-guard", (("level", "critical"),)): ["disk-guard", "--apply", "--level", "critical"],
            ("update", (("version", "1.2.3"), ("reinstall", True))):
                ["update", "--apply", "--unattended", "--version", "1.2.3", "--reinstall"],
            ("update", ()): ["update", "--apply", "--unattended"],
            ("maintain-run", ()): ["maintain", "run"],
            ("maintain-ensure", ()): ["maintain", "ensure"],
            ("maintain-uninstall", ()): ["maintain", "uninstall"],
            ("maintain-app", ()): ["maintain", "app"],
            ("auto-update-on", ()): ["auto-update", "on"],
            ("auto-update-off", ()): ["auto-update", "off"],
        }
        for (action, params), argv in cases.items():
            self.assertEqual(self.ops.command(action, dict(params)), argv, action)
        self.assertEqual(set(self.ops.ACTIONS), {a for a, _ in cases} | {"backups-decrypt"})
        out = self.ops.command("backups-decrypt", {"file": "workspace-1.obsnap", "output": str(self.home / "restore/x")})
        self.assertEqual(out, ["backups", "decrypt", "workspace-1.obsnap", str(self.home / "restore/x")])

    def test_an_ungated_act_starts_a_job_and_journals_it(self):
        out = self.act("workspace-backup")
        self.assertEqual(out["action"], "workspace-backup")
        self.assertEqual(out["job"], {"id": "job-" + "a" * 32, "status": "working"})
        self.assertNotIn("recordedAs", out)
        capability, arguments, exclusive = self.start.calls[0]
        self.assertEqual((capability, arguments, exclusive),
                         ("ops.workspace-backup", {"action": "workspace-backup", "params": {}}, True))
        row = self.journal()[-1]
        self.assertEqual((row["action"], row["caller"], row["job"]), ("workspace-backup", "agent:mcp",
                                                                      "job-" + "a" * 32))
        self.assertNotIn("decision", row)

    # --- the gate on acts that delete or are hard to undo ------------------------------------------------------

    def test_gated_acts_without_the_operators_words_are_refused_before_any_job(self):
        for action, kw, act, target in (
                ("backups-migrate", {}, "backups.migrate", "legacy-copies"),
                ("backups-decrypt", {"file": "a.obsnap", "output": "restore/out"}, "backups.decrypt", "restore/out"),
                ("cleanup", {}, "disk.cleanup", "auto"),
                ("cleanup", {"include": "manual"}, "disk.cleanup", "manual"),
                ("disk-guard", {}, "disk.guard", "current"),
                ("disk-guard", {"level": "low"}, "disk.guard", "low"),
                ("update", {}, "engine.update", "latest"),
                ("update", {"version": "9.9.9"}, "engine.update", "9.9.9")):
            out = self.act(action, **kw)
            self.assertEqual(self.code(out), "operator-decision-required", action)
            self.assertEqual((out["error"]["action"], out["error"]["target"]), (act, target), action)
        self.assertEqual(self.start.calls, [])
        self.assertEqual(self.journal(), [])

    def test_a_gated_act_with_the_words_starts_its_job_and_journals_the_decision(self):
        out = self.act("update", version="2.0.0", decision=words("engine.update", "2.0.0"))
        self.assertEqual(out["recordedAs"], {"by": "operator", "via": "relayed-instruction", "caller": "agent:mcp"})
        self.assertEqual(self.start.calls[0][0], "ops.update")
        self.assertEqual(self.start.calls[0][1], {"action": "update", "params": {"version": "2.0.0"}})
        row = self.journal()[-1]
        self.assertEqual(row["decision"]["target"], "2.0.0")
        self.assertEqual(row["decision"]["caller"], "agent:mcp")

    def test_a_decision_for_another_act_another_target_or_an_old_one_is_refused(self):
        for bad in (words("disk.guard", "auto"), words("disk.cleanup", "manual"),
                    {**words("disk.cleanup", "auto"), "at": "2020-01-01T00:00:00Z"}, "clean it", {}):
            out = self.act("cleanup", decision=bad)
            self.assertEqual(self.code(out), "decision-invalid", bad)
        self.assertEqual(self.start.calls, [])

    def test_ungated_acts_take_no_decision_and_plans_need_none(self):
        for action in ("machine-backup", "cleanup-plan", "disk-guard-plan", "maintain-run", "maintain-ensure",
                       "maintain-uninstall", "maintain-app", "auto-update-on", "auto-update-off"):
            out = self.act(action)
            self.assertIn("job", out, (action, out))
        self.assertEqual(len(self.start.calls), 9)

    # --- the decrypted backup stays inside the workspace -------------------------------------------------------

    def test_decrypt_writes_only_a_new_path_inside_the_workspace(self):
        outside = self.base / "elsewhere"
        outside.mkdir()
        (self.home / "restore" / "taken").mkdir()
        outputs = [str(outside / "x"), "../elsewhere/x", "restore/../../elsewhere/x", "restore/taken",
                   "missing-folder/x", "", ".", str(self.home)]
        try:
            (self.home / "link").symlink_to(outside, target_is_directory=True)
            outputs.append("link/x")
        except OSError:                     # Windows without the symlink privilege: the other cases still run
            pass
        for output in outputs:
            out = self.act("backups-decrypt", file="a.obsnap", output=output,
                           decision=words("backups.decrypt", "restore/x"))
            self.assertEqual(self.code(out), "invalid-argument", output)
        self.assertEqual(self.start.calls, [])
        out = self.act("backups-decrypt", file="a.obsnap", output="restore/x",
                       decision=words("backups.decrypt", "restore/x"))
        self.assertIn("job", out, out)
        self.assertEqual(self.start.calls[0][1]["params"]["output"], "restore/x")
        self.assertEqual(self.ops.command("backups-decrypt", self.start.calls[0][1]["params"])[-1],
                         str(self.home / "restore" / "x"))

    def test_decrypt_needs_a_file(self):
        out = self.act("backups-decrypt", output="restore/x", decision=words("backups.decrypt", "restore/x"))
        self.assertEqual(self.code(out), "invalid-argument")

    # --- arguments ---------------------------------------------------------------------------------------------

    def test_an_argument_of_another_act_or_an_unknown_value_is_refused_not_dropped(self):
        for action, kw in (("workspace-backup", {"level": "low"}), ("update", {"include": "manual"}),
                           ("cleanup", {"largeFiles": True}), ("cleanup", {"level": "panic"}),
                           ("cleanup", {"include": "everything"}), ("update", {"version": "latest"}),
                           ("maintain-run", {"reinstall": True}), ("nuke", {})):
            out = self.act(action, **kw)
            self.assertEqual(self.code(out), "invalid-argument", (action, kw))
        self.assertEqual(self.start.calls, [])

    def test_a_pasted_value_is_refused_and_never_echoed(self):
        for kw in ({"file": FAKE_KEY, "output": "restore/x"}, {"file": "a.obsnap", "output": "restore/" + FAKE_KEY}):
            out = self.act("backups-decrypt", decision=words("backups.decrypt", "restore/x"), **kw)
            self.assertEqual(self.code(out), "invalid-argument")
            self.clean(out)
        out = self.act("cleanup", decision=words("disk.cleanup", "auto", instruction=f"use {FAKE_KEY}"))
        self.assertEqual(self.code(out), "decision-invalid")
        self.clean(out)
        self.assertEqual(self.start.calls, [])

    def test_system_setup_off_refuses_the_acts_that_touch_the_scheduler(self):
        os.environ["OBSERVATORY_SYSTEM_SETUP"] = "0"
        for action, kw in (("maintain-run", {}), ("maintain-ensure", {}), ("auto-update-on", {}),
                           ("update", {"decision": words("engine.update", "latest")})):
            out = self.act(action, **kw)
            self.assertEqual(self.code(out), "system-setup-off", action)
        self.assertIn("job", self.act("auto-update-off"))

    def test_a_different_running_act_is_busy_and_a_failed_start_is_a_typed_refusal(self):
        self.start = Starter(busy=True)
        self.assertEqual(self.code(self.act("workspace-backup")), "busy")
        self.start = Starter(status="failed")
        self.assertEqual(self.code(self.act("workspace-backup")), "failed")

    # --- the runner: the CLI's own function, its JSON, scrubbed ------------------------------------------------

    def job(self, action, params=None):
        return {"id": "job-" + "b" * 32, "capability": f"ops.{action}",
                "arguments": {"action": action, "params": params or {}},
                "trace": {"traceId": "1" * 32, "spanId": "2" * 16, "parentSpanId": None, "flags": "01"}}

    def test_the_runner_calls_the_cli_in_process_and_answers_its_json(self):
        seen = []

        def cli(argv):
            seen.append((argv, os.environ.get("OBSERVATORY_SYSTEM_SETUP")))
            return 0, 'note\n{\n  "status": "taken",\n  "file": "w.obsnap",\n  "token": "' + FAKE_KEY + '"\n}\n', ""
        envelope = self.ops.run(self.job("workspace-backup"), inprocess=cli)
        self.assertEqual(seen, [(["workspace-backup", "--pause-writers", "--json"], None)])
        self.assertEqual(envelope["outcome"], "succeeded")
        self.assertEqual(envelope["output"]["exit"], 0)
        self.assertEqual(envelope["output"]["result"]["status"], "taken")
        self.clean(envelope)

    def test_the_runner_sets_system_setup_for_the_scheduler_acts(self):
        seen = []

        def cli(argv):
            seen.append(os.environ.get("OBSERVATORY_SYSTEM_SETUP"))
            return 0, '{"auto_update": true}', ""
        self.ops.run(self.job("maintain-ensure"), inprocess=cli)
        self.ops.run(self.job("auto-update-off"), inprocess=cli)
        self.assertEqual(seen, ["1", None])
        self.assertIsNone(os.environ.get("OBSERVATORY_SYSTEM_SETUP"))

    def test_a_nonzero_exit_with_json_is_a_partial_result_and_without_it_a_failed_job(self):
        envelope = self.ops.run(self.job("maintain-uninstall"),
                                inprocess=lambda argv: (2, '{"error": {"code": "refused", "message": "no"}}', ""))
        self.assertEqual(envelope["outcome"], "partial")
        self.assertEqual(envelope["output"]["exit"], 2)
        with self.assertRaises(RuntimeError) as caught:
            self.ops.run(self.job("maintain-uninstall"),
                         inprocess=lambda argv: (2, "", f"Observatory: refused {FAKE_KEY}\n"))
        self.assertIn("exited 2", str(caught.exception))
        self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_cleanup_and_the_disk_guard_run_through_machine_command_with_a_json_summary(self):
        seen = []

        def machine(name, argv, *, json_summary=False):
            seen.append((name, argv, json_summary))
            print("git hygiene: 3 checkouts")
            print(json.dumps({"result": "ran", "removed": 2}))
            return 0
        import workspace
        with patch.object(workspace, "machine_command", machine):
            envelope = self.ops.run(self.job("cleanup", {"include": "manual"}))
        self.assertEqual(seen, [("cleanup", ["--apply", "--include", "manual"], True)])
        self.assertEqual(envelope["output"]["result"], {"result": "ran", "removed": 2})

    def test_the_update_runs_the_engine_cli_in_a_process_of_its_own(self):
        seen = []

        def process(argv, job):
            seen.append((argv, os.environ.get("OBSERVATORY_SYSTEM_SETUP")))
            return 0, '{"status": "updated", "to": "2.0.0"}', ""
        envelope = self.ops.run(self.job("update", {"version": "2.0.0"}), process=process,
                                inprocess=lambda argv: self.fail("an update never runs in the runner's process"))
        self.assertEqual(seen, [(["update", "--apply", "--unattended", "--version", "2.0.0"], "1")])
        self.assertEqual(envelope["output"]["result"]["status"], "updated")

    def test_the_process_runner_starts_the_engine_cli_in_a_new_group(self):
        calls = []

        class Proc:
            returncode = 0

            def wait(self):
                return 0

        def popen(argv, **kw):
            calls.append((argv, kw))
            kw["stdout"].write(b'{"status": "updated"}')
            return Proc()
        jobs_dir = self.base / "jobs"
        jobs_dir.mkdir()
        import jobs
        with patch.object(jobs, "jobs_dir", lambda: jobs_dir), \
                patch.object(self.ops.subprocess, "Popen", popen):
            code, out, err = self.ops._process(["update", "--apply"], self.job("update"))
        self.assertEqual((code, json.loads(out)), (0, {"status": "updated"}))
        argv, kw = calls[0]
        self.assertEqual(argv[-5:], ["--home", str(self.home), "full", "update", "--apply"])
        self.assertIn("-P", argv)
        self.assertTrue(kw.get("start_new_session") or kw.get("creationflags"))
        self.assertEqual(list(jobs_dir.iterdir()), [], "the output files are removed once read")

    def test_the_runner_rebuilds_its_arguments_and_refuses_a_tampered_record(self):
        with self.assertRaises(RuntimeError):
            self.ops.run(self.job("cleanup", {"level": "panic"}), inprocess=lambda argv: (0, "{}", ""))
        with self.assertRaises(RuntimeError):
            self.ops.run(self.job("nuke"), inprocess=lambda argv: (0, "{}", ""))

    # --- one implementation ------------------------------------------------------------------------------------

    def test_ops_acts_reaches_only_the_clis_own_functions(self):
        source = (ROOT / "ops_acts.py").read_text(encoding="utf-8")
        self.assertIn("workspace.main(", source)
        self.assertIn("workspace.machine_command(", source)
        for verb in ("backup_vault.", "maintenance.ensure", "maintenance.run_pass", "machine_backup.take",
                     "snapshot(", "disk_guard.", "cleanup.main", "engine_update.main", "shutil.rmtree"):
            self.assertNotIn(verb, source, verb)
        server = (ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
        body = server.split("def observatory_ops(", 1)[1].split("\n@server.tool", 1)[0]
        self.assertIn("ops_acts.act(", body)

    def test_the_job_runner_knows_every_act(self):
        source = (ROOT / "tools" / "run_job.py").read_text(encoding="utf-8")
        self.assertIn("ops_acts.CAPABILITIES", source)
        self.assertEqual(self.ops.CAPABILITIES, {f"ops.{a}" for a in self.ops.ACTIONS})

    def test_machine_command_adds_the_json_summary_only_when_asked(self):
        import workspace
        source = (ROOT / "workspace.py").read_text(encoding="utf-8")
        body = source.split("def machine_command(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("json_summary", body)
        self.assertEqual(body.count('["--json"] if json_summary else []'), 2)
        self.assertIn("json_summary", workspace.machine_command.__code__.co_varnames)


if __name__ == "__main__":
    unittest.main()
