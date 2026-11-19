#!/usr/bin/env python3
"""`access_grants.list_grants` answers `access-grants/1` (OBS-658, contract DEC-0187) for the keys the OpenRouter door
issued, and agrees with the contract's own rules.

* the vendored schema is the contract's bytes (digest in `fabric/access-grants/README.md`);
* the port of FAC-SEM-185 and grantState gives the contract's answers on the contract's cases;
* an answer built from a planted ledger validates against the schema, passes FAC-SEM-185, names a holder only when it
  is measured, keeps an id across a rotation and into its revocation, never shows a provider hash, and names every
  source it could not read in `degraded`, never a zero for an unknown spend.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import access_grants as AG  # noqa: E402

VENDORED = ROOT / "fabric" / "access-grants"
SCHEMA = json.loads((VENDORED / "access-grants.schema.json").read_text(encoding="utf-8"))
NOW = dt.datetime(2026, 11, 19, 14, 0, tzinfo=dt.timezone.utc)
HASH_A = "aa" * 32
HASH_B = "bb" * 32


def schema_errors(value, ref: str | None = None) -> list:
    import jsonschema
    schema = SCHEMA if ref is None else {**SCHEMA, "$ref": f"#/$defs/{ref}"}
    if ref is not None:
        schema = {k: v for k, v in schema.items() if k not in ("type", "required", "properties",
                                                                "additionalProperties")}
    return list(jsonschema.Draft202012Validator(schema).iter_errors(value))


def contract_case() -> dict:
    """The shape of the contract's positive fixture, with neutral names."""
    return {
        "protocol": "access-grants/1", "issuer": "media-desk.default", "generatedAt": "2026-11-19T14:00:00Z",
        "grants": [
            {"id": "key_1", "holder": {"kind": "agent", "agent": "alpha-agent.cloud"}, "scopes": ["media.generate"],
             "limitUsd": 25, "limitPeriod": "day", "spentDayUsd": 3.4,
             "issued": {"at": "2026-11-12T09:30:00Z", "by": "operator", "via": "agent",
                        "agent": "fabric-dashboards.default", "ref": "decision-1"},
             "expiresAt": "2027-02-12T09:30:00Z"},
            {"id": "key_2", "holder": {"kind": "person", "ref": "member-41"},
             "subject": {"kind": "account", "provider": "openrouter", "ref": "acct-org-2"}, "scopes": [],
             "limitUsd": None, "spentDayUsd": None,
             "issued": {"at": "2026-10-01T08:00:00Z", "by": "person", "via": "dashboard", "ref": "member-7"}},
            {"id": "key_3", "holder": {"kind": "unresolved", "label": "alpha agent cloud old"},
             "subject": {"kind": "agent", "agent": "text-desk.cloud"}, "scopes": ["texts.check"],
             "issued": {"at": "2026-09-20T12:00:00Z", "by": "service", "via": "config"},
             "revoked": {"at": "2026-11-18T16:00:00Z", "by": "service", "via": "automatic",
                         "reason": "Found in a public log; reissued as key_1."}},
        ]}


def codes(doc) -> list[str]:
    return [f"{f['code']} {f['instancePath']}" for f in AG.rules(doc)]


class TheVendoredSchemaIsTheContractsBytes(unittest.TestCase):
    def test_every_digest_the_readme_names_matches(self) -> None:
        readme = (VENDORED / "README.md").read_text(encoding="utf-8")
        rows = dict(re.findall(r"^\| `([^`]+)` \| `([0-9a-f]{64})` \|$", readme, re.M))
        files = sorted(p.relative_to(VENDORED).as_posix() for p in VENDORED.rglob("*.json"))
        self.assertEqual(sorted(rows), files)
        for name, digest in rows.items():
            self.assertEqual(hashlib.sha256((VENDORED / name).read_bytes()).hexdigest(), digest, name)

    def test_the_readme_names_the_commit(self) -> None:
        self.assertIn("320c7ab9b8397cb392ca5f3ac4d56dcc5c7786a1", (VENDORED / "README.md").read_text(encoding="utf-8"))

    def test_the_contract_case_is_valid(self) -> None:
        self.assertEqual(schema_errors(contract_case()), [])


