#!/usr/bin/env python3
"""The Prowl key door (`tools/prowl.py`) against a fake Prowl admin console on loopback.

Nothing here reaches the real service. A local HTTP server plays `/admin/mcp` (streamable HTTP:
initialize with a session id, then `tools/call`, answering as an event stream like the real
server), with the tools' answers shaped as `mcp_server/admin.py` returns them. The vault is a
temporary one, written and read through the vault's own code.

What is asserted is the door's promise:
- the minted value reaches the slot and never the output, argv or the slot's metadata;
- the slot is checked before anything is minted (a held slot without --rotate mints nothing);
- a delivery the vault refuses revokes the just-minted key again;
- the admin key travels only in the Authorization header; a refused admin key is reported by
  status, with no body echoed; no admin slot configured is refused before any call;
- revoke and list answer without a value; plain http off loopback is refused.
"""
from __future__ import annotations
import contextlib
import http.server
import io
import json
import os
import pathlib
import sys
import threading
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402  sandboxes HOME first

os.environ["OBSERVATORY_VAULT_DIR"] = str(pathlib.Path(tmpdir.mkdtemp("prowl-vault-")).resolve())
sys.path.insert(0, str(ROOT / "tools"))
import vault  # noqa: E402
import use_secret  # noqa: E402
import prowl  # noqa: E402

ADMIN_SLOT = ("example-admin", "prod", "PROWL_ADMIN_KEY")
DEST_SLOT = ("example-agent", "prod", "PROWL_API_KEY")
ADMIN_VALUE = "prowl_test-only-admin-not-a-credential"


class FakeProwl:
    """State of the fake console: the keys it minted and every request it saw."""

    def __init__(self):
        self.minted: dict[str, dict] = {}
        self.revoked: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        self.auth_seen: list[str] = []
        self.urls: list[str] = []
        self.refuse_auth = False
        self.n = 0

    def tool(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        if name == "admin_api_key_issue":
            self.n += 1
            kid = f"key{self.n:04d}"
            raw = f"prowl_test-only-minted-{self.n}-{uuid.uuid4().hex[:6]}"
            self.minted[kid] = {"raw": raw, **args}
            return json.dumps({"id": kid, "key": raw, "prefix": raw[:12] + "...", "user_id": "u1",
                               "name": args["name"], "billing_mode": args.get("billing_mode", "standard"),
                               "daily_spend_limit_usd": args.get("daily_spend_limit_usd"),
                               "total_spend_limit_usd": args.get("total_spend_limit_usd"),
                               "note": "store this key now; it is not shown again"})
        if name == "admin_api_key_revoke":
            if args["key_id"] not in self.minted:
                return f"Error: no key {args['key_id']!r}"
            self.revoked.append(args["key_id"])
            return json.dumps({"id": args["key_id"], "user_id": "u1", "revoked": True})
        if name == "admin_api_keys_list":
            return json.dumps({"user_id": "u1", "email": args["user"], "keys": [
                {"id": k, "name": v["name"], "prefix": v["raw"][:12] + "...", "is_active": k not in self.revoked}
                for k, v in self.minted.items()]})
        return f"Error: unknown tool {name}"


def make_handler(state: FakeProwl):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            state.urls.append(self.path)
            state.auth_seen.append(self.headers.get("Authorization", ""))
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            if self.path != "/admin/mcp" or state.refuse_auth or \
                    self.headers.get("Authorization") != f"Bearer {ADMIN_VALUE}":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"detail":"secret-bearing body never echoed"}')
                return
            method = body.get("method")
            if "id" not in body:
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result = {"protocolVersion": prowl.PROTOCOL_VERSION, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "prowl_admin", "version": "1"}}
            elif method == "tools/call":
                if self.headers.get("Mcp-Session-Id") != "sess-1":
                    self.send_response(400)
                    self.end_headers()
                    return
                text = state.tool(body["params"]["name"], body["params"]["arguments"])
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            else:
                result = {}
            payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Mcp-Session-Id", "sess-1")
            self.end_headers()
            self.wfile.write(f"event: message\ndata: {payload}\n\n".encode())
    return H


