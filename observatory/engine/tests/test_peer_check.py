#!/usr/bin/env python3
"""The servers' peer check (OBS-355 part 1, docs/design/ACCESS.md rule 10): on a host with several
OS accounts the local server, memory over HTTP and the keyserver refuse another account's
connection before reading it; on a host with one they serve exactly as before.

`peer_owner.peer` is faked (another account cannot be conjured in a test); everything else is
real: the servers on ephemeral loopback ports, a client that sends a whole request with a bearer,
and the handler that would read it patched to fail the test if it is ever reached.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
for sub in ("", "tools", "mcp", "tests"):
    sys.path.insert(0, str(ROOT / sub) if sub else str(ROOT))
import tmp as tmpdir  # noqa: E402  a sandboxed home for a suite run by hand, and directories that go away
_IMPORT_HOME = tempfile.TemporaryDirectory(prefix="observatory-peer-check-")
with mock.patch.dict(os.environ, {"OBSERVATORY_HOME": _IMPORT_HOME.name}):
    import peer_check  # noqa: E402
    import peer_owner as P  # noqa: E402
    import serverd  # noqa: E402
    import keyserver  # noqa: E402
    import http_service  # noqa: E402

FOREIGN = P.Owner(P.ANOTHER_ACCOUNT, 502, 5200, "test")
ROOTS = P.Owner(P.SYSTEM, 0, 1, "test")
UNKNOWN = P.Owner(P.UNKNOWN, None, None, "test", "no-row")
OWN = P.Owner(P.THIS_ACCOUNT, 501, 4300, "test")


def tearDownModule():
    _IMPORT_HOME.cleanup()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def gate(accounts=2, owner=FOREIGN, setting=None, clock=None):
    cfg = pathlib.Path(tmpdir.mkdtemp(prefix="peer-check-config-"))
    if setting is not None:
        (cfg / "local.json").write_text(json.dumps({"loopback": {"peerCheck": setting}}))
    lines: list[str] = []
    calls: list = []

    def peer(sock):
        calls.append(sock)
        return owner

    g = peer_check.Gate(accounts=lambda: accounts, config_dir=lambda: cfg, peer=peer,
                        peer_at=lambda s, c: (calls.append((s, c)), owner)[1], method=lambda: "test",
                        log=lines.append, clock=clock or Clock())
    g.lines, g.calls = lines, calls
    return g


def raw_request(port: int, path: str = "/") -> bytes:
    """A whole request with a bearer, then everything the server answers until it closes."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                  "Authorization: Bearer synthetic-bearer\r\nX-Observatory-Token: synthetic\r\n\r\n".encode())
        out = b""
        while chunk := s.recv(65536):
            out += chunk
    return out


