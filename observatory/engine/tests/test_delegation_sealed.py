#!/usr/bin/env python3
"""DEC-0045, "Delegation sealed", in the engine's verifier (OBS-162).

A port of `passioncode-ai/fabric-agent-contract` `test/delegation-sealed.test.ts` at commit
`07042181bfae2a481aa0da1a5651f24c76669c48`, over the contract's own fixtures (vendored in
`fabric/delegation-vectors/`). Each block names the contract's inventory id it closes. The verifier cases are data:
a table of claims overrides and the `code/detail` the contract's verifier answers for them, so a row that drifts from
the contract is one line to compare. The meter's side (a required issuer, exact scope and effect, a fail-closed
`Meter.check` with no issuer configured) and the shared small-order key check close OBS-162's 18-case matrix.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
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
import release_signature                               # noqa: E402
import ed25519_order as ed25519                        # noqa: E402

VECTORS = ROOT / "fabric" / "delegation-vectors"
ISSUER = "urn:fabric:estate:example"


def load(name: str) -> dict:
    return json.loads((VECTORS / name).read_text(encoding="utf-8"))


CLAIMS = load("delegation-token-claims.json")
IAT, EXP = CLAIMS["iat"], CLAIMS["exp"]
NOW = IAT + 30
METER_MINT = load("delegation-mint-meter-ok.json")
METER_CLAIMS = METER_MINT["token"]
METER_NOW = METER_CLAIMS["iat"] + 5
DEEP_ACT = load("delegation-token-chain-too-deep.json")["act"]

#: The seven libsodium `has_small_order` encodings, as `src/ed25519.ts` lists them (DEC-0045 item 10).
SMALL_ORDER_HEX = (
    "0000000000000000000000000000000000000000000000000000000000000000",
    "0100000000000000000000000000000000000000000000000000000000000000",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
)


class Estate:
    """The contract test's issuer: one key, published as `estate-new`, signing any header and payload."""

    def __init__(self, kid: str = "estate-new"):
        self.key = Ed25519PrivateKey.generate()
        self.kid = kid
        raw = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.jwk = {"kty": "OKP", "crv": "Ed25519", "x": D.b64url_encode(raw), "kid": kid, "use": "sig",
                    "alg": "EdDSA"}

    def jwks(self, **extra) -> dict:
        return {"keys": [{**self.jwk, **extra}]}

    def sign(self, payload: dict, header: dict | None = None, typ: str = D.DELEGATION_TYP) -> str:
        head = D.b64url_encode(json.dumps({"alg": "EdDSA", "typ": typ, "kid": self.kid, **(header or {})}).encode())
        body = D.b64url_encode(json.dumps(payload).encode())
        return f"{head}.{body}.{D.b64url_encode(self.key.sign(f'{head}.{body}'.encode()))}"


def outcome(result) -> str:
    return "ok" if result.ok else f"{result.code}/{result.detail}"


class Vectors(unittest.TestCase):
    def test_the_vendored_vectors_are_the_contracts(self):
        rows = [line.split("|") for line in (VECTORS / "README.md").read_text(encoding="utf-8").splitlines()
                if line.startswith("| `")]
        digests = {r[1].strip().strip("`"): r[2].strip().strip("`") for r in rows}
        self.assertEqual(set(digests), {p.name for p in VECTORS.glob("*.json")})
        for name, digest in digests.items():
            self.assertEqual(hashlib.sha256((VECTORS / name).read_bytes()).hexdigest(), digest, name)