def put(slot, value):
    held = slot_value(slot)
    if held == value:
        return
    vault.store_value(*slot, value, replace=held is not None, tool="test")


def slot_value(slot):
    found, _env = use_secret.vault_slot(slot[0], slot[2], slot[1])
    return found.read_text(encoding="utf-8").strip() if found else None


def clear(slot):
    found, _env = use_secret.vault_slot(slot[0], slot[2], slot[1])
    if found:
        found.unlink()
        found.with_name(found.name + ".meta.json").unlink(missing_ok=True)


class ProwlDoorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = FakeProwl()
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.state))
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.state.refuse_auth = False
        put(ADMIN_SLOT, ADMIN_VALUE)
        clear(DEST_SLOT)
        os.environ.pop(prowl.ADMIN_SLOT_ENV, None)

    def run_door(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = prowl.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def issue(self, *extra):
        return self.run_door("issue", "example-agent research", "--user", "owner@example.com",
                             "--slot", "/".join(DEST_SLOT), "--admin-slot", "/".join(ADMIN_SLOT),
                             "--base", self.base, *extra)

    def test_issue_puts_the_value_in_the_slot_and_prints_none_of_it(self):
        code, out, err = self.issue()
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        raw = self.state.minted[doc["id"]]["raw"]
        self.assertEqual(slot_value(DEST_SLOT), raw)
        self.assertNotIn(raw, out + err)
        self.assertNotIn("key", doc)
        self.assertEqual(doc["slot"], "/".join(DEST_SLOT))
        self.assertEqual(doc["prefix"], raw[:12] + "...")
        self.assertEqual(doc["delivered"], "put")
        meta = pathlib.Path(use_secret.vault_slot(DEST_SLOT[0], DEST_SLOT[2], DEST_SLOT[1])[0])
        meta = meta.with_name(meta.name + ".meta.json")
        if meta.exists():
            self.assertNotIn(raw, meta.read_text(encoding="utf-8"))
        name, args = self.state.calls[-1]
        self.assertEqual(name, "admin_api_key_issue")
        self.assertEqual(args["daily_spend_limit_usd"], prowl.DEFAULT_DAILY_USD)
        self.assertNotIn("total_spend_limit_usd", args)

    def test_a_held_slot_mints_nothing_without_rotate_and_rotates_with_it(self):
        put(DEST_SLOT, "prowl_test-only-old-not-a-credential")
        before = len(self.state.minted)
        code, _out, err = self.issue()
        self.assertEqual(code, 1)
        self.assertIn("already holds a value", err)
        self.assertEqual(len(self.state.minted), before)
        code, out, err = self.issue("--rotate", "--daily-usd", "25", "--total-usd", "300")
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["delivered"], "rotate")
        self.assertEqual(slot_value(DEST_SLOT), self.state.minted[doc["id"]]["raw"])
        self.assertEqual(self.state.calls[-1][1]["daily_spend_limit_usd"], 25.0)
        self.assertEqual(self.state.calls[-1][1]["total_spend_limit_usd"], 300.0)

    def test_a_refused_delivery_revokes_the_minted_key(self):
        real = prowl.deliver

        def refuse(value, slot, rotate):
            raise prowl.DoorError("the vault refused the key; child output withheld")
        prowl.deliver = refuse
        try:
            code, out, err = self.issue()
        finally:
            prowl.deliver = real
        self.assertEqual(code, 1)
        kid = max(self.state.minted)
        self.assertIn(kid, self.state.revoked)
        self.assertIn("revoked again", err)
        self.assertNotIn(self.state.minted[kid]["raw"], out + err)
        self.assertIsNone(slot_value(DEST_SLOT))

    def test_the_admin_key_travels_only_in_the_header(self):
        self.state.auth_seen.clear()
        self.state.urls.clear()
        code, out, err = self.issue()
        self.assertEqual(code, 0, err)
        self.assertTrue(self.state.auth_seen and all(a == f"Bearer {ADMIN_VALUE}" for a in self.state.auth_seen))
        self.assertTrue(all(ADMIN_VALUE not in u for u in self.state.urls))
        self.assertNotIn(ADMIN_VALUE, out + err)

    def test_a_refused_admin_key_is_reported_by_status_without_the_body(self):
        self.state.refuse_auth = True
        before = len(self.state.minted)
        code, out, err = self.issue()
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", err)
        self.assertNotIn("secret-bearing", out + err)
        self.assertEqual(len(self.state.minted), before)

    def test_no_admin_slot_is_refused_before_any_call(self):
        n = len(self.state.calls)
        code, _out, err = self.run_door("issue", "example-agent research", "--user", "owner@example.com",
                                        "--slot", "/".join(DEST_SLOT), "--base", self.base)
        self.assertEqual(code, 1)
        self.assertIn(prowl.ADMIN_SLOT_ENV, err)
        self.assertEqual(len(self.state.calls), n)
        os.environ[prowl.ADMIN_SLOT_ENV] = "/".join(ADMIN_SLOT)
        code, _out, err = self.run_door("issue", "example-agent research", "--user", "owner@example.com",
                                        "--slot", "/".join(DEST_SLOT), "--base", self.base)
        self.assertEqual(code, 0, err)

    def test_revoke_and_list_answer_without_a_value(self):
        code, out, err = self.issue()
        self.assertEqual(code, 0, err)
        kid = json.loads(out)["id"]
        code, out, err = self.run_door("list", "--user", "owner@example.com", "--admin-slot",
                                       "/".join(ADMIN_SLOT), "--base", self.base)
        self.assertEqual(code, 0, err)
        self.assertNotIn(self.state.minted[kid]["raw"], out)
        code, out, err = self.run_door("revoke", kid, "--reason", "rotated", "--admin-slot",
                                       "/".join(ADMIN_SLOT), "--base", self.base)
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["revoked"])
        code, _out, err = self.run_door("revoke", kid, "--reason", "", "--admin-slot",
                                        "/".join(ADMIN_SLOT), "--base", self.base)
        self.assertEqual(code, 1)
        code, _out, err = self.run_door("revoke", "nokey", "--reason", "gone", "--admin-slot",
                                        "/".join(ADMIN_SLOT), "--base", self.base)
        self.assertEqual(code, 1)
        self.assertIn("no key", err)

    def test_no_origin_is_refused_before_any_call(self):
        os.environ.pop(prowl.BASE_ENV, None)
        n = len(self.state.calls)
        code, _out, err = self.run_door("issue", "example-agent research", "--user", "owner@example.com",
                                        "--slot", "/".join(DEST_SLOT), "--admin-slot", "/".join(ADMIN_SLOT))
        self.assertEqual(code, 1)
        self.assertIn(prowl.BASE_ENV, err)
        self.assertEqual(len(self.state.calls), n)
        os.environ[prowl.BASE_ENV] = self.base
        try:
            code, _out, err = self.run_door("list", "--user", "owner@example.com", "--admin-slot", "/".join(ADMIN_SLOT))
        finally:
            os.environ.pop(prowl.BASE_ENV, None)
        self.assertEqual(code, 0, err)

    def test_plain_http_off_loopback_is_refused(self):
        with self.assertRaises(prowl.DoorError):
            prowl.check_base("http://prowl.example.com")
        self.assertEqual(prowl.check_base("https://prowl.example.com/"), "https://prowl.example.com")

    def test_the_door_source_keeps_the_rules(self):
        src = (ROOT / "tools" / "prowl.py").read_text(encoding="utf-8")
        self.assertNotIn("exc.read()", src)
        self.assertNotIn('"--key"', src)
        self.assertIn("input=value", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
