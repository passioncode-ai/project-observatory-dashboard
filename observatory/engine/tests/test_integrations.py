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



# ---- OBS-605: the tracked providers come from the doors, scope is compared, every finding has a code -----------

import door_registry                                                # noqa: E402


def write_module(folder: pathlib.Path, name: str, text: str) -> None:
    (folder / f"{name}.py").write_text(text, encoding="utf-8")


class Registry(unittest.TestCase):
    def test_every_door_and_collector_declares_itself(self):
        found, degraded = door_registry.declarations()
        self.assertEqual(degraded, [])
        doors = {d["provider"]: d["module"] for d in found if d["kind"] == "door"}
        self.assertEqual(doors, {"cloudflare": "cloudflare", "digitalocean": "digitalocean", "heroku": "heroku",
                                 "openrouter": "openrouter", "prowl": "prowl", "sentry": "sentry"})
        covered = {d["provider"] for d in found if d["kind"] == "collector"}
        self.assertTrue(covered >= {"github", "google", "cloudflare", "digitalocean", "heroku", "openrouter"})

    def test_tracked_is_derived_never_kept_by_hand(self):
        self.assertEqual(ih.TRACKED, door_registry.providers())
        self.assertNotIn("TRACKED = frozenset({", (ROOT / "tools" / "integration_health.py").read_text())

    def test_a_new_door_is_tracked_once_it_declares_itself(self):
        tools = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-doors-"))
        cols = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-cols-"))
        write_module(tools, "acme", 'DOOR_PROVIDER = "acme"\n')
        write_module(tools, "helper", "X = 1\n")
        write_module(cols, "scan_acme_two", 'COVERS_PROVIDERS = ("acme-two",)\n')
        self.assertEqual(door_registry.providers(tools, cols), frozenset({"acme", "acme-two"}))
        self.assertEqual(door_registry.doors(tools, cols), {"acme": "acme"})
        deps = {"projects": [{"project": "project:p", "packages": [{"name": "stripe"}]}]}
        self.assertEqual(ih.untracked(deps, None, tracked=frozenset({"stripe"})), [])

    def test_a_declaration_in_no_known_shape_is_named_with_a_code(self):
        tools = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-doors-"))
        write_module(tools, "odd", "DOOR_PROVIDER = make_door()\n")
        write_module(tools, "bad", 'DOOR_PROVIDER = "Not A Name"\n')
        write_module(tools, "broken", "DOOR_PROVIDER = (\n")
        found, degraded = door_registry.declarations(tools, tools / "none")
        self.assertEqual(found, [])
        self.assertEqual(sorted(d["code"] for d in degraded),
                         ["declaration-unshaped", "declaration-unshaped", "module-unreadable"])

    def test_the_run_names_the_doors_whose_keys_are_not_asked(self):
        raw = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-ih-raw-"))
        doc, _ = ih.run([ih.Provider("cloudflare", lambda: [], lambda v: None)], raw_dir=raw, registry_dir=raw,
                        previous=None, seen=None, today="2026-11-18")
        self.assertEqual(doc["tracked_providers"], sorted(door_registry.providers()))
        self.assertIn("prowl", doc["keys_not_asked"])
        self.assertNotIn("cloudflare", doc["keys_not_asked"])


class Builders(unittest.TestCase):
    def test_every_builder_is_for_a_declared_provider_and_opens_without_a_network(self):
        self.assertTrue(set(ih.BUILDERS) <= door_registry.providers())
        degraded: list = []
        built = ih.default_providers(degraded)
        self.assertEqual({p.name for p in built} | {d["source"].split(":", 1)[1] for d in degraded}, set(ih.BUILDERS))
        self.assertTrue(all(d["code"] == "door-unavailable" for d in degraded))


class UntrackedCodes(unittest.TestCase):
    def test_a_key_with_no_door_and_a_package_with_no_door_have_their_codes(self):
        rows = {r["provider"]: r for r in ih.untracked(Untracked.DEPS, Untracked.ENV)}
        self.assertEqual(rows["stripe"]["code"], "untracked-key")
        self.assertEqual(rows["aws"]["code"], "untracked-package")
        self.assertEqual(rows["paddle"]["code"], "untracked-key")


