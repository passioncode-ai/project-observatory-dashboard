#!/usr/bin/env python3
"""OBS-601: the key doors' helpers live in one place, every degraded entry names its code, and the key server refuses
by type.

`consumer_sources` (where the copies of a key's value are looked for) and the roll guard of a door that rolls a value
in place were written four and two times, once per door; they are `key_consumers.consumer_sources` and
`key_consumers.guard_roll` now, and no door keeps a copy. Every `degraded` entry a door or a consumer source answers
carries a `code`. The key server answers every refusal as `{"error": {"code", "message"}}`, the shape the MCP tools
answer, with the code the MCP key acts take from the same mapping. Every answer and printed line is checked to carry
no fake value.
"""
from __future__ import annotations
import ast
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import key_consumers  # noqa: E402

FAKE_VALUE = "fake-door-value-" + "Z9" * 12
FAKE_KEY = "sk-or-v1-" + "FAKE" * 16
DOORS = ("openrouter.py", "cloudflare.py", "heroku.py", "digitalocean.py")


class ValueSource:
    """A value source as the finder sees one: it compares in memory and names a place, never the value."""

    name = "fake-env"

    def __init__(self, held: str | None = None):
        self.held = held

    def find(self, old):
        if key_consumers.same(self.held, old):
            return [{"kind": "env-file", "path": "/srv/alpha-web/.env", "var": "API_KEY"}], [], []
        return [], [], []


class Broken:
    name = "broken"

    def find(self, old):
        raise OSError("cannot read")

    find_key = find


def clean(test: unittest.TestCase, *texts) -> None:
    for text in texts:
        blob = text if isinstance(text, str) else json.dumps(text, default=str)
        test.assertNotIn(FAKE_VALUE, blob)
        test.assertNotIn(FAKE_KEY, blob)


# ─────────────────────────── one copy of each helper ────────────────────────

class OneCopy(unittest.TestCase):
    def test_no_door_keeps_its_own_copy(self):
        for door in DOORS:
            with self.subTest(door=door):
                text = (ROOT / "tools" / door).read_text(encoding="utf-8")
                defined = {n.name for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef)}
                self.assertFalse(defined & {"consumer_sources", "_roll_guard"}, defined & {"consumer_sources",
                                                                                         "_roll_guard"})
                self.assertTrue("key_consumers.consumer_sources(" in text or "key_consumers.guard_roll(" in text)

    def test_the_sources_name_the_slot_and_leave_out_the_excluded_file(self):
        for slot, want in (("alpha/prod/K", ("alpha", "prod", "K")), (("alpha", "prod", "K"), ("alpha", "prod", "K")),
                           (None, None), ("", None)):
            with self.subTest(slot=slot):
                sources = key_consumers.consumer_sources(slot)
                self.assertEqual([s.name for s in sources], ["heroku", "env-file", "launchd"])
                self.assertEqual(sources[2].slot, want)
                self.assertEqual(sources[1].exclude, set())
        sources = key_consumers.consumer_sources(None, exclude=Path("/srv/alpha-web/token"))
        self.assertEqual(sources[1].exclude, {str(Path("/srv/alpha-web/token"))})

    def test_a_roll_of_a_file_reads_it_in_memory_and_names_the_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp).resolve() / "example"
            import private_io
            private_io.write(token, FAKE_VALUE + "\n")
            seen = []

            def sources(slot=None, exclude=None):
                seen.append((slot, exclude))
                return [ValueSource(held=FAKE_VALUE)]
            with patch.object(key_consumers, "consumer_sources", sources):
                refuse, restart = key_consumers.guard_roll(path=token)
            self.assertEqual(seen, [(None, token)])
            self.assertTrue(refuse and "/srv/alpha-web/.env" in refuse[0], refuse)
            self.assertEqual(restart, [])
            clean(self, refuse, restart)

    def test_an_unreadable_file_refuses_and_looks_for_nothing(self):
        called = []
        with patch.object(key_consumers, "consumer_sources", lambda *a, **k: called.append(a) or []):
            refuse, restart = key_consumers.guard_roll(path=Path("/nonexistent/door-helpers/token"))
        self.assertEqual(called, [])
        self.assertEqual(len(refuse), 1)
        self.assertIn("token file could not be read (", refuse[0])

    def test_a_roll_of_a_slot_reads_the_vault_in_memory(self):
        seen = []

        def sources(slot=None, exclude=None):
            seen.append((slot, exclude))
            return [ValueSource(held="another-value")]
        with patch.object(key_consumers, "vault_value", lambda *s: FAKE_VALUE), \
                patch.object(key_consumers, "consumer_sources", sources):
            refuse, restart = key_consumers.guard_roll(slot="alpha/prod/K")
        self.assertEqual((refuse, restart, seen), ([], [], [("alpha/prod/K", None)]))

        def unreadable(*slot):
            raise RuntimeError(FAKE_VALUE)
        with patch.object(key_consumers, "vault_value", unreadable):
            refuse, _ = key_consumers.guard_roll(slot="alpha/prod/K")
        self.assertEqual(refuse, ["source destination could not be read: the slot could not be read (RuntimeError)"])
        clean(self, refuse)

    def test_a_roll_names_one_subject(self):
        with self.assertRaises(ValueError):
            key_consumers.guard_roll()


