#!/usr/bin/env python3
"""OBS-594 (CP-11): every key revoke goes through one consumer guard, `key_consumers.revoke_guard`.

The guard finds consumers by fingerprint (a key's hash or label in the OpenRouter scan, a value compared in memory by
the value sources, a cluster an App Platform app binds) and refuses with a typed code before the provider is asked:
`consumer-in-use` names each consumer, `consumer-source-unreadable` names each source it could not read (a missing or
unreadable scan included), `consumer-source-unknown` a source it does not know. Each revoke path is driven here with
fake providers and fake values: the OpenRouter door's `revoke`, the key server's `revoke`, the MCP key act,
`tools/revoke_key.py` and the DigitalOcean token revoke. Every answer, refusal and printed line is checked to carry
no fake value.
"""
from __future__ import annotations
import contextlib
import datetime
import io
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

import key_consumers  # noqa: E402

FAKE_VALUE = "fake-consumer-value-" + "Q7" * 12
FAKE_PROVISIONING = "sk-or-v1-" + "FAKE" * 16


def words(action: str, target: str) -> dict:
    at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {"by": "operator", "via": "relayed-instruction", "action": action, "target": target,
            "instruction": "do it", "at": at}


class ValueSource:
    """A value source as the guard sees one: it compares in memory and names a place, never the value."""

    name = "fake-env"

    def __init__(self, held: str | None = None, fail: bool = False):
        self.held, self.fail = held, fail

    def find(self, old):
        if self.fail:
            raise OSError("cannot read")
        if key_consumers.same(self.held, old):
            return [{"kind": "env-file", "path": "/srv/alpha-web/.env", "var": "API_KEY"}], [], []
        return [], [], []


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.scratch = self.base / "scratch"
        self.scratch.mkdir()
        self.patches = [patch.object(key_consumers.paths, "SCRATCH", self.scratch)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def scan(self, doc) -> None:
        (self.scratch / "openrouter.json").write_text(doc if isinstance(doc, str) else json.dumps(doc),
                                                      encoding="utf-8")

    def assertClean(self, *texts) -> None:
        for text in texts:
            blob = text if isinstance(text, str) else json.dumps(text, default=str)
            self.assertNotIn(FAKE_VALUE, blob)
            self.assertNotIn(FAKE_PROVISIONING, blob)


class Guard(Scratch):
    def refused(self, key, sources, value=None) -> key_consumers.RevokeRefused:
        with self.assertRaises(key_consumers.RevokeRefused) as cm:
            key_consumers.revoke_guard("alpha-web-key", key, sources, value=value)
        self.assertClean(str(cm.exception), cm.exception.answer())
        return cm.exception

    def test_a_missing_scan_refuses_instead_of_skipping_the_check(self):
        exc = self.refused({"labels": {"alpha-web-key"}}, ["openrouter-scan"])
        self.assertEqual(exc.code, key_consumers.SOURCE_UNREADABLE)
        self.assertEqual([d["code"] for d in exc.unread], ["scan-missing"])
        self.assertIn("project-observatory full openrouter", str(exc))

    def test_an_unreadable_or_misshapen_scan_refuses(self):
        for doc in ("{not json", json.dumps([1, 2]), json.dumps({"keys": {"a": 1}})):
            self.scan(doc)
            exc = self.refused({"labels": {"alpha-web-key"}}, [key_consumers.OpenRouterScanSource()])
            self.assertEqual((exc.code, exc.unread[0]["code"]), (key_consumers.SOURCE_UNREADABLE, "scan-unreadable"))

    def test_an_unknown_source_or_none_refuses(self):
        self.scan({"keys": []})
        for sources in (["openrouter-scan", "carrier-pigeon"], [], [object()]):
            exc = self.refused({"labels": {"alpha-web-key"}}, sources)
            self.assertEqual(exc.code, key_consumers.SOURCE_UNKNOWN)

    def test_a_consumer_is_found_by_hash_by_label_and_by_serves_and_named(self):
        for doc, who in (({"keys": [], "destination_hashes": {"billing-worker": {"label": "x", "hash": "h1"}}},
                          "billing-worker"),
                         ({"keys": [], "destinations": {"alpha-web": "fixture-label...abc"}}, "alpha-web"),
                         ({"keys": [{"name": "alpha-web-key", "label": "y", "serves": "observatory"}]}, "observatory")):
            self.scan(doc)
            exc = self.refused({"hash": "h1", "labels": {"fixture-label...abc"}, "names": {"alpha-web-key"}},
                               ["openrouter-scan"])
            self.assertEqual(exc.code, key_consumers.IN_USE)
            self.assertTrue(any(who in c for c in exc.consumers), exc.consumers)
            self.assertIn("reading right now", str(exc))

    def test_a_clean_scan_lets_the_revoke_go_ahead(self):
        self.scan({"keys": [{"name": "other", "label": "z", "serves": "observatory"}],
                   "destination_hashes": {"x": {"hash": "h9"}}, "destinations": {"y": "q"}})
        out = key_consumers.revoke_guard("alpha-web-key", {"hash": "h1", "labels": {"lab"}, "names": {"alpha"}},
                                         ["openrouter-scan"])
        self.assertEqual((out["consumers"], out["degraded"]), ([], []))

    def test_a_value_source_finds_by_value_in_memory_and_never_prints_it(self):
        exc = self.refused({}, [ValueSource(held=FAKE_VALUE)], value=FAKE_VALUE)
        self.assertEqual(exc.code, key_consumers.IN_USE)
        self.assertIn("/srv/alpha-web/.env", str(exc))
        out = key_consumers.revoke_guard("alpha-web-key", {}, [ValueSource(held="another")], value=FAKE_VALUE)
        self.assertEqual(out["consumers"], [])

    def test_a_value_the_door_could_not_read_or_a_failing_source_refuses_with_a_code(self):
        exc = self.refused({"unread": "the slot could not be read (OSError)"}, [ValueSource()])
        self.assertEqual((exc.code, exc.unread[0]["code"]), (key_consumers.SOURCE_UNREADABLE, "value-unreadable"))
        exc = self.refused({}, [ValueSource(fail=True)], value=FAKE_VALUE)
        self.assertEqual(exc.code, key_consumers.SOURCE_UNREADABLE)
        self.assertTrue(all(d.get("code") for d in exc.unread), exc.unread)

    def test_no_value_anywhere_is_skipped_not_degraded(self):
        out = key_consumers.revoke_guard("alpha-web-key", {}, [ValueSource()], value=None)
        self.assertEqual(out["consumers"], [])
        self.assertEqual([d["code"] for d in out["skipped"]], ["no-value"])

    def test_every_degraded_entry_of_the_finder_carries_a_code(self):
        found = key_consumers.find(FAKE_VALUE, [ValueSource(fail=True)])
        self.assertTrue(found["degraded"] and all(d.get("code") for d in found["degraded"]), found["degraded"])
        projects = self.base / "projects"
        projects.mkdir()
        env = key_consumers.EnvFileSource(inventory=self.scratch / "env.json", projects=projects)
        found = key_consumers.find(FAKE_VALUE, [env])
        self.assertTrue(found["degraded"] and all(d.get("code") for d in found["degraded"]), found["degraded"])
        self.assertClean(key_consumers.public(found))


class DigitalOceanApps(unittest.TestCase):
    """The DigitalOcean door's own finder, folded into key_consumers with its rows unchanged."""

    APPS = [{"id": "a1", "spec": {"name": "shop", "databases": [{"name": "db", "cluster_name": "shop-db",
                                                                "db_user": "app"}],
                                  "services": [{"name": "web", "envs": [{"key": "DATABASE_URL",
                                                                        "value": "postgresql://u:p@h/db"},
                                                                       {"key": "REF", "value": "${db.URL}"}]}]}},
            {"id": "a2", "spec": {"name": "blog", "databases": [{"name": "db", "cluster_name": "other"}]}}]

    def test_rows_are_the_doors_and_the_guard_names_each_app(self):
        import digitalocean
        with patch.object(digitalocean, "doctl", lambda *a, **k: self.APPS):
            rows = digitalocean.consumers({"name": "shop-db"})
        self.assertEqual(rows, [{"app": "a1", "name": "shop", "binds": [("db", "app")],
                                 "literals": ["web:DATABASE_URL (url)"]}])
        src = key_consumers.DigitalOceanAppsSource(lambda: self.APPS, digitalocean.credential_literal,
                                                   digitalocean.literal_form)
        with self.assertRaises(key_consumers.RevokeRefused) as cm:
            key_consumers.revoke_guard("shop-db/app", {"cluster": "shop-db"}, [src])
        self.assertEqual(cm.exception.code, key_consumers.IN_USE)
        self.assertIn("App Platform app shop", str(cm.exception))
        self.assertNotIn("u:p@", str(cm.exception))

        def broken():
            raise RuntimeError("doctl failed")
        with self.assertRaises(key_consumers.RevokeRefused) as cm:
            key_consumers.revoke_guard("shop-db/app", {"cluster": "shop-db"},
                                       [key_consumers.DigitalOceanAppsSource(broken, lambda k, v: False, str)])
        self.assertEqual(cm.exception.unread[0]["code"], "app-list-unreadable")


class Paths(Scratch):
    """Each revoke path calls the guard before its provider call."""

    def test_the_openrouter_door_refuses_on_a_missing_scan_before_any_provider_call(self):
        import openrouter
        asked = []
        rec = {"account": "personal", "hash": "h1", "label": "fixture-label...abc", "destination": "vault:a/prod/K"}
        with patch.object(openrouter, "_ledger_lock", contextlib.nullcontext), \
                patch.object(openrouter, "resolve_issued", lambda n: ("alpha-web-key", rec)), \
                patch.object(openrouter, "read_admin", lambda *a: asked.append(a) or ("x", FAKE_PROVISIONING)), \
                patch.object(openrouter, "_request", lambda *a, **k: asked.append("request") or {}):
            with self.assertRaises(key_consumers.RevokeRefused) as cm:
                openrouter.revoke_key("alpha-web-key")
            self.assertEqual(cm.exception.code, key_consumers.SOURCE_UNREADABLE)
            self.scan({"keys": [], "destination_hashes": {"billing-worker": {"hash": "h1"}}})
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = openrouter.cmd_revoke("alpha-web-key")
        self.assertEqual(asked, [])
        self.assertEqual(rc, 1)
        self.assertIn("consumer-in-use", err.getvalue())
        self.assertIn("billing-worker", err.getvalue())
        self.assertClean(err.getvalue())

    def keyserver(self):
        import keyserver
        asked = []

        class Door:
            def revoke_key(self, label):
                asked.append(label)
                return {"name": label, "label": None, "revoked": True}
        return keyserver, asked, [patch.object(keyserver, "_door", lambda: Door()),
                                  patch.object(keyserver, "AUDIT", self.base / "logs" / "keyserver.jsonl")]

    def test_the_key_server_refuses_on_a_missing_scan_and_on_a_consumer_typed(self):
        keyserver, asked, ps = self.keyserver()
        with contextlib.ExitStack() as stack:
            for p in ps:
                stack.enter_context(p)
            with self.assertRaises(key_consumers.RevokeRefused) as cm:
                keyserver.act_revoke({"label": "alpha-web-key"})
            self.assertEqual(cm.exception.code, key_consumers.SOURCE_UNREADABLE)
            self.scan({"keys": [{"label": "alpha-web-key", "serves": "alpha-web"}]})
            with self.assertRaises(key_consumers.RevokeRefused) as cm:
                keyserver.act_revoke({"label": "alpha-web-key"})
            self.assertEqual(cm.exception.code, key_consumers.IN_USE)
            self.scan({"keys": [{"label": "alpha-web-key", "serves": None}]})
            self.assertTrue(keyserver.act_revoke({"label": "alpha-web-key"})["ok"])
        self.assertEqual(asked, ["alpha-web-key"])

    def test_the_mcp_key_act_answers_the_guards_code(self):
        keyserver, asked, ps = self.keyserver()
        import key_acts
        with contextlib.ExitStack() as stack:
            for p in ps:
                stack.enter_context(p)
            out = key_acts.key_act("revoke", name="alpha-web-key", decision=words("keys.revoke", "alpha-web-key"))
            self.assertEqual(out["error"]["code"], key_consumers.SOURCE_UNREADABLE)
            self.scan({"keys": [{"label": "alpha-web-key", "serves": "alpha-web"}]})
            out = key_acts.key_act("revoke", name="alpha-web-key", decision=words("keys.revoke", "alpha-web-key"))
            self.assertEqual(out["error"]["code"], key_consumers.IN_USE)
            self.assertIn("alpha-web", json.dumps(out["error"]["consumers"]))
        self.assertEqual(asked, [])
        self.assertClean(out)

    def run_revoke_key(self, listing):
        import revoke_key
        calls = []

        def call(method, url, key):
            calls.append(method)
            if method == "DELETE":
                return 200, {}
            return 200, {"data": listing if calls.count("GET") == 1 else []}
        out, err = io.StringIO(), io.StringIO()
        with patch.object(revoke_key, "call", call), patch.object(revoke_key, "load_or_store", lambda: FAKE_PROVISIONING), \
                patch.object(sys, "argv", ["revoke_key.py", "--tail", "f8d"]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = revoke_key.main()
            except SystemExit as exc:
                rc = exc.code
        self.assertClean(out.getvalue(), err.getvalue(), str(rc))
        return calls, rc, out.getvalue()

    def test_revoke_key_tool_runs_the_guard_before_its_delete(self):
        listing = [{"label": "sk-or-v1-abc...f8d", "name": "old-tool", "hash": "h7", "limit": 1, "usage": 0}]
        calls, rc, _out = self.run_revoke_key(listing)
        self.assertNotIn("DELETE", calls)
        self.assertIn("consumer-source-unreadable", str(rc))
        self.scan({"keys": [], "destination_hashes": {"alpha-web": {"hash": "h7"}}})
        calls, rc, _out = self.run_revoke_key(listing)
        self.assertNotIn("DELETE", calls)
        self.assertIn("consumer-in-use", str(rc))
        self.scan({"keys": []})
        calls, rc, out = self.run_revoke_key(listing)
        self.assertIn("DELETE", calls)
        self.assertEqual(rc, 0)
        self.assertIn("verified", out)

    def test_the_digitalocean_token_revoke_runs_the_guard_before_revoking(self):
        import digitalocean
        proved = []
        rec = {"label": "alpha-read", "vault": "alpha/prod/DO_TOKEN", "preset": "observatory-read",
               "team_label": "example-team"}
        with patch.object(digitalocean, "_record", lambda label: (self.base / "rec.json", rec)), \
                patch.object(digitalocean, "_current", lambda r: FAKE_VALUE), \
                patch.object(digitalocean, "token_ok", lambda v: True), \
                patch.object(digitalocean, "revoke_and_prove", lambda v: proved.append(1) or True), \
                patch.object(digitalocean, "consumer_sources", lambda r: [ValueSource(held=FAKE_VALUE)]):
            with self.assertRaises(digitalocean.DoorError) as cm:
                digitalocean._revoke_locked("alpha-read", False)
        self.assertEqual(proved, [])
        self.assertIn("consumer-in-use", str(cm.exception))
        self.assertIn("/srv/alpha-web/.env", str(cm.exception))
        self.assertClean(str(cm.exception))


if __name__ == "__main__":
    unittest.main()