class Scope(unittest.TestCase):
    def test_the_judgement(self):
        j = ih.judge_scope
        self.assertEqual(j({"granted": ["a", "b"], "needed": ["a", "b"]})["code"], "scope-ok")
        missing = j({"granted": ["a"], "needed": ["a", "b"]})
        self.assertEqual((missing["state"], missing["code"], missing["missing"]), ("missing", "scope-missing", ["b"]))
        excess = j({"granted": ["a", "b", "c", "gist"], "needed": ["a", "b"], "allowed": ["gist"]})
        self.assertEqual((excess["state"], excess["code"], excess["excess"]), ("excess", "scope-excess", ["c"]))
        both = j({"granted": ["a", "z"], "needed": ["a", "b"]})
        self.assertEqual((both["missing"], both["excess"]), (["b"], ["z"]))
        self.assertEqual(j({"granted": None, "needed": ["a"]})["code"], "scope-unknown")
        self.assertEqual(j({"granted": ["a"], "needed": None})["code"], "scope-unknown")

    def provider(self, scope):
        return ih.Provider("heroku", lambda: [("heroku/issued/a/prod/K", lambda: VALUE_A, {"preset": "read"})],
                           lambda v, ctx: {"seen": True}, scope)

    def test_a_scope_is_recorded_on_the_key_and_its_findings_carry_codes(self):
        got = []

        def scope(slot, value, answer, ctx):
            got.append((slot, answer, ctx))
            return {"granted": ["global"], "needed": ["read"]}
        keys, degraded = ih.check_keys([self.provider(scope)], None, "2026-11-18")
        self.assertEqual(degraded, [])
        self.assertEqual(got, [("heroku/issued/a/prod/K", {"seen": True}, {"preset": "read"})])
        rec = keys[0]
        self.assertEqual((rec["state"], rec["scope"]["missing"], rec["scope"]["excess"]),
                         ("valid", ["read"], ["global"]))
        found = ifn.findings({"checked_on": "2026-11-18", "keys": keys})
        by = {f["type"]: f for f in found}
        self.assertEqual(set(by), {"integration.scope_missing", "integration.scope_excess"})
        self.assertEqual(by["integration.scope_missing"]["code"], "scope-missing")
        self.assertEqual(by["integration.scope_excess"]["code"], "scope-excess")
        self.assertEqual(by["integration.scope_missing"]["subject"], "integration:scope/heroku/issued/a/prod/K")
        blob = json.dumps([keys, found])
        self.assertNotIn(VALUE_A, blob)
        self.assertNotIn("fakevalue", blob)

    def test_a_scope_that_cannot_be_read_is_named_never_a_value(self):
        def scope(slot, value, answer, ctx):
            raise RuntimeError(f"failed with {value}")
        keys, degraded = ih.check_keys([self.provider(scope)], None, "2026-11-18")
        self.assertEqual(keys[0]["scope"], {"state": "unread", "code": "scope-unreadable", "reason": "RuntimeError"})
        self.assertEqual(degraded[0]["code"], "scope-unreadable")
        self.assertNotIn("fakevalue", json.dumps([keys, degraded]))

    def test_a_provider_without_a_scope_records_none(self):
        keys, _ = ih.check_keys([fake("openrouter", {"openrouter/admin/a": None}, [])], None, "2026-11-18")
        self.assertNotIn("scope", keys[0])

    def test_every_integration_finding_has_a_code_and_a_label(self):
        doc = Findings().doc()
        doc["keys"].append({"provider": "x", "slot": "x/y", "state": "valid",
                            "scope": ih.judge_scope({"granted": ["a", "c"], "needed": ["a", "b"]})})
        found = ifn.findings(doc)
        self.assertTrue(found)
        for f in found:
            self.assertTrue(f.get("code"), f["type"])
        import finding_types
        for t in ifn.TYPES:
            self.assertIn(t, finding_types.LABELS)


def run_out(stdout="", stderr="", rc=0, asked=None):
    def run(argv, **kw):
        if asked is not None:
            asked.append((list(argv), kw))
        import subprocess
        return subprocess.CompletedProcess(argv, rc, stdout, stderr)
    return run