class TheRulesPort(unittest.TestCase):
    """The cases of the contract's `test/access-grants-rules.test.ts` at 320c7ab9."""

    def test_the_valid_case_passes(self) -> None:
        self.assertEqual(AG.rules(contract_case()), [])

    def test_an_id_twice(self) -> None:
        doc = contract_case()
        doc["grants"][1]["id"] = doc["grants"][0]["id"]
        self.assertEqual(codes(doc), ["FAC-SEM-185 /grants/1/id"])

    def test_a_ceiling_pairs_with_its_window(self) -> None:
        doc = contract_case()
        del doc["grants"][0]["limitPeriod"]
        self.assertEqual(codes(doc), ["FAC-SEM-185 /grants/0/limitPeriod"])
        doc = contract_case()
        doc["grants"][1]["limitPeriod"] = "day"
        self.assertEqual(codes(doc), ["FAC-SEM-185 /grants/1/limitPeriod"])

    def test_the_calling_agent_exactly_when_via_agent(self) -> None:
        doc = contract_case()
        del doc["grants"][0]["issued"]["agent"]
        self.assertEqual(codes(doc), ["FAC-SEM-185 /grants/0/issued/agent"])
        doc = contract_case()
        doc["grants"][1]["issued"]["agent"] = "alpha-agent.cloud"
        self.assertEqual(codes(doc), ["FAC-SEM-185 /grants/1/issued/agent"])
        doc = contract_case()
        doc["grants"][2]["revoked"]["by"] = "agent"
        self.assertEqual(codes(doc), ["FAC-SEM-185 /grants/2/revoked/via"])
        doc = contract_case()
        doc["grants"][2]["revoked"] = {"at": "2026-11-18T16:00:00Z", "by": "agent", "via": "agent",
                                       "agent": "alpha-agent.cloud"}
        self.assertEqual(codes(doc), [])

    def test_issue_revocation_expiry_and_the_answers_time(self) -> None:
        doc = contract_case()
        doc["grants"][0]["issued"]["at"] = "2026-11-19T14:00:01Z"
        self.assertEqual(codes(doc), ["FAC-SEM-185 /grants/0/issued/at"])
        doc = contract_case()
        doc["grants"][2]["revoked"]["at"] = "2026-09-20T11:59:59Z"
        self.assertEqual(codes(doc), ["FAC-SEM-185 /grants/2/revoked/at"])
        doc = contract_case()
        doc["grants"][2]["revoked"]["at"] = "2026-11-19T14:00:01Z"
        self.assertEqual(codes(doc), ["FAC-SEM-185 /grants/2/revoked/at"])
        doc = contract_case()
        doc["grants"][0]["expiresAt"] = doc["grants"][0]["issued"]["at"]
        self.assertEqual(codes(doc), ["FAC-SEM-185 /grants/0/expiresAt"])

    def test_a_breach_is_shown_not_refused(self) -> None:
        doc = contract_case()
        doc["grants"][0]["spentDayUsd"] = 40
        self.assertEqual(codes(doc), [])

    def test_malformed_input_never_raises(self) -> None:
        self.assertEqual(AG.rules({}), [])
        self.assertEqual(AG.rules({"grants": "x"}), [])
        self.assertEqual(AG.rules({"grants": [None, 3, {}], "generatedAt": "not a time"}), [])
        self.assertEqual(codes({"grants": [{}, {}]}), ["FAC-SEM-185 /grants/1/id"])
        self.assertEqual(codes({"grants": [{"id": "a", "issued": {"at": "bad"}, "revoked": {
            "at": "2026-01-01T00:00:00Z"}, "expiresAt": "2026-01-01T00:00:00Z"}]}), [])
        self.assertEqual(codes({"grants": [{"id": "a", "revoked": "yes"}]}), [])

    def test_grant_state(self) -> None:
        now = NOW.timestamp()
        active, person, revoked = contract_case()["grants"]
        self.assertEqual(AG.grant_state(active, now), "active")
        self.assertEqual(AG.grant_state(person, now), "active")
        self.assertEqual(AG.grant_state(revoked, now), "revoked")
        self.assertEqual(AG.grant_state({**revoked, "revoked": None, "expiresAt": "2026-11-19T14:00:00Z"}, now),
                         "expired")
        self.assertEqual(AG.grant_state({**active, "expiresAt": "garbage"}, now), "active")
        self.assertIsNone(AG.grant_state(None, now))
        self.assertIsNone(AG.grant_state([], now))
        self.assertIsNone(AG.grant_state({}, float("nan")))

    def test_the_schemas_inputs(self) -> None:
        self.assertEqual(schema_errors({}, "listInput"), [])
        self.assertEqual(schema_errors({"holder": "alpha-agent.cloud", "includeRevoked": True, "limit": 500},
                                       "listInput"), [])
        self.assertNotEqual(schema_errors({"holder": "alpha agent cloud"}, "listInput"), [])
        self.assertNotEqual(schema_errors({"limit": 501}, "listInput"), [])