# ─────────────────────────── every degraded entry has a code ────────────────

class FakeApi:
    def __init__(self, apps=None, fail_list=False, configs=None):
        self.apps, self.fail_list, self.configs = apps or [], fail_list, configs or {}

    def list(self, path, tok):
        if self.fail_list:
            raise RuntimeError("heroku answered HTTP 500; provider response withheld")
        return self.apps

    def request(self, method, path, tok, body=None):
        app = path.split("/")[2]
        got = self.configs.get(app)
        if isinstance(got, Exception):
            raise got
        return got


class Codes(unittest.TestCase):
    def codes(self, entries) -> list:
        return [d.get("code") for d in entries]

    def test_heroku_names_each_unread_place(self):
        src = key_consumers.HerokuSource(api=FakeApi(fail_list=True), tokens=lambda: (["t"], None, None))
        self.assertEqual(self.codes(src.find(FAKE_VALUE)[1]), ["app-list-unreadable"])
        src = key_consumers.HerokuSource(api=FakeApi(apps=[{"id": "a1", "name": "alpha-web"},
                                                           {"id": "a2", "name": "beta-api"}],
                                                     configs={"a1": RuntimeError("HTTP 403"), "a2": ["no", "shape"]}),
                                         tokens=lambda: (["t"], None, None))
        self.assertEqual(self.codes(src.find(FAKE_VALUE)[1]), ["config-unreadable", "config-unshaped"])
        src = key_consumers.HerokuSource(api=FakeApi(), tokens=lambda: ([], None, "the token store could not be read"))
        self.assertEqual(self.codes(src.find(FAKE_VALUE)[1]), ["token-unreadable"])
        src = key_consumers.HerokuSource(api=FakeApi(), tokens=lambda: ([], "the integration is off", None))
        self.assertEqual(self.codes(src.find(FAKE_VALUE)[2]), ["not-in-use"])

    def test_env_files_name_each_unread_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = Path(tmp) / "env.json"
            inv.write_text("{not json", encoding="utf-8")
            src = key_consumers.EnvFileSource(inventory=inv)
            self.assertEqual(self.codes(src.find(FAKE_VALUE)[1]), ["inventory-unreadable"])
            folder = Path(tmp) / "folder.env"
            folder.mkdir()
            inv.write_text(json.dumps({"root": "/", "files": [{"path": str(folder).lstrip("/")}],
                                       "degraded": [{"path": "x", "reason": "unreadable"}]}), encoding="utf-8")
            self.assertEqual(self.codes(src.find(FAKE_VALUE)[1]), ["inventory-incomplete", "file-unreadable"])

    def test_services_name_their_code(self):
        def boom(*slot):
            raise OSError("nope")
        self.assertEqual(self.codes(key_consumers.LaunchdSource(("a", "b", "C"), consumers=boom).find("x")[1]),
                         ["services-unreadable"])
        self.assertEqual(self.codes(key_consumers.LaunchdSource(None).find("x")[2]), ["no-slot"])

    def test_the_finder_and_the_guard_name_a_failed_source_the_same_way(self):
        self.assertEqual(self.codes(key_consumers.find("x", [Broken()])["degraded"]), ["source-failed"])
        with self.assertRaises(key_consumers.RevokeRefused) as cm:
            key_consumers.revoke_guard("alpha", {}, [Broken()], value="x")
        self.assertEqual(self.codes(cm.exception.unread), ["source-failed"])

    def test_the_openrouter_door_names_its_codes(self):
        import openrouter
        with patch.object(openrouter, "destination_value", lambda to: (None, "the destination could not be read")):
            old, found = openrouter.find_consumers("vault:alpha/prod/K")
        self.assertEqual((old, self.codes(found["degraded"])), (None, ["destination-unreadable"]))

        def request(path, admin):
            if path == "/keys/h-1":
                raise RuntimeError("openrouter answered HTTP 500; provider response withheld")
            if path == "/keys/h-2":
                return {"data": {}}
            if path.startswith("/keys/"):
                return {"data": {"workspace_id": "ws-" + path[-1]}}
            if path == "/byok?workspace_id=ws-3":
                raise RuntimeError("openrouter answered HTTP 500; provider response withheld")
            return {"data": [{"provider": openrouter.BYOK_PROVIDER, "id": "c1"},
                             {"provider": openrouter.BYOK_PROVIDER, "id": "c2"}]}
        issued = {f"k{i}": {"hash": f"h-{i}", "account": "acct"} for i in (1, 2, 3, 4)}
        with patch.object(openrouter, "_request", request), \
                patch.object(openrouter, "read_admin", lambda acc: ("admin", FAKE_KEY)), \
                patch.object(openrouter, "byok_exclusions", lambda: []):
            plan = openrouter.byok_plan(issued)
        self.assertEqual(sorted(set(self.codes(plan["degraded"]))),
                         ["credentials-unreadable", "key-unreadable", "no-workspace", "several-credentials"])
        clean(self, plan)

    def test_every_degraded_literal_in_the_doors_carries_a_code(self):
        for rel in DOORS + ("key_consumers.py",):
            with self.subTest(file=rel):
                tree = ast.parse((ROOT / "tools" / rel).read_text(encoding="utf-8"))
                missing = []
                for node in ast.walk(tree):
                    entries = []
                    if isinstance(node, ast.Dict):
                        for k, v in zip(node.keys, node.values):
                            if isinstance(k, ast.Constant) and k.value == "degraded" and isinstance(v, ast.List):
                                entries += v.elts
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "append" and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == "degraded"):
                        entries += node.args
                    for e in entries:
                        if isinstance(e, ast.Dict) and not any(isinstance(k, ast.Constant) and k.value == "code"
                                                               for k in e.keys):
                            missing.append(e.lineno)
                self.assertEqual(missing, [], f"{rel}: degraded entries without a code at lines {missing}")


