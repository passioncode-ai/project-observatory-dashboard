#!/usr/bin/env python3
"""access-binding/1: who may do what with agent memory, decided from the transport.

PB-137 N-007. The decision is the accepted fixture file `access_binding_cases.json`
beside this suite; this suite proves `access_binding.py` reproduces every case, that a
malformed registry is refused whole, that a caller's own arguments never confer
authority, and that the published JSON Schema agrees with the evaluator. N-008 wires
the evaluator into every business entry point; these cases are its contract.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import access_binding as AB                                                         # noqa: E402

CASES = json.loads((ROOT / "tests/access_binding_cases.json").read_text(encoding="utf-8"))
SCHEMA = json.loads((ROOT / "defaults/access-binding.schema.json").read_text(encoding="utf-8"))
NOW = CASES["now"]


def registry():
    return AB.parse(CASES["registry"])


def binding(name):
    if name is None:
        return None
    if name == AB.STDIO_LOCAL.binding_id:
        return AB.STDIO_LOCAL
    return next(b for b in registry().bindings if b.binding_id == name)


class Resolution(unittest.TestCase):
    def test_every_resolve_case(self):
        reg = registry()
        for case in CASES["resolve"]:
            with self.subTest(case["name"]):
                got = AB.resolve(reg, case["channel"], now=NOW)
                self.assertEqual(got.reason, case["expect"]["reason"])
                self.assertEqual(got.binding.binding_id if got.binding else None,
                                 case["expect"]["binding"])

    def test_no_registry_still_resolves_stdio_and_denies_http(self):
        empty = AB.empty()
        self.assertEqual(AB.resolve(empty, {"channel": "stdio"}, now=NOW).reason, "stdio-local")
        self.assertEqual(AB.resolve(empty, {"channel": "http", "audience": "observatory:default",
                                            "bearerDigest": "1" * 64}, now=NOW).reason, "no-binding")

    def test_a_raw_token_is_never_accepted_in_place_of_a_digest(self):
        got = AB.resolve(registry(), {"channel": "http", "audience": "observatory:default",
                                      "bearerDigest": "a-raw-bearer-token"}, now=NOW)
        self.assertIsNone(got.binding)
        self.assertEqual(got.reason, "no-credential")


class Authorization(unittest.TestCase):
    def test_every_authorize_case(self):
        for case in CASES["authorize"]:
            with self.subTest(case["name"]):
                d = AB.authorize(binding(case["binding"]), case["request"])
                self.assertEqual((d.allow, d.reason),
                                 (case["expect"]["allow"], case["expect"]["reason"]))

    def test_every_visibility_case(self):
        for case in CASES["visibility"]:
            with self.subTest(case["name"]):
                self.assertEqual(AB.may_see(binding(case["binding"]), case["classification"]),
                                 case["expect"])

    def test_every_query_context_case(self):
        for case in CASES["queryContext"]:
            with self.subTest(case["name"]):
                self.assertEqual(AB.query_context(binding(case["binding"]), case["projectId"]),
                                 case["expect"])

    def test_a_denial_is_the_wire_s_typed_error(self):
        d = AB.authorize(binding("bnd_partner01"),
                         {"scope": "memory.read", "projectId": "project:beta", "effect": "read"})
        env = d.envelope()
        self.assertEqual(env["error"], "binding refused")
        self.assertEqual(env["code"], "project-not-bound")
        self.assertIn("bnd_partner01", env["detail"])
        self.assertIn("hint", env)
        self.assertEqual(env["degraded"], [])
        self.assertNotIn("1111", json.dumps(env), "no credential material in a refusal")

    def test_odd_requests_are_denials_never_exceptions(self):
        for request in (None, {}, {"scope": 5}, "memory.read",
                        {"scope": "memory.read", "projectId": 7, "effect": "read"}):
            with self.subTest(request):
                self.assertFalse(AB.authorize(binding("bnd_partner01"), request).allow)


class Parsing(unittest.TestCase):
    def test_every_malformed_registry_is_refused(self):
        for case in CASES["malformed"]:
            with self.subTest(case["name"]):
                with self.assertRaises(AB.BindingError):
                    AB.parse(case["doc"])

    def test_a_missing_file_is_the_empty_registry_and_a_broken_one_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "access-bindings.json"
            self.assertEqual(AB.load(path).bindings, ())
            path.write_text("{oops", encoding="utf-8")
            with self.assertRaises(AB.BindingError):
                AB.load(path)
            path.write_text(json.dumps(CASES["registry"]), encoding="utf-8")
            self.assertEqual(len(AB.load(path).bindings), 5)

    def test_the_digest_helper_never_returns_the_input(self):
        token = "an example bearer that is not real"
        digest = AB.bearer_digest(token)
        self.assertEqual(len(digest), 64)
        self.assertNotIn(token, digest)


class PublishedSchema(unittest.TestCase):
    def setUp(self):
        try:
            import jsonschema                                                          # noqa: F401
        except ImportError:
            self.skipTest("jsonschema is in the [full] extra; the base install cannot run this check")

    def test_the_schema_accepts_the_registry_and_rejects_structural_failures(self):
        import jsonschema
        v = jsonschema.Draft202012Validator(SCHEMA,
                                            format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)
        v.check_schema(SCHEMA)
        self.assertEqual(list(v.iter_errors(CASES["registry"])), [])
        for case in CASES["malformed"]:
            if not case.get("schemaRejects", True):
                continue
            with self.subTest(case["name"]):
                self.assertNotEqual(list(v.iter_errors(case["doc"])), [])


if __name__ == "__main__":
    unittest.main()
