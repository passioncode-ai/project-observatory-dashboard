#!/usr/bin/env python3
"""OBS-159: a running session MCP server moves itself onto the installed release.

The subprocess cases install a copy of this engine as release A in a temporary
directory, start its `mcp/server.py` over real pipes, complete the handshake, then
install release B over it file by file (`os.replace`, as pip does) and send the next
request. What must hold:

- the request that found the code replaced is answered exactly once, by release B,
  in the same process (same pid), without a second `initialize`;
- B's tool set differs, so `notifications/tools/list_changed` reaches the client;
- later requests are served by B; the swap is one line in `store/logs/mcp-reload.jsonl`;
- an update holding the workspace's lock, an unreadable installed tree, removed code
  and a failing `execv` each leave the server answering `stale-server` with the
  reason, the pipe alive, and every request answered once;
- after a transient refusal clears, the next request swaps.

Unit cases cover the state hand-over, the preflight judgement, the Windows branch,
the log summary for `full doctor` and the lifecycle scan's reading of a swapped pid.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import mcp_config_fixture as fx  # noqa: E402
import session_reload  # noqa: E402

POSIX = sys.platform != "win32"
PROBE = '''

@server.tool(name="observatory_reload_probe", title="Reload probe (test release)")
def observatory_reload_probe() -> dict[str, Any]:
    """Which release answers (a test-only tool of release B)."""
    return {"version": VERSION, "degraded": []}

'''
ANCHOR = "# memory/0.1 (PB-137 N-025)"


def _replace(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class Install:
    """A copy of this engine in `folder`, at a chosen VERSION."""

    def __init__(self, folder: Path, version: str) -> None:
        self.engine = folder / "engine"
        shutil.copytree(ROOT, self.engine, ignore=shutil.ignore_patterns(
            "tests", "__pycache__", "node_modules", "*.pyc", ".pytest_cache"))
        self.set_version(version)

    @property
    def server(self) -> Path:
        return self.engine / "mcp" / "server.py"

    def set_version(self, version: str) -> None:
        conf = self.engine / "configuration.py"
        text = conf.read_text(encoding="utf-8")
        import re
        _replace(conf, re.sub(r'^VERSION\s*=\s*"[^"]+"', f'VERSION = "{version}"', text, count=1, flags=re.M))

    def install_b(self, version: str) -> None:
        """Release B: a new version and one more tool, replaced file by file."""
        text = self.server.read_text(encoding="utf-8")
        assert ANCHOR in text
        _replace(self.server, text.replace(ANCHOR, PROBE + ANCHOR, 1))
        self.set_version(version)


class Client:
    """Raw JSON-RPC over the server's pipes: every line it writes is kept."""

    def __init__(self, server: Path, env: dict, log: Path) -> None:
        self.err = open(log, "wb")
        self.proc = subprocess.Popen([sys.executable, str(server)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.err, env=env)
        self.lines: queue.Queue = queue.Queue()
        self.seen: list[dict] = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for raw in self.proc.stdout:
            try:
                self.lines.put(json.loads(raw))
            except ValueError:
                self.lines.put({"unparsed": raw.decode("utf-8", "replace")})
        self.lines.put(None)

    def send(self, obj: dict) -> None:
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        self.proc.stdin.flush()

    def answer(self, mid, timeout: float = 120) -> dict:
        deadline = time.monotonic() + timeout
        for msg in self.seen:
            if msg.get("id") == mid and "method" not in msg:
                return msg
        while time.monotonic() < deadline:
            try:
                msg = self.lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if msg is None:
                raise AssertionError(f"the server closed its output before answering {mid}")
            self.seen.append(msg)
            if msg.get("id") == mid and "method" not in msg:
                return msg
        raise AssertionError(f"no answer to {mid} within {timeout} s")

    def handshake(self) -> dict:
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "reload-test", "version": "1"}}})
        init = self.answer(1)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return init

    def call(self, mid, name: str, arguments: dict | None = None) -> dict:
        self.send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments or {}}})
        return self.answer(mid)

    def close(self) -> list[dict]:
        """EOF, wait for exit, and every line the server wrote."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        while True:
            try:
                msg = self.lines.get(timeout=5)
            except queue.Empty:
                break
            if msg is None:
                break
            self.seen.append(msg)
        self.release()
        return self.seen

    def release(self) -> None:
        for stream in (self.proc.stdin, self.proc.stdout, self.err):
            try:
                stream.close()
            except OSError:
                pass


def body(answer: dict) -> dict:
    result = answer["result"]
    if result.get("structuredContent"):
        return result["structuredContent"]
    return json.loads(result["content"][0]["text"])


@unittest.skipUnless(POSIX, "os.execv replaces the process only on POSIX; Windows is the unit case below")
class SwapOverPipes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="obs159-")
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "workspace"
        fx.make_workspace(self.home)
        (self.base / "user").mkdir()
        self.install = Install(self.base / "install", "9.0.0")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OBSERVATORY_", "FABRIC_"))}
        env.update(HOME=str(self.base / "user"), OBSERVATORY_HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1",
                   OBSERVATORY_PRODUCT_LOG_DIR=str(self.base / "product-logs"),
                   OBSERVATORY_RELOAD_RETRY_SECONDS="0", OBSERVATORY_DB=str(self.home / "store/observatory.db"))
        self.env = env
        self.client = None

    def tearDown(self):
        if self.client is not None and self.client.proc.poll() is None:
            self.client.proc.kill()
            self.client.proc.wait()
        if self.client is not None:
            self.client.release()
        self.tmp.cleanup()

    def start(self, **extra) -> Client:
        self.client = Client(self.install.server, {**self.env, **extra}, self.base / "server.err")
        init = self.client.handshake()
        self.assertEqual(init["result"]["serverInfo"]["version"], "9.0.0")
        return self.client

    def log(self) -> list[dict]:
        return session_reload.read_log(self.home)

    def assert_once(self, seen: list[dict], *ids) -> None:
        for mid in ids:
            n = sum(1 for m in seen if m.get("id") == mid and "method" not in m)
            self.assertEqual(n, 1, f"request {mid} answered {n} times")
        self.assertFalse([m for m in seen if m.get("id") == session_reload.SYNTHETIC_ID],
                         "the replayed handshake must never be answered on the wire")
        self.assertFalse([m for m in seen if "unparsed" in m], "only JSON-RPC on the wire")

    def test_the_next_request_is_answered_once_by_the_installed_release(self):
        c = self.start()
        pid = c.proc.pid
        c.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in c.answer(2)["result"]["tools"]}
        self.assertNotIn("observatory_reload_probe", names)
        self.install.install_b("9.1.0")
        first = c.call(3, "observatory_reload_probe")
        self.assertNotIn("error", first)
        self.assertFalse(first["result"].get("isError"), first)
        self.assertEqual(body(first)["version"], "9.1.0")
        # No second initialize: a later request is served as well.
        c.send({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        self.assertIn("observatory_reload_probe", {t["name"] for t in c.answer(4)["result"]["tools"]})
        self.assertEqual(body(c.call(5, "observatory_reload_probe"))["version"], "9.1.0")
        self.assertEqual(c.proc.pid, pid)
        seen = c.close()
        self.assert_once(seen, 1, 2, 3, 4, 5)
        self.assertTrue([m for m in seen if m.get("method") == "notifications/tools/list_changed"],
                        "the tool set changed, so the client is told")
        swaps = [r for r in self.log() if r.get("event") == "swapped"]
        self.assertEqual([(r["pid"], r["from"], r["to"]) for r in swaps], [(pid, "9.0.0", "9.1.0")])

    def test_an_update_in_progress_refuses_until_it_ends(self):
        import oslocks
        c = self.start()
        lock = self.home.parent / f".{self.home.name}.observatory-update.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
        try:
            self.install.install_b("9.1.0")
            held = c.call(3, "observatory_reload_probe")
            self.assertTrue(held["result"]["isError"])
            refusal = body(held)
            self.assertEqual(refusal["error"], "stale-server")
            self.assertIn("an update of this workspace is in progress", refusal["detail"])
        finally:
            os.close(fd)
        self.assertEqual(body(c.call(4, "observatory_reload_probe"))["version"], "9.1.0")
        self.assert_once(c.close(), 3, 4)

    def test_an_unreadable_installed_tree_keeps_the_session_on_stale_server(self):
        c = self.start()
        self.install.install_b("9.1.0")
        self.install.server.chmod(0)
        try:
            answer = c.call(3, "observatory_reload_probe")
            refusal = body(answer)
            self.assertEqual(refusal["error"], "stale-server")
            self.assertIn("the installed release did not start", refusal["detail"])
            c.send({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
            self.assertIn("tools", c.answer(4)["result"], "the pipe stays alive")
        finally:
            self.install.server.chmod(0o644)
        self.assert_once(c.close(), 3, 4)
        self.assertTrue([r for r in self.log() if r.get("event") == "refused"])

    def test_removed_code_is_refused_with_its_reason(self):
        c = self.start()
        (self.install.engine / "configuration.py").unlink()
        refusal = body(c.call(3, "observatory_overview"))
        self.assertEqual(refusal["error"], "stale-server")
        self.assertIn("no longer installed", refusal["detail"])
        self.assert_once(c.close(), 3)

    def test_a_failed_exec_puts_the_wire_back_and_answers_once(self):
        c = self.start(OBSERVATORY_TEST_RELOAD_EXEC="fail")
        self.install.install_b("9.1.0")
        refusal = body(c.call(3, "observatory_reload_probe"))
        self.assertEqual(refusal["error"], "stale-server")
        self.assertIn("could not replace this process", refusal["detail"])
        again = body(c.call(4, "observatory_reload_probe"))
        self.assertEqual(again["error"], "stale-server", "a permanent refusal is not retried for the same tree")
        c.send({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
        self.assertIn("tools", c.answer(5)["result"])
        self.assert_once(c.close(), 3, 4, 5)


class StateAndJudgement(unittest.TestCase):
    def test_the_state_round_trips_through_an_inherited_descriptor(self):
        fd = session_reload.write_state({"reload": session_reload.RELOAD_PROTOCOL, "pending": "eA=="})
        self.assertTrue(os.get_inheritable(fd))
        env = {session_reload.STATE_ENV: str(fd)}
        state, why = session_reload.take_state(env)
        self.assertIsNone(why)
        self.assertEqual(session_reload.decode(state["pending"]), b"x")
        self.assertNotIn(session_reload.STATE_ENV, env)
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_no_state_and_an_unreadable_state(self):
        self.assertEqual(session_reload.take_state({}), (None, None))
        state, why = session_reload.take_state({session_reload.STATE_ENV: "not-a-number"})
        self.assertIsNone(state)
        self.assertIn("no descriptor", why)

    def test_judge_refuses_another_reload_protocol_or_mcp_revision(self):
        ok = {"reload": session_reload.RELOAD_PROTOCOL, "protocol": "2026-07-28", "version": "1", "tools": []}
        self.assertIsNone(session_reload.judge(ok, protocol="2026-07-28"))
        self.assertIn("reload protocol", session_reload.judge({**ok, "reload": 2}, protocol="2026-07-28"))
        self.assertIn("MCP revision", session_reload.judge(ok, protocol="2025-01-01"))

    def test_windows_answers_stale_server_with_the_reason(self):
        self.assertIsNone(session_reload.platform_can_swap("darwin"))
        reason = session_reload.platform_can_swap("win32")
        self.assertIn("cannot replace a running process", reason)
        sys.path.insert(0, str(ROOT / "mcp"))
        import code_freshness
        import capability_tools as ct
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "configuration.py"
            marker.write_text('VERSION = "1.0.0"\n')
            with patch.object(ct, "FRESHNESS", code_freshness.Freshness(marker, "1.0.0")), \
                    patch.object(session_reload, "REFUSAL", reason):
                _replace(marker, 'VERSION = "1.1.0"\n')
                detail = json.loads(ct.stale_answer().content[0].text)["detail"]
        self.assertIn("cannot replace a running process", detail)
        self.assertIn("Restart the session", detail)

    def test_server_main_takes_the_sdk_transport_where_execv_cannot_replace(self):
        source = (ROOT / "mcp/server.py").read_text(encoding="utf-8")
        main = source[source.index("def main()"):]
        self.assertLess(main.index("platform_can_swap()"), main.index("from stdio_relay import Relay"))
        self.assertLess(main.index("run_stdio_async()"), main.index("from stdio_relay import Relay"))

    def test_preflight_of_a_release_without_the_check_cannot_take_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "server.py"
            old.write_text("import sys\nsys.stdin.read()\n")
            answer, why = session_reload.preflight(sys.executable, old, [])
        self.assertIsNone(answer)
        self.assertIn("cannot take over", why)


class LogAndLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_doctor_summary_names_swaps_and_refusals_without_paths(self):
        with patch("sys.stderr"):
            session_reload.record(self.home, "swapped", **{"from": "1.0.0", "to": "1.1.0"})
            session_reload.record(self.home, "refused", **{"from": "1.0.0", "to": "1.1.0",
                                  "reason": session_reload.clean_reason("exit 2 at /secret/path?token=x")})
        out = session_reload.summary(self.home)
        self.assertEqual(out["swapped"][0]["pid"], os.getpid())
        self.assertTrue(out["swapped"][0]["running"])
        self.assertNotIn("/", out["refused"][0]["reason"])
        self.assertTrue(os.stat(session_reload.log_path(self.home)).st_mode & 0o077 == 0 or not POSIX)

    def test_the_lifecycle_scan_ages_a_swapped_server_from_its_swap(self):
        import collectors.scan_lifecycle as sl
        code = self.home / "server.py"
        code.write_text("")
        changed = os.stat(code).st_mtime
        product = {"product": "engine", "slug": "engine", "labels": [], "logs": [], "children": [],
                   "log_cap_bytes": 1, "session_servers": [{"match": "server.py", "code": str(code)}]}
        proc = {"pid": 4242, "ppid": 2, "elapsed_s": 3600, "exe": "python3", "script": "server.py"}
        snap = {"now": changed + 10, "processes": [proc], "launchd": {}}
        stale = [o for o in sl.evaluate(snap, [product])["observations"] if o["kind"] == "stale-code"]
        self.assertEqual(len(stale), 1)
        snap["reloads"] = {"4242": changed + 5}
        fresh = [o for o in sl.evaluate(snap, [product])["observations"] if o["kind"] == "stale-code"]
        self.assertEqual(fresh, [], "a server that swapped after the code changed runs that code")


if __name__ == "__main__":
    unittest.main()