def ledger() -> dict:
    return {
        "issued": {
            "alpha-web": {"account": "acct-a", "hash": HASH_A, "destination": "vault:alpha-web/prod/OPENROUTER_API_KEY",
                          "project": "alpha-web", "limit_usd": 100.0, "limit_reset": "monthly",
                          "issued_on": "2026-11-10", "rotated_on": "2026-11-15", "rotations": 1,
                          "expires_at": "2027-02-08T00:00:00Z"},
            "shared-tool": {"account": "acct-a", "hash": HASH_B, "destination": "vault:shared-tool/prod/KEY",
                            "project": "shared-tool", "limit_usd": 10.0, "limit_reset": "daily",
                            "issued_on": "2026-11-01"},
            "observatory": {"account": "acct-b", "hash": "cc" * 32, "destination": "observatory",
                            "limit_usd": 5.0, "limit_reset": None, "issued_on": "2026-10-01"},
            "half-made": {"account": "acct-a", "hash": None, "destination": "vault:x/prod/K", "pending": True,
                          "limit_usd": 10.0, "limit_reset": "monthly", "issued_on": "2026-11-19"},
            "gone-by-now": {"account": "acct-a", "hash": "dd" * 32, "destination": "vault:old/prod/K",
                            "limit_usd": 10.0, "limit_reset": "monthly", "issued_on": "2026-08-01",
                            "expires_at": "2026-11-01T00:00:00Z"},
        },
        "retired": [
            {"name": "beta-bot", "account": "acct-a", "hash": "ee" * 32, "project": "beta-bot",
             "retired_on": "2026-11-18", "reason": "revoked", "deleted": True, "usage": "read",
             "read_at": "2026-11-18T10:00:00+00:00", "usage_daily": 1.5},
            {"name": "alpha-web", "account": "acct-a", "hash": "ff" * 32, "project": "alpha-web",
             "retired_on": "2026-11-15", "reason": "rotated", "deleted": True},
            {"name": "gamma", "account": "acct-a", "hash": "11" * 32, "project": "gamma", "issued_on": "2026-11-02",
             "destination": "vault:gamma/prod/K", "limit_usd": 20.0, "limit_reset": "weekly",
             "retired_on": "2026-11-19", "reason": "revoked", "deleted": True, "usage": "read",
             "read_at": "2026-11-19T09:00:00+00:00", "usage_daily": 0.25},
        ],
    }


def sources(**over) -> AG.Sources:
    moves = [{"at": "2026-11-10T08:15:00Z", "event": "issue", "secret": "openrouter/acct-a/alpha-web"},
             {"at": "2026-11-18T11:00:00Z", "event": "revoke", "secret": "openrouter/acct-a/beta-bot"}]
    folders = {"alpha-web": {"alpha-web.default"}, "shared-tool": {"tool.default", "tool.cloud"},
               "gamma": {"gamma.default"}}
    base = dict(ledger=ledger, movements=lambda: moves,
                spend=lambda account: ("2026-11-19", {HASH_A: 2.5, HASH_B: None}) if account == "acct-a"
                else ("2026-11-19", {}),
                holders=lambda folder: set(folders.get(folder, ())), issuer="project-observatory.default", now=NOW)
    base.update(over)
    return AG.Sources(**base)


def by_label(doc: dict) -> dict:
    out = {}
    for g in doc["grants"]:
        h = g["holder"]
        out[h.get("agent") or h.get("label")] = g
    return out


