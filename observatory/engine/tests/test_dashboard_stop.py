#!/usr/bin/env python3
"""`full open --stop` ends the server `full open --serve` started, and only that.

`--serve` starts `tools/serverd.py --run` detached, so closing the terminal does
not stop it. These cases start a real server on a free loopback port for a
synthetic workspace and stop it through the same command a person runs.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DashboardStop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="observatory-dashboard-stop-")
        self.base = Path(self.tmp.name).resolve()
        (self.base / "user").mkdir()
        self.servers: list[subprocess.Popen] = []

    def tearDown(self):
        for proc in self.servers:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        self.tmp.cleanup()

    @staticmethod
    def fresh_paths():
        """`paths` resolves the workspace at import; each in-process case gets its own."""
        return patch.dict(sys.modules, {k: v for k, v in sys.modules.items() if k != "paths"}, clear=True)

    def env(self, name: str) -> dict[str, str]:
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.base / "user"),
                "LANG": "C.UTF-8", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1",
                "OBSERVATORY_HOME": str(self.base / name)}

    def workspace(self, name: str) -> dict[str, str]:
        env = self.env(name)
        p = subprocess.run([sys.executable, "observatory.py", "init"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        return env

    def serve(self, env: dict[str, str], port: int) -> subprocess.Popen:
        # The command line `full open --serve` uses: the engine's absolute path.
        proc = subprocess.Popen([sys.executable, str(ROOT / "tools/serverd.py"), "--run", "--port", str(port)],
                                cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        self.servers.append(proc)
        sys.path.insert(0, str(ROOT / "tools"))
        import dashboard_open
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if dashboard_open.healthy(port):
                return proc
            self.assertIsNone(proc.poll(), "the server exited before answering")
            time.sleep(0.2)
        self.fail("the server did not answer")

    def stop(self, env: dict[str, str], port: int) -> tuple[int, dict, str]:
        p = subprocess.run([sys.executable, "observatory.py", "open", "--stop", "--port", str(port)],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        try:
            doc = json.loads(p.stdout) if p.stdout.strip() else {}
        except ValueError:
            doc = {}
        return p.returncode, doc, p.stderr

    def test_stop_ends_this_workspaces_server_and_is_idempotent(self):
        env = self.workspace("home")
        port = free_port()
        proc = self.serve(env, port)
        code, doc, err = self.stop(env, port)
        self.assertEqual(code, 0, err)
        self.assertEqual((doc.get("stopped"), doc.get("pid"), doc.get("port")), (True, proc.pid, port))
        proc.wait(timeout=10)
        import dashboard_open
        self.assertIsNone(dashboard_open.healthy(port, timeout=1))
        code, doc, err = self.stop(env, port)
        self.assertEqual(code, 0, err)
        self.assertIs(doc.get("stopped"), False)
        self.assertIn("no Observatory server", doc.get("reason", ""))

    def test_another_workspaces_server_is_left_running(self):
        mine, theirs = self.workspace("mine"), self.workspace("theirs")
        port = free_port()
        proc = self.serve(theirs, port)
        code, _doc, err = self.stop(mine, port)
        self.assertEqual(code, 2)
        self.assertIn("another workspace", err)
        self.assertIsNone(proc.poll(), "a server serving another workspace must survive")

    def test_a_pid_that_is_not_this_server_is_never_signalled(self):
        # /health is an HTTP answer, and anything on the port can claim a pid.
        # The process must be this engine's own serverd before it is signalled.
        env = self.workspace("home")
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.servers.append(bystander)
        with patch.dict(os.environ, env, clear=True), self.fresh_paths():
            sys.path.insert(0, str(ROOT / "tools"))
            import dashboard_open
            with patch.object(dashboard_open, "healthy", return_value={"status": 302}), \
                 patch.object(dashboard_open, "health_document",
                              return_value={"pid": bystander.pid, "workspace": env["OBSERVATORY_HOME"]}):
                with self.assertRaises(dashboard_open.configuration.ConfigurationError) as ctx:
                    dashboard_open.stop_server(free_port())
        self.assertIn("not this engine's dashboard server", str(ctx.exception))
        self.assertIsNone(bystander.poll())

    def test_an_always_on_server_is_refused_with_the_command_that_stops_it(self):
        # launchd's KeepAlive would restart a signalled server; the refusal
        # names the uninstall command instead of pretending to have stopped it.
        env = self.workspace("home")
        port = free_port()
        proc = self.serve(env, port)
        with patch.dict(os.environ, env, clear=True), self.fresh_paths():
            import dashboard_open
            with patch.object(dashboard_open, "_always_on", return_value=True):
                with self.assertRaises(dashboard_open.configuration.ConfigurationError) as ctx:
                    dashboard_open.stop_server(port)
        self.assertIn("tools/serverd.py --uninstall", str(ctx.exception))
        self.assertIsNone(proc.poll(), "an always-on server is left to its uninstall command")

    def test_serve_and_stop_are_exclusive(self):
        env = self.workspace("home")
        p = subprocess.run([sys.executable, "observatory.py", "open", "--serve", "--stop"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
