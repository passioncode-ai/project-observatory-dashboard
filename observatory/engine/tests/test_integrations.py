#!/usr/bin/env python3
"""Integration health (OBS-557, CP-12, CP-13): key validity once a day, drift of a collector's answer, untracked
providers.

Every provider is a fake that records what it was asked, so nothing reaches a network; every value is a made-up
string, and the stored files are checked to hold none of them.
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "dashboard"))
import tmp as tmpdir                                                # noqa: E402
import integration_health as ih                                     # noqa: E402
import integration_findings as ifn                                  # noqa: E402

VALUE_A = "fakevalue-aaaa-1111-not-a-real-key"
VALUE_B = "fakevalue-bbbb-2222-not-a-real-key"


class Refused(RuntimeError):
    pass


def fake(name, answers: dict[str, object], asked: list):
    """A provider holding the keys in `answers` (slot -> value to read, outcome of its ping)."""
    values = {slot: f"{VALUE_A}-{i}" for i, slot in enumerate(answers)}

    def holders():
        return [(slot, (lambda s=slot: values[s])) for slot in answers]

    def ping(value):
        slot = next(s for s, v in values.items() if v == value)
        asked.append(slot)
        outcome = answers[slot]
        if isinstance(outcome, Exception):
            raise outcome
    return ih.Provider(name, holders, ping)


class KeyHealth(unittest.TestCase):
    def test_a_401_and_a_403_are_invalid_and_a_timeout_is_not(self):
        asked: list = []
        p = fake("cloudflare", {"cloudflare/admin/one": None,
                                "cloudflare/admin/two": Refused("cloudflare answered HTTP 401; withheld"),
                                "cloudflare/issued/three": Refused("cloudflare answered HTTP 403; withheld"),
                                "cloudflare/issued/four": Refused("cloudflare unreachable: TimeoutError"),
                                "cloudflare/issued/five": Refused("cloudflare answered HTTP 500; withheld")}, asked)
        keys, degraded = ih.check_keys([p], None, "2026-11-16")
        state = {k["slot"].split("/")[-1]: k["state"] for k in keys}
        self.assertEqual(state, {"one": "valid", "two": "invalid", "three": "invalid", "four": "unreachable",
                                 "five": "unreachable"})
        self.assertEqual(degraded, [])
        self.assertEqual({k["slot"].split("/")[-1]: k["status"] for k in keys if k["state"] == "invalid"},
                         {"two": 401, "three": 403})
        self.assertNotIn("VALUE", json.dumps(keys))
        self.assertNotIn("fakevalue", json.dumps(keys))

    def test_each_key_is_asked_once_a_day(self):
        asked: list = []
        p = fake("openrouter", {"openrouter/admin/a": None, "openrouter/admin/b": Refused("HTTP 401")}, asked)
        first, _ = ih.check_keys([p], None, "2026-11-16")
        self.assertEqual(len(asked), 2)
        again, _ = ih.check_keys([p], {"keys": first}, "2026-11-16")
        self.assertEqual(len(asked), 2, "same day: nothing asked again")
        self.assertEqual(again, first)
        ih.check_keys([p], {"keys": first}, "2026-11-17")
        self.assertEqual(len(asked), 4, "next day: every key is asked")
        ih.check_keys([p], {"keys": first}, "2026-11-17", force=True)
        self.assertEqual(len(asked), 6, "force asks again")

    def test_an_invalid_key_keeps_the_day_it_first_failed(self):
        p = fake("openrouter", {"openrouter/admin/b": Refused("HTTP 401")}, [])
        day1, _ = ih.check_keys([p], None, "2026-11-16")
        day2, _ = ih.check_keys([p], {"keys": day1}, "2026-11-17")
        self.assertEqual(day2[0]["since"], "2026-11-16")

    def test_a_door_that_cannot_list_is_named_and_the_others_still_run(self):
        def broken():
            raise OSError("no store")
        asked: list = []
        keys, degraded = ih.check_keys([ih.Provider("heroku", broken, lambda v: None),
                                        fake("cloudflare", {"cloudflare/admin/x": None}, asked)], None, "2026-11-16")
        self.assertEqual([k["slot"] for k in keys], ["cloudflare/admin/x"])
        self.assertEqual(degraded[0]["source"], "integration:heroku")
        self.assertNotIn("no store", json.dumps(degraded))

    def test_an_unreadable_value_is_said_and_never_pinged(self):
        asked: list = []

        def holders():
            return [("openrouter/admin/gone", lambda: (_ for _ in ()).throw(OSError("missing")))]
        keys, _ = ih.check_keys([ih.Provider("openrouter", holders, lambda v: asked.append(v))], None, "2026-11-16")
        self.assertEqual((keys[0]["state"], asked), ("unreadable", []))


class Drift(unittest.TestCase):
    def raw(self, **files):
        d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-drift-"))
        for name, doc in files.items():
            (d / name).write_text(json.dumps(doc), encoding="utf-8")
        return d

    DECL = {"cf": {"file": "cf.json", "rows": "zones", "fields": ("name", "status", "plan")}}

    def test_a_renamed_field_is_drift_with_the_day_it_was_last_seen(self):
        healthy = self.raw(**{"cf.json": {"zones": [{"name": "a.example", "status": "active", "plan": "free"}]}})
        drift, seen = ih.check_drift(healthy, self.DECL, None, "2026-11-15")
        self.assertEqual(drift, [])
        renamed = self.raw(**{"cf.json": {"zones": [{"name": "a.example", "status": "active", "plan_name": "free"},
                                                    {"name": "b.example", "status": "active", "plan": None}]}})
        drift, seen = ih.check_drift(renamed, self.DECL, seen, "2026-11-16")
        self.assertEqual(drift, [{"collector": "cf", "field": "plan", "last_seen": "2026-11-15", "rows": 2}])
        self.assertEqual(seen["cf"]["name"], "2026-11-16")

    def test_a_field_one_row_still_carries_is_not_drift(self):
        d = self.raw(**{"cf.json": {"zones": [{"name": "a", "status": "x", "plan": "free"}, {"name": "b", "status": "x"}]}})
        self.assertEqual(ih.check_drift(d, self.DECL, None, "2026-11-16")[0], [])

    def test_no_file_no_rows_or_unreadable_says_nothing(self):
        self.assertEqual(ih.check_drift(self.raw(), self.DECL, None, "2026-11-16")[0], [])
        self.assertEqual(ih.check_drift(self.raw(**{"cf.json": {"zones": []}}), self.DECL, None, "2026-11-16")[0], [])
        d = self.raw()
        (d / "cf.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(ih.check_drift(d, self.DECL, None, "2026-11-16")[0], [])

    def test_never_seen_is_drift_with_no_last_seen(self):
        d = self.raw(**{"cf.json": {"zones": [{"name": "a", "status": "x"}]}})
        drift, _ = ih.check_drift(d, self.DECL, None, "2026-11-16")
        self.assertEqual(drift, [{"collector": "cf", "field": "plan", "last_seen": None, "rows": 1}])

    def test_every_declared_collector_names_a_file_and_rows(self):
        for name, spec in ih.COLLECTOR_FIELDS.items():
            self.assertTrue(spec["file"].endswith(".json") and spec["rows"] and spec["fields"], name)


class Untracked(unittest.TestCase):
    DEPS = {"projects": [
        {"project": "project:alpha-web", "packages": [
            {"name": "stripe", "ecosystem": "npm"}, {"name": "@stripe/stripe-js", "ecosystem": "npm"},
            {"name": "@sentry/node", "ecosystem": "npm"}, {"name": "boto3", "ecosystem": "PyPI"},
            {"name": "lodash", "ecosystem": "npm"}, {"name": "@aws-sdk/client-s3", "ecosystem": "npm"}]},
        {"project": "project:beta-api", "packages": [{"name": "Braintree", "ecosystem": "PyPI"}]}]}
    ENV = {"files": [{"project": "project:alpha-web", "variables": [
        {"name": "STRIPE_API_KEY", "class": "secret"}, {"name": "SENTRY_ENVIRONMENT", "class": "config"},
        {"name": "DATABASE_URL", "class": "secret"}]},
        {"project": "project:gamma", "variables": [{"name": "OPENROUTER_API_KEY", "class": "secret"},
                                                   {"name": "PADDLE_API_KEY", "class": "secret"}]}]}

    def test_the_mapping_table_applied(self):
        rows = {(r["project"], r["provider"]): r for r in ih.untracked(self.DEPS, self.ENV)}
        self.assertEqual(sorted(rows), [("project:alpha-web", "aws"), ("project:alpha-web", "stripe"),
                                        ("project:beta-api", "braintree"), ("project:gamma", "paddle")])
        stripe = rows[("project:alpha-web", "stripe")]
        self.assertEqual(stripe["category"], "payments")
        self.assertEqual([(e["kind"], e["name"]) for e in stripe["evidence"]],
                         [("env", "STRIPE_API_KEY"), ("package", "@stripe/stripe-js"), ("package", "stripe")])

    def test_a_provider_with_a_door_is_not_untracked(self):
        providers = {r["provider"] for r in ih.untracked(self.DEPS, self.ENV)}
        self.assertFalse(providers & {"sentry", "openrouter"})
        self.assertTrue(ih.TRACKED >= {"sentry", "openrouter", "cloudflare", "digitalocean", "heroku"})

    def test_evidence_is_names_never_values(self):
        env = {"files": [{"project": "project:x", "variables": [{"name": "STRIPE_KEY", "class": "secret",
                                                                  "value": VALUE_A, "fingerprint": "abc"}]}]}
        doc = json.dumps(ih.untracked(None, env))
        self.assertIn("STRIPE_KEY", doc)
        self.assertNotIn(VALUE_A, doc)
        self.assertNotIn("abc", doc)

    def test_evidence_is_bounded_and_counted(self):
        deps = {"projects": [{"project": "project:p", "packages": [{"name": f"@aws-sdk/c{i}"} for i in range(9)]}]}
        row = ih.untracked(deps, None)[0]
        self.assertEqual((len(row["evidence"]), row["evidence_total"]), (ih.EVIDENCE_LIMIT, 9))

    def test_missing_documents_give_nothing(self):
        self.assertEqual(ih.untracked(None, None), [])


class Findings(unittest.TestCase):
    def doc(self):
        asked: list = []
        keys, _ = ih.check_keys([fake("cloudflare", {"cloudflare/admin/old": Refused("HTTP 403"),
                                                     "cloudflare/admin/ok": None}, asked)], None, "2026-11-16")
        return {"checked_on": "2026-11-16", "keys": keys,
                "drift": [{"collector": "digitalocean", "field": "droplets", "last_seen": "2026-11-10", "rows": 2}],
                "untracked": ih.untracked(Untracked.DEPS, Untracked.ENV)}

    def test_one_finding_per_invalid_key_drift_and_untracked_provider(self):
        found = ifn.findings(self.doc())
        by = {}
        for f in found:
            by.setdefault(f["type"], []).append(f)
        self.assertEqual({t: len(v) for t, v in by.items()},
                         {"integration.key_invalid": 1, "integration.drift": 1, "integration.untracked": 4})
        key = by["integration.key_invalid"][0]
        self.assertEqual((key["subject"], key["severity"]), ("integration:key/cloudflare/admin/old", "warning"))
        drift = by["integration.drift"][0]
        self.assertIn("2026-11-10", drift["detail"])
        self.assertEqual({f["severity"] for f in by["integration.untracked"]}, {"info"})
        self.assertTrue(all(f["project"].startswith("project:") for f in by["integration.untracked"]))
        self.assertNotIn("fakevalue", json.dumps(found))

    def test_silent_before_the_step_has_run(self):
        self.assertEqual(ifn.findings(None), [])
        self.assertEqual(ifn.from_file(pathlib.Path("/nonexistent/integration-health.json")), [])

    def test_every_type_is_labelled(self):
        import finding_types
        for t in ifn.TYPES:
            self.assertIn(t, finding_types.LABELS)


class Run(unittest.TestCase):
    def test_run_stores_no_value_and_offline_keeps_the_last_answer(self):
        raw = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-ih-raw-"))
        reg = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-ih-reg-"))
        (reg / "dependencies.json").write_text(json.dumps(Untracked.DEPS), encoding="utf-8")
        asked: list = []
        p = fake("cloudflare", {"cloudflare/admin/old": Refused("HTTP 401")}, asked)
        doc, seen = ih.run([p], raw_dir=raw, registry_dir=reg, previous=None, seen=None, today="2026-11-16")
        self.assertNotIn("fakevalue", json.dumps(doc))
        self.assertEqual(len(doc["untracked"]), 3)
        off, _ = ih.run([p], raw_dir=raw, registry_dir=reg, previous=doc, seen=seen, today="2026-11-17", offline=True)
        self.assertEqual(len(asked), 1)
        self.assertEqual(off["keys"], doc["keys"])
        self.assertTrue(any(d["source"] == "integrations" for d in off["degraded"]))

    def test_the_agent_view_says_why_when_the_step_has_not_run(self):
        v = ih.agent_view(doc={})
        self.assertEqual((v["keys"], v["checkedOn"]), ([], None))
        self.assertTrue(v["degraded"])
        v = ih.agent_view(doc={"checked_on": "2026-11-16", "keys": [{"slot": "b", "state": "valid"},
                                                                    {"slot": "a", "state": "invalid"}]})
        self.assertEqual([k["slot"] for k in v["keys"]], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