class TheAnswer(unittest.TestCase):
    def test_validates_and_passes_the_rule(self) -> None:
        for args in ({}, {"includeRevoked": True}):
            doc = AG.list_grants(args, sources())
            self.assertEqual(schema_errors(doc), [], args)
            self.assertEqual(AG.rules(doc), [], args)
            self.assertEqual(doc["protocol"], "access-grants/1")
            self.assertEqual(doc["issuer"], "project-observatory.default")
            self.assertNotIn("doors", doc)

    def test_no_provider_hash_reaches_the_answer(self) -> None:
        text = json.dumps(AG.list_grants({"includeRevoked": True}, sources()))
        for planted in (HASH_A, HASH_B, "cc" * 32, "dd" * 32, "ee" * 32, "11" * 32):
            self.assertNotIn(planted, text)
            self.assertNotIn(planted[:12], text)

    def test_the_holder_only_when_measured(self) -> None:
        g = by_label(AG.list_grants({}, sources()))
        self.assertEqual(g["alpha-web.default"]["holder"], {"kind": "agent", "agent": "alpha-web.default"})
        self.assertEqual(g["shared-tool"]["holder"], {"kind": "unresolved", "label": "shared-tool"},
                         "a folder whose project has two services is not guessed")
        self.assertEqual(g["project-observatory.default"]["holder"]["kind"], "agent",
                         "a key delivered to the engine is the engine's")

    def test_money(self) -> None:
        g = by_label(AG.list_grants({}, sources()))
        self.assertEqual((g["alpha-web.default"]["limitUsd"], g["alpha-web.default"]["limitPeriod"]), (100.0, "month"))
        self.assertEqual(g["alpha-web.default"]["spentDayUsd"], 2.5)
        self.assertEqual(g["shared-tool"]["limitPeriod"], "day")
        self.assertIsNone(g["shared-tool"]["spentDayUsd"], "a counter the listing did not give is unknown")
        self.assertEqual(g["project-observatory.default"]["limitPeriod"], "total", "no reset is a lifetime ceiling")
        self.assertIsNone(g["project-observatory.default"]["spentDayUsd"], "a key missing from its listing")

    def test_times_from_the_journal_else_the_day(self) -> None:
        g = by_label(AG.list_grants({}, sources()))
        self.assertEqual(g["alpha-web.default"]["issued"], {"at": "2026-11-10T08:15:00Z", "by": "operator",
                                                            "via": "config"})
        self.assertEqual(g["shared-tool"]["issued"]["at"], "2026-11-01T00:00:00Z")
        self.assertEqual(g["alpha-web.default"]["expiresAt"], "2027-02-08T00:00:00Z")

    def test_pending_and_expired_keys(self) -> None:
        doc = AG.list_grants({}, sources())
        labels = set(by_label(doc))
        self.assertNotIn("half-made", labels)
        self.assertNotIn("gone-by-now", labels, "an expired key does not admit, so it waits for includeRevoked")
        self.assertTrue(any("pending" in d for d in doc["degraded"]))
        everything = AG.list_grants({"includeRevoked": True}, sources())
        self.assertIn("gone-by-now", set(by_label(everything)))

    def test_revocations(self) -> None:
        doc = AG.list_grants({"includeRevoked": True}, sources())
        revoked = [g for g in doc["grants"] if "revoked" in g]
        self.assertEqual(len(revoked), 2, "a rotation's tombstone is not a revocation")
        beta = next(g for g in revoked if g["holder"].get("label") == "beta-bot")
        self.assertEqual(beta["revoked"]["at"], "2026-11-18T11:00:00Z")
        self.assertEqual(beta["revoked"]["reason"], "Revoked through the OpenRouter door.")
        self.assertIsNone(beta["spentDayUsd"], "a spend read on another day is not today's")
        self.assertNotIn("limitUsd", beta, "an old tombstone without its ceiling does not invent one")
        gamma = next(g for g in revoked if g["holder"].get("agent") == "gamma.default")
        self.assertEqual((gamma["limitUsd"], gamma["limitPeriod"], gamma["spentDayUsd"]), (20.0, "week", 0.25))
        self.assertEqual(gamma["id"], AG.grant_id("acct-a", "gamma", "2026-11-02"),
                         "a tombstone with its day keeps the id the live key had")
        self.assertLessEqual(gamma["issued"]["at"], gamma["revoked"]["at"])

    def test_an_id_survives_a_rotation(self) -> None:
        before = by_label(AG.list_grants({}, sources()))["alpha-web.default"]["id"]
        rotated = ledger()
        rotated["issued"]["alpha-web"].update(hash="99" * 32, rotated_on="2026-11-19", rotations=2)
        after = by_label(AG.list_grants({}, sources(ledger=lambda: rotated)))["alpha-web.default"]["id"]
        self.assertEqual(before, after)

    def test_filters_and_pages(self) -> None:
        doc = AG.list_grants({"holder": "alpha-web.default"}, sources())
        self.assertEqual([g["holder"]["agent"] for g in doc["grants"]], ["alpha-web.default"])
        self.assertEqual(AG.list_grants({"subject": "alpha-web.default"}, sources())["grants"], [])
        everything = AG.list_grants({"includeRevoked": True}, sources())["grants"]
        first = AG.list_grants({"includeRevoked": True, "limit": 2}, sources())
        self.assertEqual(len(first["grants"]), 2)
        second = AG.list_grants({"includeRevoked": True, "limit": 2, "cursor": first["nextCursor"]}, sources())
        rest = AG.list_grants({"includeRevoked": True, "limit": 500, "cursor": first["nextCursor"]}, sources())
        self.assertEqual(first["grants"] + rest["grants"], everything)
        self.assertEqual(second["grants"], everything[2:4])
        self.assertNotIn("nextCursor", rest)
        stray = AG.list_grants({"cursor": "not-ours-at-all"}, sources())
        self.assertTrue(any("cursor" in d for d in stray["degraded"]))

    def test_unread_sources_are_named_never_zero(self) -> None:
        def down(_account):
            raise AG.Unread("HTTPError")
        doc = AG.list_grants({}, sources(spend=down))
        self.assertTrue(all(g["spentDayUsd"] is None for g in doc["grants"]))
        self.assertTrue(any("acct-a" in d and "HTTPError" in d for d in doc["degraded"]))

        def no_registry(_folder):
            raise AG.Unread("the registry could not be read (OSError)")
        doc = AG.list_grants({}, sources(holders=no_registry))
        self.assertTrue(all(g["holder"]["kind"] != "agent" or g["holder"]["agent"] == "project-observatory.default"
                            for g in doc["grants"]))
        self.assertEqual(sum("registry" in d for d in doc["degraded"]), 1)

        def no_ledger():
            raise AG.Unread("the OpenRouter door's ledger could not be read (OSError)")
        doc = AG.list_grants({}, sources(ledger=no_ledger))
        self.assertEqual(doc["grants"], [])
        self.assertTrue(any("not the same as none issued" in d for d in doc["degraded"]))
        self.assertEqual(schema_errors(doc), [])

    def test_a_slow_provider_does_not_hold_the_answer(self) -> None:
        gate = threading.Event()

        def slow(account):
            gate.wait(5)
            return ("2026-11-19", {HASH_A: 2.5})
        saved = AG.SPEND_BUDGET
        AG.SPEND_BUDGET = 0.2
        try:
            doc = AG.list_grants({}, sources(spend=slow))
        finally:
            AG.SPEND_BUDGET = saved
            gate.set()
        self.assertTrue(any("not read within" in d for d in doc["degraded"]))
        self.assertTrue(all(g["spentDayUsd"] is None for g in doc["grants"]))

    def test_a_listing_from_another_day_is_not_today(self) -> None:
        doc = AG.list_grants({}, sources(spend=lambda a: ("2026-11-18", {HASH_A: 9.0})))
        self.assertTrue(all(g["spentDayUsd"] is None for g in doc["grants"]))

    def test_the_input_is_not_changed(self) -> None:
        args = {"includeRevoked": True, "limit": 3}
        kept = copy.deepcopy(args)
        AG.list_grants(args, sources())
        self.assertEqual(args, kept)


if __name__ == "__main__":
    unittest.main()