class SmallOrder(unittest.TestCase):
    """CT-29, DEC-0045 item 10: one predicate refuses a small-order key before anything is verified under it."""

    def test_the_shared_list_is_the_contracts(self):
        self.assertEqual(tuple(ed25519.SMALL_ORDER_ENCODINGS), SMALL_ORDER_HEX)
        for encoding in SMALL_ORDER_HEX:
            raw = bytes.fromhex(encoding)
            self.assertTrue(ed25519.is_small_order_key(raw), encoding)
            self.assertTrue(ed25519.is_small_order_key(raw[:31] + bytes([raw[31] | 0x80])), "sign bit set")

    @unittest.skipIf(Ed25519PrivateKey is None, "needs the cryptography package (the engine's full extra)")
    def test_the_forged_vector_is_refused_as_the_issuers_unusable_key(self):
        vector = load("delegation-small-order.json")
        result = D.verify_delegation(vector["token"], audience=vector["audience"], jwks=vector["jwks"],
                                     now=vector["now"], capability=vector["capability"], effect=vector["effect"],
                                     issuer=vector["issuer"])
        expected = vector["expected"]
        self.assertEqual(outcome(result), f"{expected['code']}/{expected['detail']}")

    @unittest.skipIf(Ed25519PrivateKey is None, "needs the cryptography package (the engine's full extra)")
    def test_every_small_order_encoding_is_refused_by_both_verifiers(self):
        estate = Estate()
        for encoding in SMALL_ORDER_HEX:
            for raw in (bytes.fromhex(encoding), bytes.fromhex(encoding)[:31] + bytes([bytes.fromhex(encoding)[31] | 0x80])):
                jwks = {"keys": [{**estate.jwk, "x": D.b64url_encode(raw)}]}
                self.assertEqual(outcome(D.verify_delegation(
                    estate.sign(CLAIMS), audience="example-writer", jwks=jwks, now=NOW, capability="writer.write",
                    effect="draft", issuer=ISSUER)), "issuer-unavailable/small-order-key", raw.hex())
                meter = D.verify_meter_token(estate.sign(METER_CLAIMS), meter="example-meter",
                                             capability="grant.check", jwks=jwks, now=METER_NOW, issuer=ISSUER)
                self.assertEqual(outcome(meter), "principal-unproven/issuer-unavailable:small-order-key")

    def test_the_check_loads_where_tools_node_shadows_the_node_package(self):
        """The keyserver and the HTTP doors start from `tools/`, whose `node.py` hides the `node` package: the
        verifier must still import (a `from node.ed25519 import` answered every MCP call with a 500 there)."""
        import subprocess
        code = "import delegation, release_signature, meter; print(delegation.is_small_order_key(bytes(32)))"
        env = {"PYTHONPATH": f"{ROOT / 'tools'}:{ROOT}", "PATH": "/usr/bin:/bin"}
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=ROOT / "tools")
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "True"), out.stderr[-400:])

    def test_a_release_signature_is_never_checked_under_a_small_order_key(self):
        """`release_signature` verifies a pinned key; it asks the same predicate first (one check, every use)."""
        forged_r = bytes.fromhex("5866666666666666666666666666666666666666666666666666666666666666")  # B
        forged = forged_r + (1).to_bytes(32, "little")                                           # R = B, S = 1
        for encoding in SMALL_ORDER_HEX:
            self.assertFalse(release_signature._check(bytes.fromhex(encoding), forged, b"m"), encoding)
            self.assertFalse(release_signature._ed25519_verify(bytes.fromhex(encoding), forged, b"m"), encoding)