# ─────────────────────────── the key server refuses by type ─────────────────

with tempfile.TemporaryDirectory(prefix="observatory-door-helpers-") as _home:
    with patch.dict(os.environ, {"OBSERVATORY_HOME": str(Path(_home).resolve())}):
        import keyserver  # noqa: E402


def raising(exc):
    def act(body):
        raise exc
    return act


class TypedRefusals(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix="observatory-door-helpers-ks-")
        root = Path(self.sandbox.name)
        self.token = "-".join(("only", "a", "synthetic", "test", "token"))
        self.patches = [patch.object(keyserver, "TOKEN_FILE", root / "private" / "token"),
                        patch.object(keyserver, "AUDIT", root / "audit" / "journal.jsonl"),
                        patch.object(keyserver.paths, "REGISTRY", root / "registry"),
                        patch.object(keyserver.paths, "SCRATCH", root / "scratch"),
                        patch.object(keyserver.paths, "DASHBOARD_DIR", root / "dashboard"),
                        patch.object(keyserver.paths, "DASHBOARD_HTML", root / "dashboard" / "index.html")]
        refused = key_consumers.RevokeRefused(key_consumers.IN_USE, "alpha is in use",
                                              consumers=[{"kind": "env-file", "path": "/srv/alpha-web/.env"}])
        self.patches.append(patch.dict(keyserver.ACTIONS, {
            "sentence": raising(ValueError("a label is required")),
            "coded": raising(ValueError("agent-disabled")),
            "missing": raising(LookupError("no issued key called 'ghost'")),
            "held": raising(LookupError("held: a copy is out of reach")),
            "lost": raising(RuntimeError(f"the vault kept no receipt for {FAKE_KEY}")),
            "broken": raising(TypeError(FAKE_VALUE)),
            "provider": raising(urllib.error.HTTPError("https://example.invalid", 429, "busy", {}, None)),
            "inuse": raising(refused)}))
        for p in self.patches:
            p.start()
        self.server = keyserver.Server(("127.0.0.1", 0), keyserver.Handler, self.token)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for p in reversed(self.patches):
            p.stop()
        self.sandbox.cleanup()

    def request(self, method="POST", path="/api/sentence", headers=None, body=b"{}", token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        sent = {**({"X-Observatory-Token": self.token} if token else {}), **(headers or {})}
        try:
            conn.request(method, path, body=body if method == "POST" else None, headers=sent)
            r = conn.getresponse()
            raw = r.read().decode("utf-8")
            return r.status, json.loads(raw), raw
        finally:
            conn.close()

    def assertTyped(self, got, status, code):
        status_got, body, raw = got
        self.assertEqual(status_got, status, raw)
        self.assertIsInstance(body.get("error"), dict, raw)
        self.assertEqual(body["error"]["code"], code, raw)
        self.assertIsInstance(body["error"].get("message"), str, raw)
        self.assertTrue(body["error"]["message"], raw)
        clean(self, raw)
        return body

    def test_the_request_refusals_are_typed(self):
        port = self.port
        self.assertTyped(self.request(token=False), 401, "unauthenticated")
        self.assertTyped(self.request(headers={"Host": "example.invalid"}), 403, "host-not-loopback")
        self.assertTyped(self.request(headers={"Origin": "http://example.invalid"}), 403, "origin-not-same")
        self.assertTyped(self.request(path="/elsewhere"), 404, "no-such-path")
        self.assertTyped(self.request("GET", "/elsewhere"), 404, "no-such-path")
        self.assertTyped(self.request(path="/api/nothing-here"), 404, "unknown-action")
        body = self.assertTyped(self.request(path="/api/put"), 403, "stdin-only")
        self.assertIn("vault.py", body["error"]["message"])
        self.assertTyped(self.request(body=b"[1]"), 400, "invalid-body")
        self.assertTyped(self.request(body=b"x" * (keyserver.BODY_LIMIT + 1)), 413, "body-too-large")
        self.assertTyped(self.request("GET", "/dashboard/" + "missing.css"), 404, "no-such-path")
        self.assertTyped(self.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"}), 404, "not-built")

    def test_an_acts_exception_is_answered_by_its_type(self):
        self.assertTyped(self.request(path="/api/sentence"), 400, "invalid-argument")
        self.assertTyped(self.request(path="/api/coded"), 400, "agent-disabled")
        self.assertTyped(self.request(path="/api/missing"), 404, "not-found")
        self.assertTyped(self.request(path="/api/held"), 404, "held")
        self.assertTyped(self.request(path="/api/lost"), 500, "unconfirmed")
        self.assertTyped(self.request(path="/api/broken"), 500, "failed")
        self.assertTyped(self.request(path="/api/provider"), 502, "provider-refused")
        body = self.assertTyped(self.request(path="/api/inuse"), 400, key_consumers.IN_USE)
        self.assertEqual(body["error"]["consumers"], [{"kind": "env-file", "path": "/srv/alpha-web/.env"}])
        self.assertEqual((body["code"], body["unread"]), (key_consumers.IN_USE, []), "the fields R1 published stay")

    def test_the_mcp_key_acts_take_the_same_codes(self):
        for exc in (ValueError("a label is required"), ValueError("agent-disabled"), LookupError("held: x"),
                    LookupError("the provider refused it"), RuntimeError("x"), TypeError("x"),
                    urllib.error.HTTPError("https://example.invalid", 429, "busy", {}, None)):
            with self.subTest(exc=type(exc).__name__):
                status, code = keyserver.refusal(exc)
                self.assertIsInstance(status, int)
                import key_acts
                with patch.dict(keyserver.ACTIONS, {"mint": raising(exc)}):
                    with self.assertRaises(key_acts.Refusal) as cm:
                        key_acts._run("mint", {}, None)
                self.assertEqual(cm.exception.code, code)


if __name__ == "__main__":
    unittest.main()
