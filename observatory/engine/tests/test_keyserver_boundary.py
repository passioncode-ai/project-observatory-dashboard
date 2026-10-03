#!/usr/bin/env python3
"""Synthetic keyserver boundary checks; never read live inventories or keys."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
with tempfile.TemporaryDirectory(prefix="observatory-import-boundary-") as import_home:
    with patch.dict(os.environ, {"OBSERVATORY_HOME": str(Path(import_home).resolve())}):
        import keyserver


class KeyserverBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix="observatory-keyserver-boundary-")
        self.root = Path(self.sandbox.name)
        self.token = "-".join(("only", "a", "synthetic", "test", "token"))
        self.patches = [
            patch.object(keyserver, "TOKEN_FILE", self.root / "private" / "token"),
            patch.object(keyserver, "AUDIT", self.root / "audit" / "journal.jsonl"),
            patch.object(keyserver.paths, "REGISTRY", self.root / "registry"),
            patch.object(keyserver.paths, "SCRATCH", self.root / "scratch"),
            patch.object(keyserver.paths, "DATA", self.root / "projects"),
        ]
        for item in self.patches:
            item.start()
        self.server = keyserver.Server(("127.0.0.1", 0), keyserver.Handler, self.token)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.effects = []
        self.action = patch.dict(keyserver.ACTIONS, {"probe": lambda body: self.effects.append(body) or {"ok": True}})
        self.action.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.action.stop()
        for item in reversed(self.patches):
            item.stop()
        self.sandbox.cleanup()

    def request(self, method="POST", path="/api/probe", headers=None, body=b"{}"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        all_headers = {"X-Observatory-Token": self.token, **(headers or {})}
        try:
            conn.request(method, path, body=body if method == "POST" else None, headers=all_headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_same_origin_and_cli_reach_action(self):
        for host in ("127.0.0.1", "localhost"):
            authority = f"{host}:{self.port}"
            code, _, _ = self.request(headers={"Host": authority, "Origin": f"http://{authority}"})
            self.assertEqual(code, 200)
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(len(self.effects), 3)

    def test_origin_prefix_and_different_port_refused_before_effects(self):
        for origin in (
            "http://localhost.evil.invalid", "http://127.0.0.1.evil.invalid",
            "http://" + f"localhost:{self.port}" + "@evil.invalid", "null",
            "http://127.0.0.1:1", f"https://127.0.0.1:{self.port}",
            f"http://127.0.0.1:{self.port}/", f"http://127.0.0.1:{self.port}?x=1",
            f"http://127.0.0.1:{self.port}#fragment", f"http://localhost:{self.port}",
        ):
            with self.subTest(origin=origin):
                self.assertEqual(self.request(headers={"Origin": origin})[0], 403)
        self.assertEqual(self.effects, [])
        self.assertFalse(keyserver.AUDIT.exists())

    def test_a_declared_caller_is_a_label_never_an_authority(self):
        # docs/design/ACCESS.md: `X-Observatory-Caller` is what the client says it
        # is. Without the token it opens nothing; with it, every name gets the
        # same rights, and the name is only written beside the action.
        code, _, _ = self.request(headers={"X-Observatory-Token": "", "X-Observatory-Caller": "operator"})
        self.assertEqual(code, 401)
        self.assertEqual(self.effects, [])
        seen = []
        with patch.dict(keyserver.ACTIONS, {"probe": lambda body: seen.append(keyserver._CALLER.get()) or {"ok": True}}):
            for name in ("operator", "agent:some-session", "unnamed"):
                self.assertEqual(self.request(headers={"X-Observatory-Caller": name})[0], 200)
        self.assertEqual(seen, ["operator", "agent:some-session", "unnamed"])
        # A raw client can put anything in the header; the journal gets one token.
        forged = keyserver.caller_name('x\n{"by":"root"} ' + "y" * 200)
        self.assertNotIn("\n", forged)
        self.assertLessEqual(len(forged), 80)

    def test_the_audit_names_the_caller(self):
        # The caller header travels into the audit row as a label, and the row
        # says so itself: whoever reads the journal later must not take
        # `caller` for an authenticated identity. The only thing the server
        # proved is that the request carried this workspace's token.
        def echo(body):
            keyserver.audit("fixture.echo", "fixture-subject", {})
            return {"caller": keyserver._CALLER.get()}
        with patch.dict(keyserver.ACTIONS, {"probe": echo}):
            code, _, body = self.request(headers={"X-Observatory-Caller": "session:abc"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["caller"], "session:abc")
        receipt = json.loads(keyserver.AUDIT.read_text().splitlines()[-1])
        self.assertEqual(receipt["caller"], "session:abc")
        self.assertIs(receipt["caller_verified"], False)
        self.assertEqual(receipt["principal"], "local-token-holder")

    def test_a_cors_preflight_is_never_granted(self):
        # A cross-origin page can send the token header only after a preflight;
        # the keyserver answers none, so the browser never sends the action.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request("OPTIONS", "/api/probe", headers={
            "Origin": "http://attacker.invalid", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-observatory-token"})
        response = conn.getresponse()
        headers = {k.lower() for k, _ in response.getheaders()}
        conn.close()
        self.assertGreaterEqual(response.status, 400)
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertEqual(self.effects, [])

    def test_another_workspaces_token_is_refused(self):
        # Two workspaces, two servers, two tokens: a page or agent holding one
        # workspace's token gets nothing from the other.
        other = keyserver.Server(("127.0.0.1", 0), keyserver.Handler, "-".join(("a", "second", "workspace", "token")))
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", other.server_address[1], timeout=3)
            conn.request("POST", "/api/probe", body=b"{}", headers={"X-Observatory-Token": self.token})
            self.assertEqual(conn.getresponse().status, 401)
            conn.close()
        finally:
            other.shutdown(); other.server_close(); thread.join(timeout=2)
        self.assertEqual(self.effects, [])

    def test_rebinding_host_cannot_obtain_page_token(self):
        with patch.object(keyserver.Handler, "_page_for") as page:
            for host in ("evil.invalid", "localhost.evil.invalid", "127.0.0.1:1", "localhost", ""):
                code, _, raw = self.request("GET", "/", headers={"Host": host})
                self.assertEqual(code, 403)
                self.assertNotIn(self.token.encode(), raw)
            page.assert_not_called()

    def test_duplicate_host_origin_and_token_are_refused(self):
        for name, value in (("Host", f"127.0.0.1:{self.port}"),
                            ("Origin", f"http://127.0.0.1:{self.port}"),
                            (keyserver.HEADER, self.token)):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
            try:
                conn.putrequest("POST", "/api/probe", skip_host=True)
                conn.putheader("Host", f"127.0.0.1:{self.port}")
                conn.putheader(keyserver.HEADER, self.token)
                if name == "Origin":
                    conn.putheader("Origin", value)
                conn.putheader(name, value)
                conn.putheader("Content-Length", "2")
                conn.endheaders(b"{}")
                response = conn.getresponse()
                self.assertIn(response.status, (401, 403))
                response.read()
            finally:
                conn.close()
        self.assertEqual(self.effects, [])

    def test_token_is_checked_with_compare_digest(self):
        with patch.object(keyserver.hmac, "compare_digest", wraps=keyserver.hmac.compare_digest) as compare:
            self.assertEqual(self.request(headers={keyserver.HEADER: "wrong"})[0], 401)
            compare.assert_called_once()
        self.assertEqual(self.effects, [])

    def test_bad_json_and_body_bounds_do_not_reach_action(self):
        for body in (b"[]", b"null", b'"text"', b'{"limit": NaN}', b'{"limit": Infinity}', b"\xff"):
            self.assertEqual(self.request(body=body)[0], 400)
        self.assertEqual(self.request(headers={"Content-Length": "-1"})[0], 400)
        self.assertEqual(self.request(headers={"Content-Length": "65537"})[0], 413)
        self.assertEqual(self.request(headers={"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.effects, [])

    def test_token_page_is_uncacheable_and_not_frameable(self):
        page = self.root / "page.html"
        page.write_text("<!doctype html><html><head></head><body>fixture</body></html>")
        with patch.object(keyserver.Handler, "_page_for", return_value=(page, "text/html")):
            code, headers, raw = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn(self.token.encode(), raw)

    def test_empty_tokens_and_routable_bind_are_refused(self):
        for address, tok in (("0.0.0.0", self.token), ("127.0.0.1", "")):
            with self.assertRaises(ValueError):
                keyserver.Server((address, 0), keyserver.Handler, tok)

    def test_token_created_private_reused_and_symlinks_refused(self):
        import runtime_identity
        with self.assertRaises(runtime_identity.IdentityError):
            keyserver.token()
        self.assertFalse(keyserver.TOKEN_FILE.exists(), "a read must never mint a token")
        keyserver.TOKEN_FILE.parent.mkdir(mode=0o700)
        first = runtime_identity.load(keyserver.TOKEN_FILE, "keyserver-token", initialize=True)
        self.assertEqual(first, keyserver.token())
        self.assertEqual(first, runtime_identity.load(keyserver.TOKEN_FILE, "keyserver-token",
                                                      initialize=True), "init never replaces")
        self.assertEqual(keyserver.TOKEN_FILE.stat().st_mode & 0o777, 0o600)
        target = self.root / "unrelated"
        target.write_text("untouched")
        keyserver.TOKEN_FILE.unlink()
        keyserver.TOKEN_FILE.symlink_to(target)
        with self.assertRaises(runtime_identity.IdentityError):
            keyserver.token()
        self.assertEqual(target.read_text(), "untouched")

    def test_empty_and_publicly_readable_token_files_refused(self):
        import runtime_identity
        keyserver.TOKEN_FILE.parent.mkdir(mode=0o700)
        keyserver.TOKEN_FILE.write_text("")
        keyserver.TOKEN_FILE.chmod(0o600)
        with self.assertRaises(runtime_identity.IdentityError):
            keyserver.token()
        self.assertEqual(keyserver.TOKEN_FILE.read_text(), "", "a refused file is left as it was")
        keyserver.TOKEN_FILE.write_text("A" * 43)
        keyserver.TOKEN_FILE.chmod(0o644)
        with self.assertRaises(runtime_identity.IdentityError):
            keyserver.token()

    def test_nonfinite_limits_refused_before_audit_or_provider(self):
        with patch.object(keyserver, "_door") as door:
            for value in ("NaN", "Infinity", "-Infinity"):
                for action, body in ((keyserver.act_mint, {"destination": "observatory", "limit": value}),
                                     (keyserver.act_limit, {"label": "fixture", "limit": value})):
                    with self.assertRaises(ValueError):
                        action(body)
            door.assert_not_called()
        self.assertFalse(keyserver.AUDIT.exists())

    def test_annotation_audit_does_not_copy_free_text_or_value(self):
        import sign_credential
        synthetic = "sk-" + "f" * 48
        with patch.object(sign_credential, "write", side_effect=ValueError("refused")):
            with self.assertRaises(ValueError):
                keyserver.act_annotate({"id": "fixture", "purpose": synthetic, "owner": synthetic})
        text = keyserver.AUDIT.read_text()
        self.assertNotIn(synthetic, text)
        self.assertEqual(json.loads(text)["action"], "annotate")
        self.assertEqual(keyserver.AUDIT.stat().st_mode & 0o777, 0o600)

    def test_accidental_value_shaped_label_is_redacted_in_audit(self):
        synthetic = "sk-" + "a" * 48
        keyserver.audit("fixture", synthetic, {})
        record = json.loads(keyserver.AUDIT.read_text())
        self.assertEqual(record["subject"], "[redacted]")
        self.assertNotIn(synthetic, keyserver.AUDIT.read_text())

    def test_audit_scrub_uses_the_shared_shapes_and_keeps_a_session_caller(self):
        """The scrub caught only `sk-…` and 40+ runs: a 37-character token sent
        as an annotate id sat unredacted in the journal, and UUID or 32-hex keys
        passed. An agent's session id IS a UUID, and naming the caller is the
        journal's point, so the caller keeps its UUID."""
        shapes = {"mixed37": "Fake" + "Tokn" + "_" + "Ab3" * 9 + "x",
                  "uuid": "-".join(("0f" * 4, "1a2b", "3c4d", "5e6f", "7a" * 6)),
                  "hex32": "0123456789abcdef" * 2}
        session = "-".join(("1b" * 4, "2c3d", "4e5f", "6a7b", "8c" * 6))
        token = keyserver._CALLER.set(session)
        try:
            for label, value in shapes.items():
                keyserver.audit("fixture", f"credential:{value}", {"label": f"see {value} here"})
        finally:
            keyserver._CALLER.reset(token)
        text = keyserver.AUDIT.read_text()
        for label, value in shapes.items():
            self.assertNotIn(value, text, label)
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertTrue(all(r["caller"] == session for r in rows), rows[0]["caller"])
        self.assertTrue(all("[redacted]" in r["subject"] and r["label"].startswith("see ") for r in rows))

    def test_annotate_refuses_a_value_shaped_id_before_the_audit_row(self):
        synthetic = "Fake" + "Tokn" + "_" + "Ab3" * 9 + "x"
        with self.assertRaises(ValueError) as caught:
            keyserver.act_annotate({"id": synthetic, "purpose": "p", "evidence": "e"})
        self.assertNotIn(synthetic, str(caught.exception))
        self.assertFalse(keyserver.AUDIT.exists(), "a refused id must not become a journal row")

    def _vault_env(self):
        store = self.root / "vault" / "projects"
        return store, patch.dict(os.environ, {"OBSERVATORY_VAULT_DIR": str(store)})

    def test_leak_route_cannot_be_turned_into_vault_options(self):
        """Body fields were handed to vault.py as positional argv: `project:
        "--help"` printed help, exited 0 and the route answered 200 `marked`
        with nothing recorded; `where: "--force"` came back as usage text."""
        store, env = self._vault_env()
        with env:
            for body in ({"project": "--help", "env": "prod", "name": "CF_API_TOKEN",
                          "where": "somewhere long enough"},
                         {"project": "alpha-web", "env": "--force", "name": "CF_API_TOKEN",
                          "where": "somewhere long enough"},
                         {"project": "alpha-web", "env": "prod", "name": "-h",
                          "where": "somewhere long enough"},
                         {"project": ["alpha-web"], "env": "prod", "name": "CF_API_TOKEN",
                          "where": "somewhere long enough"}):
                with self.assertRaises(ValueError, msg=str(body)):
                    keyserver.act_leak(body)
            self.assertFalse((store / "leaks.jsonl").exists(), "a refused body recorded something")
            out = keyserver.act_leak({"project": "alpha-web", "env": "prod", "name": "CF_API_TOKEN",
                                      "where": "--force pushed to a fork, seen in its CI log"})
            self.assertEqual(out["marked"], "alpha-web/prod/CF_API_TOKEN")
            rows = [json.loads(line) for line in (store / "leaks.jsonl").read_text().splitlines()]
            self.assertEqual([r["where"] for r in rows], ["--force pushed to a fork, seen in its CI log"])

    def test_leak_route_needs_the_vaults_receipt(self):
        """Exit 0 is not a receipt: a vault that printed help and exited 0 was
        reported as a recorded leak."""
        silent = self.root / "silent_vault.py"
        silent.write_text("import sys\nsys.exit(0)\n")
        store, env = self._vault_env()
        with env, patch.object(keyserver, "VAULT", silent):
            with self.assertRaises(RuntimeError):
                keyserver.act_leak({"project": "alpha-web", "env": "prod", "name": "CF_API_TOKEN",
                                    "where": "a CI log, job 4412"})

    def test_refusals_never_quote_a_credential_shaped_input(self):
        """Unknown action path, reveal name, revoke label (the door's own
        sentence) and mint destination all quoted the caller's value back."""
        planted = "sk-or-v1-" + "FAKE" * 10
        code, _, raw = self.request(path="/api/" + planted)
        self.assertEqual(code, 404)
        self.assertNotIn(planted.encode(), raw)
        keyserver.paths.SCRATCH.mkdir()
        (keyserver.paths.SCRATCH / "env.json").write_text(json.dumps({"files": [
            {"path": "fixture.env", "variables": [{"name": "EXAMPLE_KEY", "class": "fixture"}]}]}))
        for body in ({"path": "fixture.env", "name": planted}, {"path": planted, "name": "EXAMPLE_KEY"}):
            code, _, raw = self.request(path="/api/reveal", body=json.dumps(body).encode())
            self.assertEqual(code, 400)
            self.assertNotIn(planted.encode(), raw)
        with patch.object(keyserver, "_door") as door:
            door.return_value.revoke_key.side_effect = LookupError(f"no issued key called {planted!r}")
            code, _, raw = self.request(path="/api/revoke", body=json.dumps({"label": planted}).encode())
        self.assertEqual(code, 404)
        self.assertNotIn(planted.encode(), raw)
        code, _, raw = self.request(path="/api/mint", body=json.dumps(
            {"destination": planted, "limit": 1}).encode())
        self.assertEqual(code, 400)
        self.assertNotIn(planted.encode(), raw)
        code, _, raw = self.request(path="/api/no-such-action")
        self.assertIn(b"no-such-action", raw, "a plain identifier is still quoted")

    def test_startup_lines_reach_a_redirected_stdout_at_once(self):
        """The URL and audit-path lines sat in a block buffer when stdout was a
        file or a pipe, and were lost when the server was stopped."""
        import select
        import subprocess
        state = self.root / "state"
        env = {**os.environ, "OBSERVATORY_STATE": str(state)}
        init = subprocess.run([sys.executable, str(ROOT / "tools/runtime_identity.py"), "init",
                               "keyserver-token"], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(init.returncode, 0, init.stderr)
        proc = subprocess.Popen([sys.executable, str(ROOT / "tools/keyserver.py"), "--port", "0"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            seen, fd = b"", proc.stdout.fileno()
            import time
            deadline = time.monotonic() + 15
            while b"audit:" not in seen and time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
                if not ready:
                    break
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                seen += chunk
            text = seen.decode()
            self.assertIn("keyserver on http://127.0.0.1:", text, "nothing reached stdout while the server ran")
            self.assertIn("audit:", text)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            proc.stdout.close()
            proc.stderr.close()

    def test_explicit_reveal_is_inventory_scoped_and_audited_without_value(self):
        keyserver.paths.SCRATCH.mkdir()
        keyserver.paths.DATA.mkdir()
        synthetic = "synthetic-" + "b" * 32
        target = keyserver.paths.DATA / "fixture.env"
        target.write_text("EXAMPLE_KEY=" + synthetic + "\n")
        (keyserver.paths.SCRATCH / "env.json").write_text(json.dumps({"files": [
            {"path": "fixture.env", "variables": [{"name": "EXAMPLE_KEY", "class": "fixture"}]}]}))
        code, _, raw = self.request(path="/api/reveal", body=json.dumps(
            {"path": "fixture.env", "name": "EXAMPLE_KEY"}).encode())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["value"], synthetic)
        self.assertNotIn(synthetic, keyserver.AUDIT.read_text())
        self.assertEqual(json.loads(keyserver.AUDIT.read_text())["action"], "reveal")
        code, _, raw = self.request(path="/api/reveal", body=json.dumps(
            {"path": "other.env", "name": "EXAMPLE_KEY"}).encode())
        self.assertEqual(code, 400)
        self.assertNotIn(synthetic.encode(), raw)

    def test_symlink_audit_refuses_before_provider(self):
        target = self.root / "unrelated"
        target.write_text("untouched")
        keyserver.AUDIT.parent.mkdir()
        keyserver.AUDIT.symlink_to(target)
        with patch.object(keyserver, "_door") as door:
            with self.assertRaises(OSError):
                keyserver.act_limit({"label": "fixture", "limit": 1})
            door.assert_not_called()
        self.assertEqual(target.read_text(), "untouched")

    def test_unexpected_exception_does_not_reflect_sensitive_detail(self):
        synthetic = "private-" + "v" * 48
        def fail(body):
            raise RuntimeError(synthetic)
        with patch.dict(keyserver.ACTIONS, {"probe": fail}):
            code, _, raw = self.request()
        self.assertEqual(code, 500)
        self.assertNotIn(synthetic.encode(), raw)


if __name__ == "__main__":
    unittest.main()