@unittest.skipIf(Ed25519PrivateKey is None, "needs the cryptography package (the engine's full extra)")
class Sealed(unittest.TestCase):
    def setUp(self):
        self.estate = Estate()

    def call(self, token, **over):
        options = {"audience": "example-writer", "jwks": self.estate.jwks(), "now": NOW, "capability": "writer.write",
                   "effect": "draft", "issuer": ISSUER, **over}
        return outcome(D.verify_delegation(token, **options))

    def meter(self, token, **over):
        options = {"meter": "example-meter", "capability": "grant.check", "jwks": self.estate.jwks(),
                   "now": METER_NOW, "issuer": ISSUER, **over}
        return outcome(D.verify_meter_token(token, **options))

    def token(self, **over):
        return self.estate.sign({**CLAIMS, **over})

    # F-B-305 ------------------------------------------------------------------------------------------------------
    def test_F_B_305_two_grants_are_a_claim_of_the_wrong_type(self):
        self.assertEqual(self.call(self.estate.sign(load("delegation-token-two-grants.json"))),
                         "unauthenticated/malformed-claims")

    # F-B-306 ------------------------------------------------------------------------------------------------------
    def test_F_B_306_the_meters_hop_and_only_it_may_hold_five_actors(self):
        five = load("delegation-mint-meter-depth-five.json")
        parent, token = five["parent"], five["token"]
        self.assertEqual(D.MAX_CHAIN_DEPTH, 4)
        self.assertEqual(self.call(self.estate.sign(parent), now=parent["iat"] + 5), "ok")
        accepted = D.verify_meter_token(self.estate.sign(token), meter="example-meter", capability="grant.check",
                                        jwks=self.estate.jwks(), now=METER_NOW, issuer=ISSUER)
        self.assertTrue(accepted.ok, accepted)
        self.assertEqual((accepted.caller, accepted.chain), ("example-writer", (
            "example-writer", "example-media", "example-agent.default", "example-planner", "example-host")))
        other = {**token, "aud": "example-writer", "scope": "writer.write", "effect": ["draft"]}
        self.assertEqual(self.call(self.estate.sign(other), now=METER_NOW), "chain-too-deep/depth-5")
        six = {**token, "act": {"sub": "example-proxy", "act": token["act"]}}
        self.assertEqual(self.meter(self.estate.sign(six)), "principal-unproven/chain-too-deep:depth-6")

    # F-B-307 ------------------------------------------------------------------------------------------------------
    TYPES = (
        ("actor not an agent id", {"act": {"sub": "Not An Agent Id!"}}),
        ("nested actor not an agent id", {"act": {"sub": "example-media", "act": {"sub": "Nope Nope"}}}),
        ("session not an id", {"act": {"sub": "example-media", "session": 42}}),
        ("session with a space", {"act": {"sub": "example-media", "session": "two words"}}),
        ("short jti", {"jti": "x"}),
        ("numeric jti", {"jti": 42}),
        ("project not a uri", {"project": 7}),
        ("spend not an object", {"spend": "lots"}),
        ("spend mode unknown", {"spend": {"mode": "some"}}),
        ("limited spend without remaining", {"spend": {"mode": "limited"}}),
        ("spend window unknown", {"spend": {"mode": "limited", "remaining": [{"window": "year", "remainingUsd": 1}]}}),
        ("spend amount negative", {"spend": {"mode": "limited", "remaining": [{"window": "day", "remainingUsd": -1}]}}),
        ("per-call not a number", {"spend": {"mode": "limited", "remaining": [{"window": "day", "remainingUsd": 1}],
                                             "perCallMaxUsd": "1"}}),
        ("scope double-spaced", {"scope": "writer.write  writer.check"}),
        ("scope upper case", {"scope": "Writer.write"}),
        ("effect twice", {"effect": ["draft", "draft"]}),
        ("iss not a uri", {"iss": "estate"}),
        ("sub not a uri", {"sub": "operator"}),
        ("aud not an agent id", {"aud": "Example Writer"}),
        ("nbf not a time", {"nbf": "soon"}),
    )

    def test_F_B_307_claims_of_the_wrong_type_are_unauthenticated(self):
        for name, overrides in self.TYPES:
            with self.subTest(name):
                self.assertRegex(self.call(self.token(**overrides)), r"^unauthenticated/malformed-(claims|chain)$")
        self.assertEqual(self.call(self.token(act={"sub": "Not An Agent Id!"})), "unauthenticated/malformed-chain")
        without_act = {k: v for k, v in CLAIMS.items() if k != "act"}
        self.assertEqual(self.call(self.estate.sign(without_act)), "unauthenticated/malformed-chain")   # CT-31
        self.assertEqual(self.call(self.token(spend={"mode": "unlimited"}, project="urn:project:b", nbf=IAT)), "ok")

    def test_a_partner_grant_token_is_refused_at_row_5(self):
        """DEC-0090 (OBS-171): a verifier not yet at the partner channel's `verifyDelegation` refuses every token whose
        chain holds a partner actor at DL-13 step 5, innermost or outermost, for a callee and for the meter. The
        engine takes that option until it ports the partner actor (OBS-171's open part), so no partner-grant token is
        read as an ordinary chain."""
        partner = {"partner": "org:acme_partners"}
        for act in ({"sub": "example-media", "act": {"sub": "acme-setup.default", "act": partner}},
                    {"sub": "example-media", "act": partner}, partner):
            with self.subTest(act=act):
                self.assertEqual(self.call(self.token(act=act)), "unauthenticated/malformed-chain")
                self.assertEqual(self.meter(self.estate.sign({**load("delegation-mint-meter-ok.json")["token"],
                                                              "act": {"sub": "example-meter", "act": act}})),
                                 "principal-unproven/unauthenticated:malformed-chain")

    def test_F_B_307_the_first_row_that_applies_answers(self):
        late = EXP + 61
        order = (
            ("unauthenticated/malformed-chain", {"act": {"sub": "x"}, "aud": "example-reader"}, {}),
            ("unauthenticated/malformed-chain", {"act": {"sub": "x"}}, {"now": late}),
            ("unauthenticated/malformed-claims", {"jti": "x"}, {"now": late}),
            ("unauthenticated/wrong-issuer", {"iss": "urn:fabric:estate:other"}, {"now": late}),
            ("unauthenticated/lifetime-above-profile", {"exp": IAT + 601, "aud": "example-reader"}, {}),
            ("token-expired/expired", {"aud": "example-reader"}, {"now": late}),
            ("wrong-audience/audience-mismatch", {"act": DEEP_ACT}, {"audience": "example-reader"}),
            ("chain-too-deep/depth-5", {"act": DEEP_ACT}, {"capability": "writer.send"}),
            ("scope-not-granted/capability-not-in-scope", {}, {"capability": "writer.send", "effect": "send"}),
        )
        for expected, overrides, options in order:
            with self.subTest(expected, overrides=overrides):
                self.assertEqual(self.call(self.token(**overrides), **options), expected)

    # F-B-313 ------------------------------------------------------------------------------------------------------
    def test_F_B_313_the_issuer_is_required(self):
        for missing in (None, ""):
            self.assertEqual(self.call(self.token(), issuer=missing), "issuer-unavailable/no-trusted-issuer")
            self.assertEqual(self.meter(self.estate.sign(METER_CLAIMS), issuer=missing),
                             "principal-unproven/issuer-unavailable:no-trusted-issuer")
        with self.assertRaises(TypeError):
            D.verify_delegation(self.token(), audience="example-writer", jwks=self.estate.jwks(), now=NOW,
                                capability="writer.write", effect="draft")
        self.assertEqual(self.meter(self.estate.sign({**METER_CLAIMS, "iss": "urn:fabric:estate:other"})),
                         "principal-unproven/unauthenticated:wrong-issuer")
        self.assertEqual(self.meter(self.estate.sign(METER_CLAIMS), issuer="urn:fabric:estate:other"),
                         "principal-unproven/unauthenticated:wrong-issuer")

    # F-B-312 ------------------------------------------------------------------------------------------------------
    def test_F_B_312_the_meter_reads_one_capability_and_effect_none(self):
        self.assertEqual(self.meter(self.estate.sign(METER_CLAIMS)), "ok")
        for effect in (["send", "charge"], ["none", "draft"]):
            self.assertEqual(self.meter(self.estate.sign({**METER_CLAIMS, "effect": effect})),
                             "principal-unproven/effect-above-ceiling:meter-effect-not-none", effect)
        self.assertEqual(self.meter(self.estate.sign({**METER_CLAIMS, "scope": "grant.check writer.write"})),
                         "principal-unproven/scope-not-granted:meter-scope-not-exact")

    # F-B-319 ------------------------------------------------------------------------------------------------------
    def test_F_B_319_a_key_of_another_use_or_algorithm_is_the_issuers(self):
        for extra in ({"use": "enc"}, {"alg": "ES256"}):
            self.assertEqual(self.call(self.token(), jwks=self.estate.jwks(**extra)),
                             "issuer-unavailable/key-not-for-signing", extra)

    # F-B-309 ------------------------------------------------------------------------------------------------------
    def test_F_B_309_every_security_must_has_a_failing_case(self):
        for member in ("crit", "x5u", "x5c"):
            value = ["exp"] if member == "crit" else "https://attacker.example/x"
            self.assertEqual(self.call(self.estate.sign(CLAIMS, {member: value})), f"unauthenticated/header-{member}")
        self.assertEqual(self.call(self.token(), jwks=self.estate.jwks(d="private")), "issuer-unavailable/unusable-key")
        h, p, s = self.token().split(".")
        longer = D.b64url_encode(D.b64url_decode(s) + b"\x00")
        self.assertEqual(self.call(f"{h}.{p}.{longer}"), "unauthenticated/bad-signature")
        self.assertEqual(self.call(Estate().sign(CLAIMS)), "unauthenticated/bad-signature")
        approval = self.estate.sign({"iss": ISSUER}, typ=D.APPROVAL_TYP)
        self.assertIsNone(D.read_delegation_token({"Authorization": f"Bearer {approval}"}))
        self.assertIsNone(D.read_delegation_token({"Authorization": f"Bearer {self.estate.sign(CLAIMS, {'typ': 'JWT'})}"}))

    # round 1 of review: Python's `$` matches before a final newline; the contract's `.test` does not ----------------
    def test_a_trailing_newline_is_never_part_of_a_grammar(self):
        cases = (
            ("jti", {"jti": CLAIMS["jti"] + "\n"}, "unauthenticated/malformed-claims"),
            ("sub", {"sub": "urn:person:operator\n"}, "unauthenticated/malformed-claims"),
            ("iss", {"iss": ISSUER + "\n"}, "unauthenticated/malformed-claims"),
            ("task", {"task": "task-1\n"}, "unauthenticated/malformed-claims"),
            ("project", {"project": "urn:project:a\n"}, "unauthenticated/malformed-claims"),
            ("act.sub", {"act": {"sub": "example-media\n"}}, "unauthenticated/malformed-chain"),
            ("act.session", {"act": {"sub": "example-media", "session": "run-1\n"}}, "unauthenticated/malformed-chain"),
            ("scope", {"scope": "writer.write writer.check zz.extra\n"}, "unauthenticated/malformed-claims"),
            ("aud", {"aud": "example-writer\n"}, "unauthenticated/malformed-claims"),
        )
        for name, overrides, expected in cases:
            with self.subTest(name):
                self.assertEqual(self.call(self.token(**overrides)), expected)
        self.assertEqual(self.meter(self.estate.sign({**METER_CLAIMS, "scope": "grant.check\n"})),
                         "principal-unproven/unauthenticated:malformed-claims")
        token = self.token()
        self.assertIsNone(D.read_delegation_token({"Authorization": f"Bearer {token}\n"}))

    def test_non_json_numbers_a_bad_signature_segment_and_integer_valued_times(self):
        head = D.b64url_encode(json.dumps({"alg": "EdDSA", "typ": D.DELEGATION_TYP, "kid": "estate-new"}).encode())
        for constant in ("NaN", "Infinity", "-Infinity"):
            body = json.dumps(CLAIMS)[:-1] + f', "exp": {constant}}}'
            payload = D.b64url_encode(body.replace(f'"exp": {EXP}, ', "").encode())
            signing = f"{head}.{payload}"
            token = f"{signing}.{D.b64url_encode(self.estate.key.sign(signing.encode()))}"
            self.assertEqual(self.call(token), "unauthenticated/malformed", constant)
        h, p, _s = self.token().split(".")
        self.assertEqual(self.call(f"{h}.{p}.A"), "unauthenticated/bad-signature")
        self.assertEqual(self.call(self.token(iat=float(IAT), exp=float(EXP))), "ok")
        self.assertEqual(self.call(self.token(exp=EXP + 0.5)), "unauthenticated/malformed-claims")

    # round 2 of review: white space and digits as JavaScript reads them --------------------------------------------
    def test_white_space_and_digits_are_javascripts(self):
        # U+FEFF is white space only in JavaScript: the contract refuses a URI holding it, so the engine does.
        for claim in ("sub", "iss", "project"):
            self.assertEqual(self.call(self.token(**{claim: "urn:person:op\ufefferator"})),
                             "unauthenticated/malformed-claims", claim)
        # U+001F and U+0085 are white space only in Python: the contract accepts them inside a URI, so the engine does.
        for char in ("\x1f", "\x85"):
            self.assertEqual(self.call(self.token(sub=f"urn:person:op{char}erator")), "ok", repr(char))
        token = self.token()
        self.assertIsNone(D.read_delegation_token({"Authorization": f"Bearer {token}\ufeff"}))
        self.assertEqual(D.read_delegation_token({"Authorization": f"Bearer\u3000{token}"}), (token, "authorization"))
        # A digit of another script is not a digit to the contract's regular expressions.
        from node import relay
        self.assertTrue(relay.is_relay_origin("https://relay.example:4430"))
        self.assertFalse(relay.is_relay_origin("https://relay.example:\u0664\u0664\u0663\u0660"))
        self.assertIsNone(M.parse_time("\u0662\u0660\u0662\u0666-10-19T12:00:00Z"))

    def test_an_integer_past_pythons_digit_limit_is_a_claim_of_the_wrong_type(self):
        body = json.dumps({k: v for k, v in CLAIMS.items() if k != "exp"})[:-1] + ', "exp": 1' + "0" * 5000 + "}"
        head = D.b64url_encode(json.dumps({"alg": "EdDSA", "typ": D.DELEGATION_TYP, "kid": "estate-new"}).encode())
        signing = f"{head}.{D.b64url_encode(body.encode())}"
        token = f"{signing}.{D.b64url_encode(self.estate.key.sign(signing.encode()))}"
        self.assertEqual(self.call(token), "unauthenticated/malformed-claims")

    def test_an_integer_a_double_cannot_hold_is_an_infinity_as_in_javascript(self):
        """Review round 3: `JSON.parse` rounds 2**1024 - 2**970 and above to Infinity, one less to the largest double."""
        boundary = 2 ** 1024 - 2 ** 970
        def remaining(amount):
            return self.token(spend={"mode": "limited", "remaining": [{"window": "day", "remainingUsd": amount}]})
        for amount, expected in ((10 ** 308, "ok"), (2 * 10 ** 308, "unauthenticated/malformed-claims"),
                                 (10 ** 399, "unauthenticated/malformed-claims"),
                                 (boundary - 1, "ok"), (boundary, "unauthenticated/malformed-claims")):
            with self.subTest(digits=len(str(amount)), amount=str(amount)[:6]):
                self.assertEqual(self.call(remaining(amount)), expected)
        self.assertEqual(self.call(self.token(exp=10 ** 399)), "unauthenticated/malformed-claims")

    def test_a_published_x_with_a_trailing_newline_fails_closed(self):
        """Node's base64url decoder skips the newline; the engine refuses the key as unusable (the issuer's fault),
        which never accepts what the contract refuses. Recorded in docs/design/METER.md."""
        self.assertEqual(self.call(self.token(), jwks=self.estate.jwks(x=self.estate.jwk["x"] + "\n")),
                         "issuer-unavailable/unusable-key")

    # the ratchet ---------------------------------------------------------------------------------------------------
    def test_malformed_input_is_refused_and_escapes_are_read(self):
        h, p, s = self.token().split(".")
        self.assertEqual(self.call(f"{D.b64url_encode(b'[1]')}.{p}.{s}"), "unauthenticated/malformed")
        self.assertEqual(self.call(f"{h}.{p}.%%%"), "unauthenticated/malformed")
        self.assertEqual(self.call("a" * 8193), "unauthenticated/malformed")
        self.assertEqual(self.call(42), "unauthenticated/malformed")
        self.assertEqual(self.call(self.token(note='a "quoted" \\ word')), "ok")
        self.assertEqual(D.chain_depth({"sub": 1}), float("inf"))
        self.assertEqual(sorted(D.effect_closure([])), [])


