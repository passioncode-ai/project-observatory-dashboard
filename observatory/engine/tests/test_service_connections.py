#!/usr/bin/env python3
"""What Observatory uses, declared in its well-known document (`fabric-connections/0.1`, OBS-326).

Every case builds a temporary workspace and scratch folder and holds the declaration to the contract's own
schema, vendored at the pin (`fabric/interop-schemas/service-connections-extension.schema.json`). What
each class holds:

- Shape: the declaration is valid against the contract (declared, never reported), ids once.
- Configured: a provider whose integration is off is never declared, a leftover receipt included; one
  that is on but never ran reads `unknown`, never `ok`.
- State: a collector's own `degraded` rows make its connection `degraded`, an unreadable receipt too;
  a clean receipt is `ok`; `lastAt` is the collector's own time, never invented.
- Fabric's hub, the release feed and the usage analytics: each from the engine's own record.
- Nothing private: a reason never echoes a collector's text (a path, a name, a key).
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import interop  # noqa: E402
import service_connections as sc  # noqa: E402

KINDS = ("service", "model", "connector", "store", "channel")
SCHEMA = interop.contract_validator("service-connections-extension.schema.json")

AT = "2026-11-05T10:00:00Z"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve() / "workspace"
        (self.base / "store").mkdir(parents=True)
        (self.base / "workspace.json").write_text("{}", encoding="utf-8")
        # The collectors' output folder: passed in, as the server passes paths.SCRATCH.
        self.scratch = self.base / "scratch"
        self.scratch.mkdir()
        self.settings = {"integrations": {}, "features": {}}
        self.env = {}

    def receipt(self, name: str, doc) -> Path:
        path = self.scratch / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc) if not isinstance(doc, str) else doc, encoding="utf-8")
        return path

    def on(self, *names):
        for name in names:
            self.settings["integrations"][name] = True

    def declared(self):
        return sc.declared(self.base, self.scratch, self.settings, environ=self.env)

    def by_id(self):
        return {c["id"]: c for c in self.declared()}

    def view(self):
        """The declaration as a host reads it: declared when the key is there and the value is valid."""
        ext = sc.extension(self.base, self.scratch, self.settings, environ=self.env)
        value = ext.get(sc.KEY)
        errors = [] if value is None else [e.message for e in SCHEMA.iter_errors(value)] + sc.check(value)
        return {"declared": value is not None and not errors, "reported": bool(errors), "errors": errors,
                "connections": (value or {}).get("connections", [])}


class Shape(Base):
    def test_a_fresh_install_declares_what_it_always_uses_and_nothing_else(self):
        view = self.view()
        self.assertTrue(view["declared"], view["errors"])
        self.assertFalse(view["reported"])
        ids = {c["id"] for c in view["connections"]}
        self.assertEqual(ids, {"fabric-hub", "release-feed"}, "no provider that is not configured")
        for c in view["connections"]:
            self.assertEqual(c["state"], "unknown", "never used on this install: unknown, never ok")
            self.assertNotIn("lastAt", c)

    def test_every_connection_has_the_contract_members_only(self):
        self.on(*sc.INTEGRATIONS)
        for c in self.declared():
            self.assertLessEqual(set(c), {"id", "kind", "target", "capability", "state", "label", "purpose",
                                          "reason", "lastAt"})
            self.assertIn(c["kind"], KINDS)
        self.assertTrue(self.view()["declared"])

    def test_the_hub_is_a_service_by_its_product_id_with_the_capability_it_calls(self):
        hub = self.by_id()["fabric-hub"]
        self.assertEqual((hub["kind"], hub["target"], hub["capability"]), ("service", "fabric", "project.resolve"))

    def test_providers_are_connectors(self):
        self.on("github", "cloudflare", "openrouter")
        found = self.by_id()
        for ident in ("github", "cloudflare", "openrouter"):
            self.assertEqual(found[ident]["kind"], "connector")


class Configured(Base):
    def test_an_integration_that_is_off_is_never_declared_even_with_a_leftover_receipt(self):
        self.receipt("heroku.json", {"scanned_at": AT, "degraded": []})
        self.assertNotIn("heroku", self.by_id())
        self.settings["integrations"]["heroku"] = False
        self.assertNotIn("heroku", self.by_id())

    def test_on_but_never_run_is_unknown(self):
        self.on("bitbucket")
        c = self.by_id()["bitbucket"]
        self.assertEqual(c["state"], "unknown")
        self.assertNotIn("lastAt", c)


class State(Base):
    def test_a_clean_receipt_is_ok_with_the_collectors_own_time(self):
        self.on("digitalocean")
        self.receipt("digitalocean.json", {"scanned_at": AT, "degraded": []})
        c = self.by_id()["digitalocean"]
        self.assertEqual((c["state"], c["lastAt"]), ("ok", AT))

    def test_the_collectors_degraded_rows_make_the_connection_degraded(self):
        self.on("google")
        self.receipt("google.json", {"scanned_at": AT, "degraded": [
            {"source": "/srv/someone/private-project", "reason": "token ghp_" + "A" * 36 + " refused"}]})
        c = self.by_id()["google"]
        self.assertEqual(c["state"], "degraded")
        self.assertNotIn("someone", json.dumps(c))
        self.assertNotIn("ghp_", json.dumps(c), "a reason never echoes the collector's text")
        self.assertTrue(self.view()["declared"], "and the declaration stays valid")

    def test_an_unreadable_receipt_is_degraded(self):
        self.on("heroku")
        self.receipt("heroku.json", "{not json")
        self.assertEqual(self.by_id()["heroku"]["state"], "degraded")

    def test_several_receipts_take_the_worst_state_and_the_latest_time(self):
        self.on("cloudflare")
        self.receipt("cloudflare_zones.json", {"scanned_at": "2026-11-05T09:00:00Z", "degraded": []})
        self.receipt("cloudflare_account.json", {"scanned_at": AT, "degraded": [{"source": "a", "reason": "b"}]})
        c = self.by_id()["cloudflare"]
        self.assertEqual((c["state"], c["lastAt"]), ("degraded", AT))

    def test_a_receipt_without_a_time_takes_the_time_the_engine_wrote_it(self):
        self.on("github")
        path = self.receipt("gh/_degraded.json", [])
        os.utime(path, (1_793_000_000, 1_793_000_000))
        c = self.by_id()["github"]
        self.assertEqual(c["state"], "ok")
        self.assertEqual(c["lastAt"], datetime.datetime.fromtimestamp(1_793_000_000, datetime.timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(sc.check({"connections": [c]}), [])


class Hub(Base):
    def test_a_hub_that_answers_is_ok(self):
        self.receipt("fabric-attribution.json", {"checked_at": AT, "hub": {"origin": "x"}, "degraded": []})
        hub = self.by_id()["fabric-hub"]
        self.assertEqual((hub["state"], hub["lastAt"]), ("ok", AT))

    def test_a_dead_hub_is_down_no_hub_is_unknown_and_a_refusal_degraded(self):
        # No hub on this machine (`hub-absent`) means Fabric is not installed, not an outage.
        for reason, state in (("hub-absent", "unknown"), ("hub-dead", "down"), ("access-refused", "degraded")):
            with self.subTest(reason=reason):
                self.receipt("fabric-attribution.json", {"checked_at": AT, "degraded": [
                    {"source": "fabric", "reason": reason}]})
                self.assertEqual(self.by_id()["fabric-hub"]["state"], state)


class ReleaseFeed(Base):
    def check(self, record):
        (self.base / "store" / "update-check.json").write_text(json.dumps(record), encoding="utf-8")

    def test_the_last_check_decides(self):
        for result, state in (("up-to-date", "ok"), ("update-available", "ok"), ("undetermined", "degraded")):
            with self.subTest(result=result):
                self.check({"check": {"at": AT, "result": result}})
                feed = self.by_id()["release-feed"]
                self.assertEqual((feed["state"], feed["lastAt"]), (state, AT))
                self.assertEqual(feed["kind"], "connector")


class Analytics(Base):
    def test_declared_only_while_collecting_and_its_state_from_the_last_send(self):
        self.assertNotIn("usage-analytics", self.by_id(), "no App-Key: nothing is collected or sent")
        self.env = {"OBSERVATORY_ANALYTICS_APP_KEY": "A-SH-0000000000"}
        self.assertEqual(self.by_id()["usage-analytics"]["state"], "unknown", "nothing sent yet")
        import usage_analytics as UA
        UA.note_send(self.base, ok=True, at=datetime.datetime(2026, 11, 5, 10, tzinfo=datetime.timezone.utc))
        c = self.by_id()["usage-analytics"]
        self.assertEqual((c["state"], c["lastAt"]), ("ok", AT))
        UA.note_send(self.base, ok=False, at=datetime.datetime(2026, 11, 5, 11, tzinfo=datetime.timezone.utc))
        self.assertEqual(self.by_id()["usage-analytics"]["state"], "degraded")
        self.env["DO_NOT_TRACK"] = "1"
        self.assertNotIn("usage-analytics", self.by_id(), "off: not used")


class Check(Base):
    def test_the_contracts_rules_refuse_what_a_host_would_hide(self):
        good = {"id": "x", "kind": "connector", "target": "x", "state": "ok"}
        self.assertEqual(sc.check({"connections": [good]}), [])
        for bad in ({**good, "target": "a b"}, {**good, "kind": "robot"}, {**good, "capability": "project.resolve"},
                    {**good, "kind": "service", "target": "Not_A_Service"}, {**good, "extra": "x"},
                    {**good, "purpose": "see https://example.invalid/page"}, {**good, "lastAt": "2026-11-05"}):
            with self.subTest(bad=bad):
                self.assertTrue(sc.check({"connections": [bad]}))
        self.assertTrue(sc.check({"connections": [good, good]}), "an id named twice")


class Fallback(Base):
    def test_a_declaration_the_kit_refuses_is_named_never_published(self):
        out = sc.extension(self.base, self.scratch, self.settings, environ=self.env,
                           declared=lambda *a, **k: [{"id": "x", "kind": "connector", "target": "a b", "state": "ok"}])
        self.assertEqual(out, {})
        self.assertTrue(sc.LAST_PROBLEM)


if __name__ == "__main__":
    unittest.main()
