#!/usr/bin/env python3
"""The meter (meter.py, OBS-55) and the delegation verifier under it (delegation.py, contract DEC-0039).

A throwaway Ed25519 estate key signs tokens the way Fabric's hub will; a temporary grant directory stands for the
hub's grant projection (Fabric ADR-0144 amendment 1). The verifier is walked through the contract's table DL-13
row by row; the meter through each window and level, each grant state, settlement, lapse and a race of
concurrent checks against one limit. Every answer is validated against the contract's `grant-check` schema.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
except ImportError:                                    # the engine's `full` extra
    Ed25519PrivateKey = None

import delegation as D                                 # noqa: E402
import meter as M                                      # noqa: E402

SCHEMAS = ROOT / "fabric" / "delegation-schemas"
ISSUER = "urn:fabric:estate:test"
NOW = int(datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc).timestamp())   # a Wednesday
GRANT_ID = "urn:grant:project-a:writer"


def b64(raw: bytes) -> str:
    return D.b64url_encode(raw)


class Estate:
    """An issuer: a key pair, its JWK Set, and a signer for any header and claims."""

    def __init__(self, kid: str = "estate-1"):
        self.key = Ed25519PrivateKey.generate()
        self.kid = kid
        public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.jwk = {"kty": "OKP", "crv": "Ed25519", "x": b64(public), "kid": kid, "use": "sig", "alg": "EdDSA"}

    @property
    def jwks(self) -> dict:
        return {"keys": [self.jwk]}

    def sign(self, claims: dict, header: dict | None = None, *, raw_payload: str | None = None) -> str:
        head = {"alg": "EdDSA", "typ": D.DELEGATION_TYP, "kid": self.kid, **(header or {})}
        h = b64(json.dumps(head).encode())
        p = b64((raw_payload if raw_payload is not None else json.dumps(claims)).encode())
        return f"{h}.{p}.{b64(self.key.sign(f'{h}.{p}'.encode()))}"


def claims(**over) -> dict:
    base = {"iss": ISSUER, "sub": "urn:person:operator", "act": {"sub": "example-writer", "act": {"sub": "example-media"}},
            "aud": "project-observatory", "scope": "grant.check", "effect": ["none"], "grant": [f"{GRANT_ID}#3"],
            "project": "urn:project:a", "iat": NOW - 10, "exp": NOW + 590, "jti": "tok-meter-0000000001"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def grant(**over) -> dict:
    doc = {"kind": "standing-grant",
           "meta": {"id": GRANT_ID, "revision": 3, "contentHash": "sha256:" + "4" * 64,
                    "createdAt": "2026-10-11T09:00:00Z", "createdBy": "urn:person:operator"},
           "project": "urn:project:a",
           "principal": {"id": "urn:agent:example-media", "revision": 1, "contentHash": "sha256:" + "5" * 64},
           "effects": ["send"], "resourcePatterns": ["urn:project:a:drafts/*"], "dataClasses": ["project-internal"],
           "startsAt": "2026-10-11T09:00:00Z", "expiresAt": "2026-11-11T09:00:00Z", "idempotency": "required",
           "issuer": "urn:person:operator", "state": "active", "principalKind": "agent", "audience": "example-writer",
           "capabilities": ["writer.write"], "effectCeiling": ["draft", "send"],
           "spend": {"mode": "limited", "limits": [{"window": "task", "limitUsd": 2}, {"window": "day", "limitUsd": 5},
                                                   {"window": "month", "limitUsd": 50}],
                     "perCallMaxUsd": 1.5, "payer": "owner",
                     "credentialSource": {"kind": "project-key", "ref": "openrouter:project-a"}}}
    for key, value in over.items():
        if value is None:
            doc.pop(key, None)
        else:
            doc[key] = value
    return doc


def request(amount: float, jti: str = "tok-callee-000000001", **over) -> dict:
    return {"amountUsd": amount, "tokenJti": jti, **over}


@unittest.skipIf(Ed25519PrivateKey is None, "needs the cryptography package (the engine's full extra)")
class Verifier(unittest.TestCase):
    """DL-13, in its order."""

    def setUp(self):
        self.estate = Estate()

    def verify(self, token, jwks=None, **over):
        options = {"audience": "project-observatory", "jwks": self.estate.jwks if jwks is None else jwks,
                   "now": NOW, "capability": "grant.check", "effect": "none", "issuer": ISSUER, **over}
        return D.verify_delegation(token, **options)

    def assertRefused(self, result, code, detail=None):
        self.assertFalse(result.ok, result)
        self.assertEqual(result.code, code, result)
        if detail:
            self.assertEqual(result.detail, detail)

    def test_a_valid_token_names_the_person_the_caller_and_the_chain(self):
        result = self.verify(self.estate.sign(claims()))
        self.assertTrue(result.ok, result)
        self.assertEqual((result.principal, result.caller, result.chain, result.kid),
                         ("urn:person:operator", "example-writer", ("example-writer", "example-media"), "estate-1"))

    def test_no_published_keys_is_the_issuers(self):
        self.assertRefused(self.verify(self.estate.sign(claims()), jwks={"keys": []}), "issuer-unavailable")

    def test_the_header_is_exactly_the_profiles(self):
        for header, detail in (({"alg": "HS256"}, "alg-not-eddsa"), ({"typ": D.APPROVAL_TYP}, "wrong-typ"),
                               ({"kid": ""}, "no-kid"), ({"jku": "https://evil.example/k"}, "header-jku"),
                               ({"jwk": self.estate.jwk}, "header-jwk"), ({"crit": ["x"]}, "header-crit")):
            self.assertRefused(self.verify(self.estate.sign(claims(), header)), "unauthenticated", detail)

    def test_a_duplicate_member_is_refused_never_resolved(self):
        body = json.dumps(claims())[:-1] + ', "aud": "someone-else"}'
        self.assertRefused(self.verify(self.estate.sign({}, raw_payload=body)), "unauthenticated", "duplicate-member")

    def test_malformed_tokens(self):
        for token in ("", "a.b", "a.b.c.d", "x" * 9000, None, "!!.e30.AA"):
            self.assertRefused(self.verify(token), "unauthenticated")

    def test_another_key_or_a_tampered_payload_fails_the_signature(self):
        stranger = Estate()
        self.assertRefused(self.verify(stranger.sign(claims())), "unauthenticated", "bad-signature")
        h, _p, s = self.estate.sign(claims()).split(".")
        forged = b64(json.dumps(claims(aud="project-observatory", sub="urn:person:mallory")).encode())
        self.assertRefused(self.verify(f"{h}.{forged}.{s}"), "unauthenticated", "bad-signature")

    def test_key_lookup(self):
        token = self.estate.sign(claims())
        self.assertRefused(self.verify(token, jwks={"keys": [{**self.estate.jwk, "kid": "other"}]}),
                           "unauthenticated", "unknown-key")
        self.assertRefused(self.verify(token, jwks={"keys": [self.estate.jwk, self.estate.jwk]}),
                           "issuer-unavailable", "duplicate-kid")
        # DEC-0045 item 7 (F-B-319): a published key of another use is the issuer's fault, as any unusable key.
        self.assertRefused(self.verify(token, jwks={"keys": [{**self.estate.jwk, "use": "enc"}]}),
                           "issuer-unavailable", "key-not-for-signing")
        self.assertRefused(self.verify(token, jwks={"keys": [{**self.estate.jwk, "crv": "X25519"}]}),
                           "issuer-unavailable", "unusable-key")
        self.assertRefused(self.verify(token, jwks={"keys": [{**self.estate.jwk, "d": "secret"}]}),
                           "issuer-unavailable", "unusable-key")

    def test_claims_and_issuer(self):
        self.assertRefused(self.verify(self.estate.sign(claims(aud=["project-observatory"]))), "unauthenticated",
                           "malformed-claims")
        self.assertRefused(self.verify(self.estate.sign(claims(effect=["spend"]))), "unauthenticated",
                           "malformed-claims")
        self.assertRefused(self.verify(self.estate.sign(claims(grant=[]))), "unauthenticated", "malformed-claims")
        self.assertRefused(self.verify(self.estate.sign(claims(iss="urn:fabric:estate:other"))), "unauthenticated",
                           "wrong-issuer")

    def test_times(self):
        self.assertRefused(self.verify(self.estate.sign(claims(iat=NOW, exp=NOW + 601))), "unauthenticated",
                           "lifetime-above-profile")
        self.assertRefused(self.verify(self.estate.sign(claims(iat=NOW + 61, exp=NOW + 600))), "unauthenticated",
                           "issued-in-future")
        self.assertRefused(self.verify(self.estate.sign(claims(nbf=NOW + 61))), "unauthenticated", "not-yet-valid")
        self.assertTrue(self.verify(self.estate.sign(claims(iat=NOW - 600, exp=NOW - 59))).ok, "within the leeway")
        self.assertRefused(self.verify(self.estate.sign(claims(iat=NOW - 600, exp=NOW - 61))), "token-expired")

    def test_audience_chain_scope_and_effect(self):
        self.assertRefused(self.verify(self.estate.sign(claims(aud="copylot"))), "wrong-audience")
        deep = {"sub": "agent-a", "act": {"sub": "agent-b", "act": {"sub": "agent-c", "act": {
            "sub": "agent-d", "act": {"sub": "agent-e"}}}}}
        self.assertRefused(self.verify(self.estate.sign(claims(act=deep))), "chain-too-deep", "depth-5")
        self.assertRefused(self.verify(self.estate.sign(claims(act={"sub": 7}))), "unauthenticated", "malformed-chain")
        self.assertRefused(self.verify(self.estate.sign(claims(scope="grant.checker"))), "scope-not-granted")
        token = self.estate.sign(claims(effect=["send"], scope="writer.send"))
        self.assertRefused(self.verify(token, capability="writer.send", effect="send", read_only=True),
                           "effect-above-ceiling", "read-only-door")
        self.assertTrue(self.verify(token, capability="writer.send", effect="draft").ok, "draft is below send")
        self.assertRefused(self.verify(token, capability="writer.send", effect="charge"), "effect-above-ceiling")

    def test_the_meters_hop_may_be_one_actor_deeper(self):
        """Amendment 3: a token for the meter (read-only, effect none) may carry five actors; no other may."""
        def chain(n):
            node = None
            for i in reversed(range(n)):
                node = {"sub": f"agent-{i}", **({"act": node} if node else {})}
            return node
        five, six = self.estate.sign(claims(act=chain(5))), self.estate.sign(claims(act=chain(6)))
        self.assertTrue(D.verify_meter_token(five, meter="project-observatory", capability="grant.check",
                                             jwks=self.estate.jwks, now=NOW, issuer=ISSUER).ok)
        self.assertEqual(D.verify_meter_token(six, meter="project-observatory", capability="grant.check",
                                              jwks=self.estate.jwks, now=NOW, issuer=ISSUER).detail,
                         "chain-too-deep:depth-6")
        self.assertRefused(self.verify(five), "chain-too-deep", "depth-5")

    def test_the_meter_token_maps_every_refusal_to_principal_unproven(self):
        result = D.verify_meter_token(self.estate.sign(claims(aud="copylot")), meter="project-observatory",
                                      capability="grant.check", jwks=self.estate.jwks, now=NOW, issuer=ISSUER)
        self.assertEqual((result.code, result.detail), ("principal-unproven", "wrong-audience:audience-mismatch"))

    def test_reading_the_token_from_headers(self):
        token = self.estate.sign(claims())
        self.assertEqual(D.read_delegation_token({"authorization": f"Bearer {token}"}), (token, "authorization"))
        self.assertEqual(D.read_delegation_token({"Fabric-Delegation": " ", "Authorization": f"Bearer {token}"}),
                         ("", "fabric-delegation"), "a present Fabric-Delegation is the token, even empty")
        self.assertIsNone(D.read_delegation_token({"Authorization": "Bearer opaque-descriptor-token"}))


@unittest.skipIf(Ed25519PrivateKey is None, "needs the cryptography package (the engine's full extra)")
class Meter(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.grants = self.dir / "grants"
        self.grants.mkdir(mode=0o700)
        self.grants.chmod(0o700)
        self.estate = Estate()
        (self.dir / "jwks.json").write_text(json.dumps(self.estate.jwks))
        self.now = [NOW]
        self.write_grant(grant())
        self.meter = self.make()
        self.validator = self.schema_validator()

    def make(self, **config) -> M.Meter:
        base = {"issuer": ISSUER, "jwks": str(self.dir / "jwks.json"), "grantsDir": str(self.grants),
                "keyCeilingsUsd": {"openrouter:project-a": 30}}
        return M.Meter(db_path=self.dir / "meter" / "meter.db", config={**base, **config}, clock=lambda: self.now[0])

    def write_grant(self, doc: dict) -> Path:
        """As the hub writes it: 0600 in a 0700 directory."""
        path = self.grants / f"{doc['meta']['id']}.json"
        path.write_text(json.dumps(doc))
        path.chmod(0o600)
        return path

    def schema_validator(self):
        try:
            import jsonschema
            from referencing import Registry, Resource
        except ImportError:
            return None
        docs = {p.name: json.loads(p.read_text()) for p in SCHEMAS.glob("*.schema.json")}
        registry = Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in docs.values())
        cls = jsonschema.validators.validator_for(docs["grant-check.schema.json"])
        return {"now": cls(docs["grant-check.schema.json"], registry=registry),
                "request": cls(docs["grant-check-request.schema.json"], registry=registry),
                "grant": cls({"$ref": docs["governance.schema.json"]["$id"] + "#/$defs/grant"}, registry=registry)}

    def check(self, amount, token_claims=None, **req) -> dict:
        signed = dict(token_claims or claims())
        shift = self.now[0] - NOW               # the hub mints a fresh token for every call
        signed["iat"], signed["exp"] = signed["iat"] + shift, signed["exp"] + shift
        token = self.estate.sign(signed)
        body = request(amount, **req)
        answer = self.meter.check(token, body)
        if self.validator:
            self.validator["request"].validate(body)
            self.validator["now"].validate(answer)
        return answer

    def assertDenied(self, answer, code, level=None, window=None):
        self.assertEqual(answer["decision"], "denied", answer)
        self.assertEqual((answer["code"], answer["level"], answer.get("window")), (code, level, window), answer)

    def test_the_planted_grant_is_a_valid_standing_grant(self):
        if not self.validator:
            self.skipTest("needs jsonschema")
        self.validator["grant"].validate(grant())

    def test_an_allowed_check_reserves_and_answers_what_remains(self):
        answer = self.check(0.5)
        self.assertEqual(answer["decision"], "allowed", answer)
        self.assertTrue(answer["receipt"].startswith("rcpt_"))
        self.assertEqual((answer["grantRef"], answer["reservedUsd"], answer["remainingUsd"]), (f"{GRANT_ID}#3", 0.5, 1.5))
        self.assertEqual(answer["expiresAt"], M.iso(NOW + 3600))

    def test_a_reservation_is_refused_at_the_window(self):
        """Fabric's step-3 gate: a reservation refused at the window."""
        for i in (1, 2, 3):                       # three tasks, each inside its own 2 USD task window
            self.assertEqual(self.check(1.4, claims(task=f"task-{i}"))["decision"], "allowed")
        self.assertDenied(self.check(1.0, claims(task="task-4")), "limit-reached", "L1", "day")
        self.now[0] += 86400                      # the next UTC day opens a new day window
        self.assertEqual(self.check(1.0, claims(task="task-5"))["decision"], "allowed")

    def test_the_per_call_maximum_is_window_call_at_L3(self):
        self.assertDenied(self.check(1.6), "limit-reached", "L3", "call")

    def test_the_task_window_keys_on_task_then_session_then_grant(self):
        self.assertEqual(self.check(1.2, claims(task="task-1"), jti="tok-callee-000000001")["decision"], "allowed")
        self.assertDenied(self.check(1.2, claims(task="task-1"), jti="tok-callee-000000002"), "limit-reached", "L2", "task")
        self.assertEqual(self.check(1.2, claims(task="task-2"), jti="tok-callee-000000002")["decision"], "allowed")
        session = claims(act={"sub": "example-writer", "act": {"sub": "example-media", "session": "s-1"}})
        self.assertEqual(self.check(1.2, session, jti="tok-callee-000000003")["decision"], "allowed")
        self.assertDenied(self.check(1.2, session, jti="tok-callee-000000004"), "limit-reached", "L2", "task")
        self.assertEqual(M.Meter.task_key(claims(), f"{GRANT_ID}#3"), f"grant:{GRANT_ID}#3")

    def test_without_task_or_session_the_task_window_is_the_whole_grant(self):
        """An unsigned tokenJti never keys the window: fresh jtis must not open fresh L2 windows."""
        self.assertEqual(self.check(1.2, jti="tok-callee-000000001")["decision"], "allowed")
        self.assertDenied(self.check(1.2, jti="tok-callee-000000002"), "limit-reached", "L2", "task")

    def test_total_project_week_and_month_windows(self):
        self.write_grant(grant(spend={**grant()["spend"], "perCallMaxUsd": None, "limits": [
            {"window": "total", "limitUsd": 3}]} | {"perCallMaxUsd": 10}))
        self.assertEqual(self.check(2)["decision"], "allowed")
        self.assertDenied(self.check(2, jti="tok-callee-000000002"), "limit-reached", "L1", "total")
        other = grant(meta={**grant()["meta"], "id": "urn:grant:project-a:other", "revision": 1},
                      spend={**grant()["spend"], "limits": [{"window": "project", "limitUsd": 2.5}], "perCallMaxUsd": 10})
        self.write_grant(other)
        token = claims(grant=["urn:grant:project-a:other#1"])
        self.assertDenied(self.check(1, token), "limit-reached", "L1", "project")   # 2 already spent on project a
        weekly = grant(spend={**grant()["spend"], "limits": [{"window": "week", "limitUsd": 4}], "perCallMaxUsd": 10})
        self.write_grant(weekly)
        self.now[0] = NOW + 5 * 86400              # Monday: a new ISO week
        self.assertEqual(self.check(4, jti="tok-callee-000000003")["decision"], "allowed")
        self.assertDenied(self.check(0.1, jti="tok-callee-000000004"), "limit-reached", "L1", "week")

    def test_L0_the_estate_total_and_the_keys_ceiling(self):
        unlimited = grant(spend={"mode": "unlimited", "payer": "owner",
                                 "credentialSource": {"kind": "project-key", "ref": "openrouter:project-a"}})
        self.write_grant(unlimited)
        self.assertIsNone(self.check(29)["remainingUsd"], "an unlimited grant has no grant-level remainder")
        self.assertDenied(self.check(2, jti="tok-callee-000000002"), "limit-reached", "L0", "month")  # key ceiling 30
        self.meter = self.make(keyCeilingsUsd={}, estateMonthUsd=40)
        self.assertEqual(self.check(10, jti="tok-callee-000000003")["decision"], "allowed")
        self.assertDenied(self.check(2, jti="tok-callee-000000004"), "limit-reached", "L0", "month")    # 39 + 2 > 40
        status = self.meter.status()
        self.assertEqual((status["estateUsd"], status["alert"]), (39.0, True))

    def test_measured_provider_spend_counts_against_L0(self):
        """OBS-56: with a fresh spend scan the estate total is what the providers charged plus open reservations."""
        spend = self.dir / "spend.json"
        unlimited = grant(spend={"mode": "unlimited", "payer": "owner",
                                 "credentialSource": {"kind": "capability", "agent": "urn:agent:x"}})
        self.write_grant(unlimited)
        spend.write_text(json.dumps({"month": M.iso(NOW)[:7], "scanned_at": M.iso(NOW - 3600), "total_usd": 995}))
        self.meter = self.make(spendFile=str(spend), keyCeilingsUsd={})
        self.assertEqual(self.check(4, claims(task="t-1"))["decision"], "allowed")          # 995 + 4 = 999
        self.assertDenied(self.check(2, claims(task="t-2")), "limit-reached", "L0", "month")  # 999 + 2 > 1000
        self.assertEqual(self.meter.status()["estateCountedFrom"], "providers+open")
        spend.write_text(json.dumps({"month": M.iso(NOW)[:7], "scanned_at": M.iso(NOW - 40 * 3600), "total_usd": 995}))
        self.assertEqual(self.check(2, claims(task="t-3"))["decision"], "allowed", "a stale scan is not believed")
        self.assertEqual(self.meter.status()["estateCountedFrom"], "ledger")
        spend.write_text(json.dumps({"month": "2026-09", "scanned_at": M.iso(NOW), "total_usd": 995}))
        self.assertEqual(self.meter.status()["estateCountedFrom"], "ledger", "last month's scan is not this month's")

    def test_grant_states(self):
        cases = [(grant(state="revoked"), "grant-revoked"), (grant(state="expired"), "grant-expired"),
                 (grant(expiresAt="2026-10-12T00:00:00Z"), "grant-expired"),
                 (grant(startsAt="2026-10-20T00:00:00Z"), "no-grant"),
                 (grant(meta={**grant()["meta"], "revision": 4}), "no-grant"),
                 (grant(state="paused"), "unknown-state"), (grant(project="urn:project:b"), "no-grant"),
                 (grant(spend=None), "no-grant")]
        for doc, code in cases:
            with self.subTest(code=code, doc=doc.get("state")):
                self.write_grant(doc)
                self.assertDenied(self.check(0.1), code)
        self.write_grant(grant(spend=None))
        self.assertEqual(self.check(0)["decision"], "allowed", "a grant without spend still answers a zero check")

    def test_the_projection_missing_or_unreadable(self):
        (self.grants / f"{GRANT_ID}.json").unlink()
        self.assertDenied(self.check(0.1), "no-grant")
        (self.grants / f"{GRANT_ID}.json").write_text("{not json")
        self.assertDenied(self.check(0.1), "unknown-state")
        self.meter = self.make(grantsDir="")
        self.assertDenied(self.check(0.1), "unknown-state")
        self.assertDenied(self.check(0.1, claims(grant=["../../etc/passwd#1"])), "unknown-state")
        self.meter = self.make()
        self.assertDenied(self.check(0.1, claims(grant=["../../etc/passwd#1"])), "no-grant")

    def test_the_projection_is_read_strictly(self):
        """DEC-0041 DL-23/DL-24: the owner's alone, a regular file, the standing-grant shape, RFC 3339 times."""
        import os as _os
        path = self.grants / f"{GRANT_ID}.json"
        path.chmod(0o644)
        self.assertDenied(self.check(0.1), "unknown-state")
        self.assertIn("loose-permissions", self.meter.check(self.estate.sign(claims()), request(0.1))["reason"])
        path.chmod(0o600)
        self.grants.chmod(0o755)
        self.assertDenied(self.check(0.1), "unknown-state")
        self.grants.chmod(0o700)
        path.unlink()
        target = self.dir / "outside.json"                        # this grant itself, but not where the hub writes
        target.write_text(json.dumps(grant()))
        target.chmod(0o600)
        path.symlink_to(target)
        self.assertDenied(self.check(0.1), "unknown-state")
        path.unlink()
        _os.mkfifo(path, 0o600)
        self.assertDenied(self.check(0.1), "unknown-state")       # O_NONBLOCK: a FIFO never hangs the meter
        path.unlink()
        self.write_grant(grant(startsAt="2026-10-11 09:00:00"))
        self.assertDenied(self.check(0.1), "unknown-state")       # a local time is not RFC 3339
        self.write_grant(grant(dataClasses=[]))
        self.assertDenied(self.check(0.1), "unknown-state")       # outside the standing-grant shape
        doc = grant()
        (self.grants / f"{GRANT_ID}.json").write_text(json.dumps({**doc, "meta": {**doc["meta"], "id": "urn:grant:x"}}))
        self.assertDenied(self.check(0.1), "unknown-state")       # another grant's content under this name
        self.write_grant(grant(meta={**grant()["meta"], "revision": 2}))
        self.assertDenied(self.check(0.1), "unknown-state")       # the hub has not written the token's revision yet
        self.write_grant(grant(meta={**grant()["meta"], "revision": 4}))
        self.assertDenied(self.check(0.1), "no-grant")            # revised since the token was minted
        self.write_grant(grant())
        self.assertEqual(self.check(0.1)["decision"], "allowed")

    def test_a_malformed_task_claim_is_an_unproven_principal(self):
        self.assertDenied(self.check(0.1, claims(task="has spaces")), "principal-unproven")

    def test_the_credential_source_must_be_the_projects_key_or_a_capability(self):
        # Another agent's key is outside the standing-grant shape itself (DEC-0038): the projection is refused
        # before the source is read. The meter's own credential-source-refused branch stays as a second guard.
        self.write_grant(grant(spend={**grant()["spend"], "credentialSource": {"kind": "agent-key", "ref": "copylot"}}))
        self.assertDenied(self.check(0.1), "unknown-state")

    def test_which_grant(self):
        # Fabric ADR-0144 amendment 3: exactly one grant per token; two refuse the token itself.
        two = claims(grant=[f"{GRANT_ID}#3", "urn:grant:project-a:other#1"])
        self.assertDenied(self.check(0.1, two), "principal-unproven")
        self.assertDenied(self.check(0.1, two, grantRef=f"{GRANT_ID}#3"), "principal-unproven")
        self.assertEqual(self.check(0.1, grantRef=f"{GRANT_ID}#3")["decision"], "allowed")
        self.assertDenied(self.check(0.1, grantRef="urn:grant:project-a:else#1"), "no-grant")

    def test_an_unproven_principal_is_denied_before_anything_is_read(self):
        stranger = Estate()
        answer = self.meter.check(stranger.sign(claims()), request(0.1))
        self.assertDenied(answer, "principal-unproven")
        self.assertDenied(self.meter.check(self.estate.sign(claims(scope="writer.write")), request(0.1)),
                          "principal-unproven")
        self.assertNotIn(".", answer["reason"].split("(")[0], "the reason never carries the token")

    def test_a_malformed_request_is_an_error_not_a_refusal(self):
        token = self.estate.sign(claims())
        for body in ({"amountUsd": -1, "tokenJti": "tok-callee-000000001"}, {"amountUsd": 1, "tokenJti": "short"},
                     {"amountUsd": True, "tokenJti": "tok-callee-000000001"},
                     {"amountUsd": 1, "tokenJti": "tok-callee-000000001", "token": "x"}, []):
            with self.assertRaises(M.RequestError):
                self.meter.check(token, body)

    def test_settlement_counts_what_was_spent_and_a_reservation_lapses(self):
        first = self.check(1.4, claims(task="task-1"))
        self.meter.settle(first["receipt"], 0.2)
        with self.assertRaises(M.RequestError):
            self.meter.settle(first["receipt"], 0.2)
        with self.assertRaises(M.RequestError):
            self.meter.settle("rcpt_unknown", 0.1)
        self.assertEqual(self.check(1.4, claims(task="task-2"))["decision"], "allowed")
        self.assertEqual(self.check(1.4, claims(task="task-3"))["decision"], "allowed")   # 0.2 + 2.8
        self.assertEqual(self.check(1.4, claims(task="task-4"))["decision"], "allowed")   # 4.4
        self.assertDenied(self.check(1.4, claims(task="task-5")), "limit-reached", "L1", "day")
        self.now[0] += 3601                        # unsettled reservations lapse; the settled 0.2 stays
        self.assertEqual(self.check(1.4, claims(task="task-6"))["decision"], "allowed")

    def test_concurrent_checks_cannot_overrun_a_limit(self):
        answers, lock = [], threading.Lock()

        def one(i):
            meter = self.make()                    # one connection each, as separate callers would hold
            answer = meter.check(self.estate.sign(claims(task=f"task-{i}")),
                                 request(0.9, jti=f"tok-callee-{i:09d}"))
            with lock:
                answers.append(answer)
        threads = [threading.Thread(target=one, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        allowed = [a for a in answers if a["decision"] == "allowed"]
        self.assertEqual(len(allowed), 5, "5 x 0.9 fits the 5 USD day, a sixth would not")
        self.assertEqual({(a["code"], a.get("window")) for a in answers if a["decision"] == "denied"},
                         {("limit-reached", "day")}, "the writers queue; none fails on a busy ledger")
        self.assertEqual(self.meter.status()["estateUsd"], 4.5)

    def test_the_keys_are_cached_and_reread_on_an_unknown_kid(self):
        clock = [0.0]
        reads = []
        source = M.KeySource(lambda: reads.append(1) or self.estate.jwks, clock=lambda: clock[0])
        source.get("estate-1"), source.get("estate-1")
        self.assertEqual(len(reads), 1)
        source.get("estate-2")
        self.assertEqual(len(reads), 2, "an unknown kid re-reads")
        source.get("estate-2")
        self.assertEqual(len(reads), 2, "at most once a minute")
        clock[0] = 601
        source.get("estate-1")
        self.assertEqual(len(reads), 3, "the cache lives at most ten minutes")

    def test_the_ledger_is_private_and_holds_no_identity(self):
        self.check(0.5)
        self.assertEqual((self.dir / "meter").stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.dir / "meter" / "meter.db").stat().st_mode & 0o777, 0o600)
        raw = (self.dir / "meter" / "meter.db").read_bytes()
        for word in (b"urn:person:operator", b"example-media", b"example-writer"):
            self.assertNotIn(word, raw)

    def test_the_vendored_schemas_are_the_contracts(self):
        table = (SCHEMAS / "README.md").read_text()
        for path in SCHEMAS.glob("*.schema.json"):
            self.assertIn(f"`{path.name}` | `{hashlib.sha256(path.read_bytes()).hexdigest()}`", table, path.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