class GitHub(unittest.TestCase):
    HEAD = "HTTP/2.0 200 OK\nContent-Type: application/json\nX-Oauth-Scopes: {scopes}\n\n{{\"login\": \"example\"}}\n"

    def test_the_session_scopes_header_is_compared(self):
        asked: list = []
        p = ih.github_provider(run=run_out(self.HEAD.format(scopes="repo, gist, admin:org"), asked=asked),
                               which=lambda n: "/usr/bin/gh")
        keys, degraded = ih.check_keys([p], None, "2026-11-18")
        self.assertEqual(degraded, [])
        rec = keys[0]
        self.assertEqual((rec["slot"], rec["state"]), ("github/gh-session", "valid"))
        self.assertEqual((rec["scope"]["missing"], rec["scope"]["excess"]), (["read:org"], ["admin:org"]))
        self.assertEqual(asked[0][0], ["gh", "api", "-i", "user"])

    def test_a_refused_session_is_invalid_and_a_missing_gh_holds_no_key(self):
        p = ih.github_provider(run=run_out("HTTP/2.0 401 Unauthorized\n\n{}", "gh: Bad credentials (HTTP 401)", 1),
                               which=lambda n: "/usr/bin/gh")
        keys, _ = ih.check_keys([p], None, "2026-11-18")
        self.assertEqual((keys[0]["state"], keys[0]["status"]), ("invalid", 401))
        none = ih.github_provider(run=run_out(), which=lambda n: None)
        self.assertEqual(ih.check_keys([none], None, "2026-11-18")[0], [])

    def test_a_fine_grained_token_answers_no_scope_header(self):
        p = ih.github_provider(run=run_out("HTTP/2.0 200 OK\n\n{}"), which=lambda n: "/usr/bin/gh")
        keys, _ = ih.check_keys([p], None, "2026-11-18")
        self.assertEqual(keys[0]["scope"]["code"], "scope-unknown")


class FakeCloudflare:
    PRESETS = {"analytics": {"name": "observatory-analytics-read (managed)",
                             "groups": ("Zone Read", "Analytics Read", "DNS Read")}}

    def __init__(self, base: pathlib.Path, granted):
        self.base = base
        self.granted = granted
        self.calls: list = []
        self.token = base / "example"
        self.token.write_text(VALUE_A + "\n", encoding="utf-8")

    def admins(self):
        return [("acct", self.base / "admin")]

    def issued(self):
        return [self.token]

    def label_of(self, p):
        return p.name

    def read_meta(self, p):
        return {"account_id": "acc-1", "preset": "analytics", "stash": "acct"}

    def read_admin(self, label):
        return label, VALUE_B

    def _request(self, path, token, payload=None, method=None):
        self.calls.append((path, token == VALUE_A, token == VALUE_B))
        if path.endswith("/tokens/verify"):
            return {"result": {"id": "tok-1", "status": "active"}}
        if path.endswith("/tokens/tok-1"):
            return {"result": {"policies": [{"permission_groups": [{"name": g} for g in self.granted]}]}}
        return {"result": []}


class CloudflareScope(unittest.TestCase):
    def test_an_issued_token_is_verified_and_its_groups_compared(self):
        base = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-cf-"))
        (base / "admin").write_text(VALUE_B + "\n", encoding="utf-8")
        cf = FakeCloudflare(base, ["Zone Read", "Analytics Read", "DNS Write"])
        p = ih.cloudflare_provider(cf, read=lambda path: path.read_text().strip())
        keys, degraded = ih.check_keys([p], None, "2026-11-18")
        self.assertEqual(degraded, [])
        by = {k["slot"]: k for k in keys}
        issued = by["cloudflare/issued/example"]
        self.assertEqual((issued["scope"]["missing"], issued["scope"]["excess"]), (["DNS Read"], ["DNS Write"]))
        self.assertNotIn("scope", by["cloudflare/admin/acct"])
        self.assertIn(("/accounts/acc-1/tokens/verify", True, False), cf.calls)
        self.assertIn(("/accounts/acc-1/tokens/tok-1", False, True), cf.calls)
        self.assertNotIn("fakevalue", json.dumps(keys))