@unittest.skipIf(Ed25519PrivateKey is None, "needs the cryptography package (the engine's full extra)")
class MeterIssuer(unittest.TestCase):
    """CT-20: the meter passes its issuer; with none configured it fails closed and says why."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.estate = Estate()
        (self.dir / "jwks.json").write_text(json.dumps(self.estate.jwks()))
        (self.dir / "grants").mkdir(mode=0o700)

    def make(self, **config) -> M.Meter:
        base = {"audience": "example-meter", "grantsDir": str(self.dir / "grants")}
        return M.Meter(db_path=self.dir / "meter.db", config={**base, **config}, clock=lambda: METER_NOW)

    def check(self, meter: M.Meter) -> dict:
        return meter.check(self.estate.sign(METER_CLAIMS), {"amountUsd": 0.1, "tokenJti": "tok-callee-000000001"})

    def test_no_issuer_configured_fails_closed_with_a_named_reason(self):
        meter = self.make(jwks=str(self.dir / "jwks.json"))
        self.assertIsNone(meter.issuer)
        answer = self.check(meter)
        self.assertEqual((answer["decision"], answer["code"]), ("denied", "principal-unproven"))
        self.assertIn("no-trusted-issuer", answer["reason"])
        self.assertIn("meter:no-trusted-issuer", meter.readiness()["degraded"])

    def test_the_issuer_comes_with_the_hubs_keys_and_never_alone(self):
        hub = self.dir / "hub.json"
        hub.write_text(json.dumps({"issuer": ISSUER, "jwks": self.estate.jwks(), "grantsDir": str(self.dir / "grants")}))
        from_hub = self.make(hubJson=str(hub))
        self.assertEqual((from_hub.issuer, from_hub.readiness()["issuer"]), (ISSUER, "hub"))
        self.assertNotIn("no-trusted-issuer", self.check(from_hub).get("reason", ""))
        # Keys from another file than the hub's: the hub's issuer does not vouch for them.
        mixed = self.make(hubJson=str(hub), jwks=str(self.dir / "jwks.json"))
        self.assertIsNone(mixed.issuer)
        self.assertIn("no-trusted-issuer", self.check(mixed)["reason"])
        configured = self.make(hubJson=str(hub), issuer="urn:fabric:estate:other")
        self.assertEqual((configured.issuer, configured.readiness()["issuer"]), ("urn:fabric:estate:other", "config"))

    def test_the_slot_door_needs_the_issuer_too(self):
        meter = self.make(jwks=str(self.dir / "jwks.json"))
        slot = self.estate.sign({**METER_CLAIMS, "scope": "secret.use"})
        answer = meter.authorize_slot(slot, "project-a/production/OPENROUTER_API_KEY")
        self.assertEqual(answer["code"], "principal-unproven")
        self.assertIn("no-trusted-issuer", answer["reason"])


if __name__ == "__main__":
    unittest.main()
