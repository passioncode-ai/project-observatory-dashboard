"""`python -P -m observatory.job NAME ARGS`: what a scheduled job's definition names instead of a
path inside `<venv>/lib/python3.X/site-packages` (OBS-393).

A venv rebuilt on another Python release (3.14 to 3.15) left every launchd job naming files that
no longer existed, and launchd failed each run with exit 78. The launcher finds the engine this
interpreter has installed now and runs the program as its file would run: same arguments, and on
POSIX the same process (exec), so the scheduler's signals and process listings see the script.
"""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from observatory import job
from observatory import full_cli

REPO = Path(__file__).resolve().parents[1]
#: `-P` keeps the working folder off sys.path, so the package is found by this path, as an install finds it.
ENV = {**os.environ, "PYTHONPATH": str(REPO)}


class LauncherTests(unittest.TestCase):
    def test_the_table_is_the_installers(self):
        # tools/install_launchd.py names the same programs in the definitions it writes (JOB_TOOLS).
        source = (job.engine_root() / "tools" / "install_launchd.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        table = next(ast.literal_eval(node.value) for node in ast.walk(tree)
                     if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "JOB_TOOLS" for t in node.targets))
        self.assertEqual({name: entry[:2] for name, entry in job.TOOLS.items()}, table)

    def test_every_name_runs_a_file_of_this_engine(self):
        self.assertEqual(job.engine_root(), full_cli.engine_path())
        for name, (relative, kind, _chdir) in job.TOOLS.items():
            self.assertTrue((job.engine_root() / relative).is_file(), name)
            argv, start = job.command(name, ["--x"])
            self.assertEqual(argv[-1], "--x")
            if kind == "python" or os.name == "nt":
                self.assertEqual(argv[0], sys.executable, name)
        self.assertEqual(job.command("maintain", ["run"])[1], job.engine_root(), "it starts in the engine, as before")
        self.assertIsNone(job.command("mcp", [])[1], "the MCP server keeps its host's folder")
        with self.assertRaises(ValueError):
            job.command("../../bin/sh", [])

    def test_an_unknown_name_is_refused_before_anything_runs(self):
        p = subprocess.run([sys.executable, "-P", "-m", "observatory.job", "nothing-like-this"],
                           cwd=REPO, env=ENV, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 2)
        self.assertIn("unknown program", p.stderr)
        p = subprocess.run([sys.executable, "-P", "-m", "observatory.job"], cwd=REPO, env=ENV,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 2)

    def test_the_program_gets_its_arguments_and_its_exit_code_comes_back(self):
        # A fake engine: `maintain` is a script that records its argv and folder, then exits 7.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "tools").mkdir()
            marker = root / "seen"
            (root / "tools" / "maintain.py").write_text(
                "import json, os, sys, pathlib\n"
                f"pathlib.Path({str(marker)!r}).write_text(json.dumps([sys.argv, os.getcwd()]))\n"
                "raise SystemExit(7)\n")
            code = (f"import observatory.job as j; from pathlib import Path; "
                    f"j.engine_root = lambda: Path({str(root)!r}); raise SystemExit(j.main(['maintain', 'run', '--x']))")
            p = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=ENV, capture_output=True, text=True,
                               timeout=60)
            self.assertEqual(p.returncode, 7, p.stderr)
            argv, cwd = json.loads(marker.read_text())
            self.assertEqual(argv, [str(root / "tools" / "maintain.py"), "run", "--x"])
            self.assertEqual(Path(cwd).resolve(), root)


    @unittest.skipIf(os.name == "nt", "exec and signal dispositions are POSIX's")
    def test_the_program_starts_with_default_signals(self):
        # Python ignores SIGPIPE, and an ignored signal survives exec: bash's tick would have
        # inherited it (review of OBS-393, finding 11).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "tools").mkdir()
            # A writer into a closed pipe dies of SIGPIPE (141) with the default, and fails (1) when ignored.
            (root / "tools" / "tick.sh").write_text('yes | head -n 1 >/dev/null; echo "${PIPESTATUS[0]}"\n')
            code = (f"import observatory.job as j; from pathlib import Path; "
                    f"j.engine_root = lambda: Path({str(root)!r}); raise SystemExit(j.main(['tick']))")
            p = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=ENV, capture_output=True, text=True,
                               timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.strip(), "141", "SIGPIPE arrives with its default, not ignored")


if __name__ == "__main__":
    unittest.main()