class FakeHerokuDoor:
    PRESETS = {"read": {"scopes": ("read",)}, "identity": {"scopes": ("identity",)}}

    def __init__(self, granted):
        self.granted = granted
        self.calls: list = []

    def admins(self):
        return [("main", pathlib.Path("/nonexistent/heroku-admin/main"))]

    def ledger(self):
        return {"issued": {"alpha/prod/K": {"preset": "read", "authorization_id": "auth-1", "admin": "main"}}}

    def read_admin(self, label):
        return label, VALUE_B

    def q(self, text):
        return text

    def _request(self, method, path, token, payload=None):
        self.calls.append((method, path, token == VALUE_A, token == VALUE_B))
        if path == "/oauth/authorizations/auth-1":
            return {"id": "auth-1", "scope": self.granted}
        return {"id": "account"}


class HerokuScope(unittest.TestCase):
    def test_issued_authorizations_are_asked_and_their_scope_compared(self):
        hk = FakeHerokuDoor(["global"])
        p = ih.heroku_provider(hk, slot_value=lambda *slot: VALUE_A, read=lambda path: VALUE_B)
        keys, degraded = ih.check_keys([p], None, "2026-11-18")
        self.assertEqual(degraded, [])
        by = {k["slot"]: k for k in keys}
        self.assertEqual(sorted(by), ["heroku/admin/main", "heroku/issued/alpha/prod/K"])
        rec = by["heroku/issued/alpha/prod/K"]
        self.assertEqual((rec["state"], rec["scope"]["missing"], rec["scope"]["excess"]),
                         ("valid", ["read"], ["global"]))
        self.assertIn(("GET", "/account", True, False), hk.calls)
        self.assertIn(("GET", "/oauth/authorizations/auth-1", False, True), hk.calls)
        self.assertNotIn("fakevalue", json.dumps(keys))

    def test_an_empty_slot_is_unreadable_and_never_sent(self):
        hk = FakeHerokuDoor(["read"])
        p = ih.heroku_provider(hk, slot_value=lambda *slot: None, read=lambda path: VALUE_B)
        keys, _ = ih.check_keys([p], None, "2026-11-18")
        rec = {k["slot"]: k for k in keys}["heroku/issued/alpha/prod/K"]
        self.assertEqual(rec["state"], "unreadable")
        self.assertFalse([c for c in hk.calls if c[2]])


class SentryRefusal(Exception):
    pass


class FakeSentry:
    PRESETS = {"releases": {"scopes": ("org:ci",)}}

    def __init__(self, scopes, proof_holds=True):
        self.scopes = scopes
        self.proof = proof_holds
        self.sent: list = []

    def ledger(self):
        return {"issued": {"alpha/prod/SENTRY": {"preset": "releases", "base_url": "https://sentry.example"}},
                "broad": {}}

    def read_slot(self, *slot):
        return VALUE_A

    def _narrow_value_ok(self, slot, row, value):
        if not self.proof:
            raise SentryRefusal("the slot no longer holds the token recorded for it; nothing sent")

    def scopes_of(self, value, base):
        self.sent.append(base)
        if isinstance(self.scopes, Exception):
            raise self.scopes
        return self.scopes


class SentryScope(unittest.TestCase):
    def test_auth_info_scopes_are_compared(self):
        sn = FakeSentry(["org:ci", "org:read"])
        keys, degraded = ih.check_keys([ih.sentry_provider(sn)], None, "2026-11-18")
        rec = keys[0]
        self.assertEqual((rec["slot"], rec["state"]), ("sentry/issued/alpha/prod/SENTRY", "valid"))
        self.assertEqual(rec["scope"]["excess"], ["org:read"])
        self.assertEqual(sn.sent, ["https://sentry.example"])

    def test_a_value_the_ledger_did_not_prove_is_never_sent(self):
        sn = FakeSentry(["org:ci"], proof_holds=False)
        keys, _ = ih.check_keys([ih.sentry_provider(sn)], None, "2026-11-18")
        self.assertEqual((keys[0]["state"], sn.sent), ("unreadable", []))

    def test_a_401_is_invalid(self):
        sn = FakeSentry(RuntimeError("Sentry answered HTTP 401 on GET /api/0/; provider response withheld"))
        keys, _ = ih.check_keys([ih.sentry_provider(sn)], None, "2026-11-18")
        self.assertEqual((keys[0]["state"], keys[0]["status"]), ("invalid", 401))



if __name__ == "__main__":
    unittest.main()
