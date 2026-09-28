"""Compatibility checks for the old CLI and isolated full-engine launcher."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from observatory import cli
from observatory import full_cli


class FullLauncherTests(unittest.TestCase):
    def test_existing_commands_still_parse(self):
        for command in ['init', 'doctor', 'demo', 'scan', 'status', 'dashboard', 'export']:
            self.assertEqual(cli.parser().parse_args([command]).cmd, command)

    def test_home_precedence_and_separate_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(full_cli.full_home(), Path.home() / '.local/share/project-observatory-full')
        with patch.dict(os.environ, {'OBSERVATORY_HOME': '/private/old', 'OBSERVATORY_FULL_HOME': '/private/new'}, clear=True):
            self.assertEqual(full_cli.full_home(), Path('/private/new'))
            self.assertEqual(full_cli.full_home('/private/explicit'), Path('/private/explicit'))

    def test_portable_state_refused_without_modification(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            (home / 'config.json').write_text('{"private":"synthetic"}')
            before = (home / 'config.json').read_bytes()
            with contextlib.redirect_stderr(io.StringIO()), patch.object(full_cli.subprocess, 'run') as run:
                self.assertEqual(full_cli.run(['init'], str(home)), 2)
                run.assert_not_called()
            self.assertEqual((home / 'config.json').read_bytes(), before)
            self.assertEqual(len(list(home.iterdir())), 1)

    def test_subprocess_preserves_arguments_and_exit(self):
        with patch.object(full_cli.subprocess, 'run') as run:
            run.return_value.returncode = 2
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(full_cli.run(['upgrade', '--apply', '--writers-stopped'], str(Path(tmp).resolve())), 2)
            argv = run.call_args.args[0]
            self.assertEqual(argv[-3:], ['upgrade', '--apply', '--writers-stopped'])
            self.assertTrue(run.call_args.kwargs['env']['OBSERVATORY_HOME'])
            self.assertNotIn('shell', run.call_args.kwargs)

    def test_full_help_and_path_are_nonmutating_routes(self):
        self.assertTrue(cli.parser().parse_args(['full', '--help']).engine_help)
        with patch('observatory.full_cli.run', return_value=0) as run:
            self.assertEqual(cli.main(['full', '--help']), 0)
            run.assert_called_once_with(['--help'], None)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['full-path']), 0)
        self.assertEqual(Path(out.getvalue().strip()), full_cli.engine_path())


if __name__ == '__main__':
    unittest.main()


class BareCommand(unittest.TestCase):
    """`observatory` / `project-observatory` with no arguments opens the dashboard."""

    def test_bare_opens_the_dashboard_of_an_initialized_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "workspace"
            home.mkdir()
            (home / "workspace.json").write_text("{}")
            with patch.dict(os.environ, {"OBSERVATORY_HOME": str(home)}), \
                    patch("observatory.full_cli.run", return_value=0) as run:
                self.assertEqual(cli.main([]), 0)
            run.assert_called_once_with(["open"], None)

    def test_home_without_a_command_opens_that_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "elsewhere"
            home.mkdir()
            (home / "workspace.json").write_text("{}")
            with patch("observatory.full_cli.run", return_value=0) as run:
                self.assertEqual(cli.main(["--home", str(home)]), 0)
            run.assert_called_once_with(["open"], str(home))

    def test_bare_without_a_workspace_says_where_to_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with patch.dict(os.environ, {"OBSERVATORY_HOME": str(Path(tmp) / "absent")}), \
                    patch("observatory.full_cli.run") as run, patch("sys.stdout", out):
                self.assertEqual(cli.main([]), 0)
            run.assert_not_called()
            self.assertIn("full init", out.getvalue())
            self.assertIn("demo", out.getvalue())

    def test_short_name_is_an_installed_entry_point(self):
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        import re
        self.assertRegex(text, r'(?m)^observatory = "observatory\.cli:main"$')
        self.assertRegex(text, r'(?m)^project-observatory = "observatory\.cli:main"$')

    def test_program_name_follows_the_command_typed(self):
        with patch("sys.argv", ["/usr/local/bin/observatory"]):
            self.assertEqual(cli.parser().prog, "observatory")
        with patch("sys.argv", ["/usr/local/bin/project-observatory"]):
            self.assertEqual(cli.parser().prog, "project-observatory")