class Decision(unittest.TestCase):
    def test_one_account_and_auto_never_reads_the_table(self):
        g = gate(accounts=1)
        self.assertIsNone(g.check_socket(object()))
        self.assertIsNone(g.check_addresses(("127.0.0.1", 1), ("127.0.0.1", 2)))
        self.assertEqual(g.calls, [])
        self.assertEqual(g.snapshot()["active"], False)
        self.assertIsNone(g.snapshot()["method"])

    def test_several_or_unknown_accounts_turn_it_on(self):
        for accounts in (2, None):
            with self.subTest(accounts=accounts):
                self.assertTrue(gate(accounts=accounts).active())

    def test_always_turns_it_on_with_one_account(self):
        g = gate(accounts=1, setting="always")
        self.assertEqual(g.check_socket(object()), peer_check.FOREIGN)

    def test_no_off_a_bad_value_reads_as_always(self):
        g = gate(accounts=1, setting="off")
        snap = g.snapshot()
        self.assertEqual((snap["setting"], snap["active"]), ("always", True))
        self.assertIn("read as always", snap["problem"])

    def test_codes(self):
        self.assertEqual(peer_check.code_for(FOREIGN), peer_check.FOREIGN)
        self.assertEqual(peer_check.code_for(ROOTS), peer_check.FOREIGN)
        self.assertEqual(peer_check.code_for(UNKNOWN), peer_check.UNVERIFIED)
        self.assertIsNone(peer_check.code_for(OWN))

    def test_decision_is_reread_after_its_ttl(self):
        clock = Clock()
        count = [1]
        g = peer_check.Gate(accounts=lambda: count[0], config_dir=lambda: pathlib.Path(tmpdir.mkdtemp()),
                            peer=lambda s: FOREIGN, method=lambda: "t", log=lambda line: None, clock=clock)
        self.assertFalse(g.active())
        count[0] = 2
        clock.now += peer_check.ACTIVE_TTL - 1
        self.assertFalse(g.active())
        clock.now += 2
        self.assertTrue(g.active())

    def test_one_log_line_per_uid_and_code_per_minute_and_every_refusal_counted(self):
        clock = Clock()
        g = gate(clock=clock)
        g.check_socket(object())
        g.check_socket(object())
        clock.now += peer_check.LOG_EVERY
        g.check_socket(object())
        self.assertEqual(len(g.lines), 2)
        self.assertEqual(json.loads(g.lines[0]), {"event": "peer.refused", "code": "peer-foreign-account",
                                                  "peerUid": 502, "method": "test"})
        self.assertEqual(g.counters()["peer-foreign-account"], 3)
        self.assertEqual(g.counters()["peer-unverified"], 0)

    def test_lingering_close_delivers_the_403(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        out: list[bytes] = []
        t = threading.Thread(target=lambda: out.append(raw_request(port)))
        t.start()
        conn, _ = listener.accept()
        peer_check.refuse(conn, peer_check.FOREIGN)
        conn.close()
        listener.close()
        t.join(5)
        self.assertTrue(out[0].startswith(b"HTTP/1.1 403 Forbidden\r\n"))
        self.assertIn(b"Connection: close\r\n", out[0])
        self.assertTrue(out[0].endswith(b'{"error":"peer-foreign-account"}'))


class LocalServer(unittest.TestCase):
    def serve(self, g) -> int:
        patch = mock.patch.object(serverd.peer_check, "GATE", g)
        patch.start()
        self.addCleanup(patch.stop)
        server = serverd.LoopbackServer(("127.0.0.1", 0), serverd.Handler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_foreign_and_unverified_are_refused_before_the_request_line(self):
        for owner, code in ((FOREIGN, "peer-foreign-account"), (ROOTS, "peer-foreign-account"),
                            (UNKNOWN, "peer-unverified")):
            with self.subTest(code=code, kind=owner.kind):
                port = self.serve(gate(owner=owner))
                with mock.patch.object(serverd.Handler, "handle_one_request",
                                       side_effect=AssertionError("the request was read")) as read:
                    for path in ("/", "/.well-known/fabric-service", "/dashboard/index.html"):
                        out = raw_request(port, path)
                        head, _, body = out.partition(b"\r\n\r\n")
                        self.assertTrue(head.startswith(b"HTTP/1.1 403"), out[:80])
                        self.assertIn(b"Connection: close", head)
                        self.assertEqual(json.loads(body), {"error": code})
                read.assert_not_called()

    def test_one_account_serves_as_before(self):
        g = gate(accounts=1)
        port = self.serve(g)

        def answer(handler):
            handler.raw_requestline = handler.rfile.readline()
            handler.parse_request()
            handler.send_response(204)
            handler.end_headers()
            handler.close_connection = True

        with mock.patch.object(serverd.Handler, "handle_one_request", autospec=True, side_effect=answer):
            out = raw_request(port)
        self.assertTrue(out.startswith(b"HTTP/1.0 204") or out.startswith(b"HTTP/1.1 204"), out[:40])
        self.assertEqual(g.calls, [])

    def test_counters_ride_the_receipt_but_never_cause_a_write(self):
        g = gate()
        writes = []
        tmp = pathlib.Path(tmpdir.mkdtemp())
        with mock.patch.object(serverd.peer_check, "GATE", g), \
                mock.patch.object(serverd, "RECEIPT", tmp / "serverd.json"), \
                mock.patch.object(serverd.atomic, "write_json", side_effect=lambda p, d: writes.append(d)), \
                mock.patch.object(serverd, "refresh_remote", return_value={}), \
                mock.patch.object(serverd, "refresh_leaks", return_value={}), \
                mock.patch.object(serverd, "refresh_skills", return_value={}), \
                mock.patch.object(serverd, "_tick_health", return_value={}), \
                mock.patch.object(serverd, "_LAST_RECEIPT", [None, 0.0]):
            serverd.heartbeat()
            for _ in range(5):
                g.check_socket(object())
            doc = serverd.heartbeat()
        self.assertEqual(len(writes), 1)
        self.assertEqual(doc["peer"]["refused"]["peer-foreign-account"], 5)
        self.assertEqual(doc["peer"]["active"], True)


class Keyserver(unittest.TestCase):
    def test_foreign_is_refused_before_the_handler_exists(self):
        g = gate()
        with mock.patch.object(keyserver.peer_check, "GATE", g):
            srv = keyserver.Server(("127.0.0.1", 0), keyserver.Handler, "synthetic-token")
            srv.RequestHandlerClass = mock.Mock(side_effect=AssertionError("the handler ran"))
            threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            try:
                out = raw_request(srv.server_address[1], "/api/state")
            finally:
                srv.shutdown()
                srv.server_close()
        self.assertEqual(json.loads(out.partition(b"\r\n\r\n")[2]), {"error": "peer-foreign-account"})
        srv.RequestHandlerClass.assert_not_called()

    def test_one_account_reaches_the_handler(self):
        g = gate(accounts=1)
        with mock.patch.object(keyserver.peer_check, "GATE", g):
            srv = keyserver.Server(("127.0.0.1", 0), keyserver.Handler, "synthetic-token")
            reached = threading.Event()
            srv.RequestHandlerClass = lambda request, address, server: reached.set()
            threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            try:
                with socket.create_connection(srv.server_address, timeout=5):
                    self.assertTrue(reached.wait(5))
            finally:
                srv.shutdown()
                srv.server_close()


class MemoryHttp(unittest.TestCase):
    def call(self, g, headers=()):
        inner_calls = []

        async def inner(scope, receive, send):
            inner_calls.append(scope)

        door = http_service.BindingGate(inner, port=47313, rate=10, peers=g)
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            raise AssertionError("the body was read")

        scope = {"type": "http", "server": ("127.0.0.1", 47313), "client": ("127.0.0.1", 50002),
                 "headers": list(headers)}
        with mock.patch.object(door.MA, "http_channel", side_effect=AssertionError("a bearer was resolved")):
            asyncio.run(door(scope, receive, send))
        return sent, inner_calls

    def test_foreign_refused_before_host_origin_and_bearer(self):
        # No Host header at all: were Host checked first this would be a 421.
        for owner, code in ((FOREIGN, "peer-foreign-account"), (UNKNOWN, "peer-unverified")):
            with self.subTest(code=code):
                g = gate(owner=owner)
                sent, inner = self.call(g, [(b"authorization", b"Bearer synthetic-bearer"),
                                            (b"origin", b"http://evil.example")])
                start, body = sent
                self.assertEqual(start["status"], 403)
                self.assertIn((b"connection", b"close"), start["headers"])
                self.assertEqual(json.loads(body["body"]), {"error": code})
                self.assertEqual(inner, [])
                self.assertEqual(g.calls, [(("127.0.0.1", 47313), ("127.0.0.1", 50002))])

    def test_one_account_goes_on_to_the_host_check(self):
        g = gate(accounts=1)
        sent, _ = self.call(g)
        self.assertEqual(sent[0]["status"], 421)
        self.assertEqual(g.calls, [])


class Relay(unittest.TestCase):
    def test_the_relay_is_exempt(self):
        # Its admission is the estate record's signatures; behind a TLS terminator every peer is the
        # terminator's process, which may run as another account (docs/design/ACCESS.md, rule 10).
        for name in ("node/relay_server.py", "tools/relay_cli.py"):
            self.assertNotIn("peer_check", (ROOT / name).read_text(encoding="utf-8"), name)


class Doctor(unittest.TestCase):
    def test_status_names_the_setting_count_and_the_servers_counters(self):
        base = pathlib.Path(tmpdir.mkdtemp())
        (base / "config").mkdir()
        (base / "config" / "local.json").write_text(json.dumps({"loopback": {"peerCheck": "always"}}))
        scratch = pathlib.Path(tmpdir.mkdtemp())
        (scratch / "serverd.json").write_text(json.dumps({"peer": {"refused": {"peer-foreign-account": 3,
                                                                               "peer-unverified": 0}}}))
        import paths
        with mock.patch.object(paths, "SCRATCH", scratch), \
                mock.patch.object(peer_check.host_accounts, "count", return_value=1), \
                mock.patch.object(peer_check.peer_owner, "method", return_value="sysctl"):
            doc = peer_check.status(base)
        self.assertEqual((doc["setting"], doc["active"], doc["accountsOnHost"], doc["method"]),
                         ("always", True, 1, "sysctl"))
        self.assertEqual(doc["serverRefused"]["peer-foreign-account"], 3)


if __name__ == "__main__":
    unittest.main()
