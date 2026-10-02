#!/usr/bin/env python3
"""Public/full CLI routing contracts; synthetic roots and mocked execution only."""
from __future__ import annotations
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import observatory as cli


class CLICompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="observatory-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = patch.object(cli, "ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        (self.root / "tests").mkdir()
        (self.root / "tests/run_portable.py").write_text("# synthetic runner\n")
        (self.root / "public-profile.json").write_text(json.dumps({"schema_version": 1, "profile": "public"}))

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cli.main(["observatory.py", *args])
        return code, output.getvalue()

    def test_public_check_forwards_options_exit_and_never_opens_workspace(self):
        with patch.object(cli.subprocess, "call", return_value=7) as run, \
             patch.object(cli.configuration, "validate_workspace", side_effect=AssertionError("live workspace")), \
             patch.object(cli.paths, "tighten", side_effect=AssertionError("workspace write")):
            self.assertEqual(self.invoke("check", "--suite", "ledger")[0], 7)
        run.assert_called_once_with([*cli.STEPS["check-portable"], "--suite", "ledger"], cwd=self.root)

    def test_missing_runner_is_an_error_not_a_skip(self):
        (self.root / "tests/run_portable.py").unlink()
        with patch.object(cli.subprocess, "call") as run:
            code, output = self.invoke("check")
        self.assertEqual(code, 2)
        self.assertIn("unavailable", output)
        run.assert_not_called()

    def test_unknown_future_profile_fails_closed(self):
        (self.root / "public-profile.json").write_text('{"schema_version":999,"profile":"public"}')
        self.assertEqual(self.invoke("--help")[0], 2)

    def test_linked_profile_fails_closed(self):
        marker = self.root / "public-profile.json"
        original = marker.read_bytes()
        marker.unlink()
        (self.root / "target.json").write_bytes(original)
        marker.symlink_to(self.root / "target.json")
        self.assertEqual(self.invoke("--help")[0], 2)

    def test_historical_gate_retained_in_unmarked_source(self):
        (self.root / "public-profile.json").unlink()
        with patch.object(cli, "unavailable_step", return_value=""), \
             patch.object(cli.configuration, "validate_workspace"), \
             patch.object(cli.configuration, "load"), patch.object(cli.paths, "tighten"), \
             patch.dict(sys.modules, {"tick_lease": None}), \
             patch.object(cli, "_run_group", return_value=13) as group:
            self.assertEqual(self.invoke("check")[0], 13)
        self.assertEqual(group.call_args.args[:2], ("check", cli.GROUPS["check"]))
        self.assertIn("test-trap-map", group.call_args.args[1])

    def test_missing_historical_test_fails_before_workspace_access(self):
        with patch.object(cli.configuration, "validate_workspace", side_effect=AssertionError("live access")):
            code, output = self.invoke("test-docs")
        self.assertEqual(code, 2)
        self.assertIn("source payload", output)

    def test_source_only_composite_is_explicitly_unsupported(self):
        with patch.object(cli.configuration, "validate_workspace", side_effect=AssertionError("live access")):
            code, output = self.invoke("all")
        self.assertEqual(code, 2)
        self.assertIn("historical composite", output)

    def test_help_only_advertises_present_payloads(self):
        code, output = self.invoke("--help")
        self.assertEqual(code, 0)
        steps = next(line for line in output.splitlines() if line.startswith("steps:"))
        self.assertIn("check-portable", steps)
        self.assertNotIn("test-docs", steps)
        self.assertNotIn("docs-current", steps)
        self.assertNotIn("setup", steps)
        groups = next(line for line in output.splitlines() if line.startswith("groups:"))
        self.assertIn("local", groups)
        self.assertNotIn("all", groups)

    def test_backup_names_are_distinct(self):
        self.assertNotIn("backup", cli.WORKSPACE_COMMANDS)
        self.assertIn("workspace-backup", cli.WORKSPACE_COMMANDS)
        self.assertEqual(cli.STEPS["backup"][1], "tools/backup_store.py")
        import workspace
        with patch.object(workspace, "main", return_value=9) as command:
            self.assertEqual(self.invoke("workspace-backup", "--writers-stopped")[0], 9)
        command.assert_called_once_with(["workspace-backup", "--writers-stopped"])

    def test_local_cycle_has_no_plugin_provider_or_paid_step(self):
        self.assertEqual(cli.GROUPS["local"], ["scan-fs", "env", "merge", "emit", "validate", "scan-events", "findings", "dashboard", "smoke", "smoke-pages"])
        self.assertTrue(all(not name.startswith("test") for name in cli.GROUPS["local"]))

    def test_local_step_passes_offline_flag_without_changing_parent_environment(self):
        import os
        process = MagicMock()
        process.stdout = io.StringIO("")
        process.wait.return_value = 0
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(cli.subprocess, "Popen", return_value=process) as spawn, \
             contextlib.redirect_stdout(io.StringIO()):
            cli.run("merge", offline=True)
            self.assertNotIn("OBSERVATORY_OFFLINE", os.environ)
        self.assertEqual(spawn.call_args.kwargs["env"]["OBSERVATORY_OFFLINE"], "1")

    def test_offline_disables_integrations_even_when_configured(self):
        import os
        with patch.dict(os.environ, {"OBSERVATORY_OFFLINE": "1"}), \
             patch.object(cli.configuration, "load", return_value={"integrations": {"github": True}, "features": {"scheduler": True}}):
            self.assertFalse(cli.configuration.enabled("github"))
            self.assertTrue(cli.configuration.enabled("scheduler", "features"))

    def test_direct_remote_collector_does_not_probe_when_disabled(self):
        from collectors import scan_remotes
        with patch.object(cli.configuration, "enabled", return_value=False), \
             patch.object(scan_remotes, "probe", side_effect=AssertionError("network")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scan_remotes.main(["scan_remotes.py", str(self.root / "remote.json")]), 0)
        self.assertFalse((self.root / "remote.json").exists())

    def test_unrecognized_step_arguments_are_not_silently_ignored(self):
        with patch.object(cli, "unavailable_step", return_value=""), \
             patch.object(cli.configuration, "validate_workspace", side_effect=AssertionError("live access")):
            code, output = self.invoke("backup", "--unexpected")
        self.assertEqual(code, 2)
        self.assertIn("arguments are not accepted", output)



