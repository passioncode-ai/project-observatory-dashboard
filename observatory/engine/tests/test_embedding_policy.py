#!/usr/bin/env python3
"""embedding-policy/1: which memory text may leave the machine for an embedding model.

PB-137 N-002. The decision is the accepted fixture file `embedding_policy_cases.json`
beside this suite; this suite proves `embedding_policy.py` reproduces every case,
that every malformed policy is refused rather than half-read, and that the published
JSON Schema and the evaluator agree on what a valid policy is. N-003 wires the
evaluator into indexing, the query path and the worker; these cases are its contract.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import embedding_policy as EP                                                       # noqa: E402

CASES = json.loads((ROOT / "tests/embedding_policy_cases.json").read_text(encoding="utf-8"))
SCHEMA = json.loads((ROOT / "defaults/embedding-policy.schema.json").read_text(encoding="utf-8"))


def _policy(name):
    return EP.parse(CASES["policies"][name]) if name else EP.empty()


class Decisions(unittest.TestCase):
    def test_every_record_case(self):
        for case in CASES["records"]:
            with self.subTest(case["name"]):
                v = EP.for_record(_policy(case["policy"]), case["record"],
                                  configured=case.get("configured", CASES["configured"]))
                self.assertEqual((v.route, v.reason),
                                 (case["expect"]["route"], case["expect"]["reason"]))

    def test_every_query_case(self):
        for case in CASES["queries"]:
            with self.subTest(case["name"]):
                v = EP.for_query(_policy(case["policy"]), case["context"],
                                 configured=case.get("configured", CASES["configured"]))
                self.assertEqual((v.route, v.reason),
                                 (case["expect"]["route"], case["expect"]["reason"]))

    def test_a_remote_verdict_names_what_it_was_decided_under(self):
        v = EP.for_record(_policy("consented"), CASES["records"][0]["record"],
                          configured=CASES["configured"])
        self.assertTrue(v.remote)
        self.assertEqual((v.provider, v.model, v.policy_revision),
                         ("openai", "text-embedding-3-small", 3))
        local = EP.for_record(EP.empty(), CASES["records"][0]["record"],
                              configured=CASES["configured"])
        self.assertFalse(local.remote)
        self.assertIsNone(local.provider)

    def test_odd_input_is_a_local_verdict_never_an_exception(self):
        policy = _policy("consented")
        for record in ({}, {"classification": 7}, {"project_id": 5, "classification": "public"},
                       {"project_id": "project:alpha", "classification": ["public"],
                        "scope": "project"}, "not a mapping"):
            with self.subTest(record):
                v = EP.for_record(policy, record, configured=CASES["configured"])
                self.assertEqual(v.route, "local")
        self.assertEqual(EP.for_query(policy, None, configured=CASES["configured"]).route, "local")
        self.assertEqual(EP.for_record(policy, CASES["records"][0]["record"], configured=None).route,
                         "local")


class Parsing(unittest.TestCase):
    def test_every_malformed_policy_is_refused(self):
        for case in CASES["malformed"]:
            with self.subTest(case["name"]):
                with self.assertRaises(EP.PolicyError):
                    EP.parse(case["doc"])

    def test_a_missing_file_is_the_empty_policy_and_a_broken_one_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "embedding-policy.json"
            empty = EP.load(path)
            self.assertEqual(empty.projects, {})
            self.assertEqual(empty.revision, 0)
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(EP.PolicyError):
                EP.load(path)
            path.write_text(json.dumps(CASES["policies"]["consented"]), encoding="utf-8")
            self.assertEqual(EP.load(path).revision, 3)

    def test_aliases_from_the_plan_map_to_the_stored_classes(self):
        self.assertEqual(EP.canonical_classification("private"), "confidential")
        self.assertEqual(EP.canonical_classification("secret-adjacent"), "confidential")
        self.assertEqual(EP.canonical_classification("internal"), "project-internal")
        self.assertIsNone(EP.canonical_classification("restricted"))
        self.assertIsNone(EP.canonical_classification(None))


class PublishedSchema(unittest.TestCase):
    """The schema is what another implementation reads; the evaluator is what runs.
    They must agree on every structural case, or one of them is lying."""

    def setUp(self):
        try:
            import jsonschema                                                          # noqa: F401
        except ImportError:
            self.skipTest("jsonschema is in the [full] extra; the base install cannot run this check")

    def test_the_schema_accepts_the_accepted_policy_and_rejects_the_structural_failures(self):
        import jsonschema
        validator = jsonschema.Draft202012Validator(
            SCHEMA, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)
        validator.check_schema(SCHEMA)
        self.assertEqual(list(validator.iter_errors(CASES["policies"]["consented"])), [])
        for case in CASES["malformed"]:
            if not case["schemaRejects"]:
                continue
            with self.subTest(case["name"]):
                self.assertNotEqual(list(validator.iter_errors(case["doc"])), [])


if __name__ == "__main__":
    unittest.main()