class PortableRunnerTests(unittest.TestCase):
    def test_source_reader_excludes_prose_on_supported_python_versions(self):
        from source_reader import code_only
        source = 'def operation():\n    # hidden_comment\n    return f"hidden_literal {value}"\n'
        result = code_only(source)
        self.assertIn('def operation():', result)
        self.assertIn('return', result)
        self.assertNotIn('hidden_comment', result)
        self.assertNotIn('hidden_literal', result)
        self.assertEqual(len(result.splitlines()), len(source.splitlines()))

    def test_clean_environment_preserves_loader_but_excludes_credentials(self):
        import os
        import run_portable as runner
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'LD_LIBRARY_PATH': '/synthetic/runtime/lib', 'PATH': '/synthetic/bin',
                'OPENAI_API_KEY': 'synthetic-private', 'OBSERVATORY_HOME': '/unwanted'}, clear=True):
            env = runner.clean_env(Path(directory))
            self.assertEqual(env['LD_LIBRARY_PATH'], '/synthetic/runtime/lib')
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertNotEqual(env['OBSERVATORY_HOME'], '/unwanted')

    def test_failure_diagnostic_keeps_actual_exit_and_bounded_tail(self):
        import run_portable as runner
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            template = base / 'template'; (template / 'tests').mkdir(parents=True)
            (template / 'tests/test_fixture.py').write_text(
                'print("x" * 7000)\nraise RuntimeError("synthetic child refusal")\n')
            with contextlib.redirect_stderr(io.StringIO()):
                result = runner.run_suite('fixture', base, template, 10)
            self.assertEqual(result['status'], 'FAIL')
            self.assertEqual(result['exit_code'], 1)
            self.assertIn('synthetic child refusal', result['failure_tail'])
            self.assertLessEqual(len(result['failure_tail']), 6000)
            self.assertEqual(result['log'], 'fixture/run.log')
            self.assertGreater((base / result['log']).stat().st_size, 7000)

if __name__ == "__main__":
    unittest.main()
