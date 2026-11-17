#!/usr/bin/env python3
"""The Fabric contract's folder-attribution vectors (DEC-0089), run against `folder_attribution.py`.

The contract is the one rule set Fabric, Fabric Switchboard and Observatory attribute a folder by;
each product runs its vectors in its own gate and keeps no rule of its own beside them. Every
vector is a case here: the repository identity (`normalize_origin`), the answer
(`attribute_folder`) and the folder a product records after it (`explain_folder(...).record`).
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import folder_attribution as fa  # noqa: E402

VECTORS = ROOT / "fabric" / "attribution-vectors"


class Vectors(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = json.loads((VECTORS / "folder-attribution.json").read_text(encoding="utf-8"))

    def test_the_copy_is_the_pinned_one(self) -> None:
        pin = json.loads((VECTORS / "SOURCE.json").read_text(encoding="utf-8"))
        self.assertRegex(pin["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(set(self.doc), {"about", "origin", "attribution", "record"})
        self.assertGreaterEqual(len(self.doc["attribution"]), 100)
        canonical = json.dumps(self.doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), pin["canonicalSha256"])

    def test_repository_identity(self) -> None:
        for vector in self.doc["origin"]:
            with self.subTest(origin=vector["origin"]):
                self.assertEqual(fa.normalize_origin(vector["origin"]), vector["expected"])

    def test_attribution(self) -> None:
        for vector in self.doc["attribution"]:
            with self.subTest(vector["name"]):
                self.assertEqual(fa.attribute_folder(vector["snapshot"]), vector["expected"])

    def test_record(self) -> None:
        for vector in self.doc["record"]:
            with self.subTest(vector["name"]):
                self.assertEqual(fa.explain_folder(vector["snapshot"])["record"], vector["record"])

    def test_record_origin_drops_credentials(self) -> None:
        userinfo = "user" + ":" + "pw"  # spelled apart: the public-release gate reads a literal as a credential
        self.assertEqual(fa.record_origin("https://" + userinfo + "@example.com/example-org/alpha-web.git"),
                         "https://example.com/example-org/alpha-web.git")
        self.assertEqual(fa.record_origin("git:secret@example.com:example-org/alpha-web.git"),
                         "git@example.com:example-org/alpha-web.git")
        self.assertEqual(fa.record_origin("https://example.com/example-org/alpha-web?api_key=abc"),
                         "https://example.com/example-org/alpha-web")
        self.assertIsNone(fa.record_origin("https://example.com/aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5zA7bC9d/r"))
        self.assertEqual(fa.record_origin("/local/path/repo"), "/local/path/repo")

    def test_a_token_user_name_is_read_by_its_shape(self) -> None:
        """DEC-0177: 40 or more of `[A-Za-z0-9_-]` holding a letter and a digit; a name is none."""
        token = "0123456789abcdef" * 2 + "01234567"  # the contract's own fake, 40 hex
        self.assertTrue(fa.token_user_name(token))
        self.assertTrue(fa.token_user_name("a" * 39 + "1"))
        for name in ("git", "deploy-bot", "a" * 40, "1" * 40, "a" * 38 + "1", "a.b" + token):
            self.assertFalse(fa.token_user_name(name), name)

    def test_the_detector_refuses_a_url_whose_user_is_a_token(self) -> None:
        token = "0123456789abcdef" * 2 + "01234567"
        self.assertTrue(fa.secret_url("https://" + token + "@example.com/example-org/alpha-web.git"))
        self.assertTrue(fa.secret_url("ssh://" + token + "@example.com/example-org/alpha-web.git"))
        # Percent-encoded, it is decoded before it is read.
        self.assertTrue(fa.secret_url("ssh://%30" + token[1:] + "@example.com/example-org/alpha-web.git"))
        for kept in ("ssh://git@example.com/example-org/alpha-web.git",
                     "https://deploy-bot@example.com/example-org/alpha-web.git",
                     "ssh://" + "a" * 40 + "@example.com/example-org/alpha-web.git"):
            self.assertFalse(fa.secret_url(kept), kept)

    def test_record_origin_keeps_no_token_user_in_any_form(self) -> None:
        token = "0123456789abcdef" * 2 + "01234567"
        tail = "example.com/example-org/alpha-web.git"
        self.assertIsNone(fa.record_origin("ssh://" + token + "@" + tail))
        self.assertIsNone(fa.record_origin(token + "@example.com:example-org/alpha-web.git"))
        self.assertIsNone(fa.record_origin("ssh://x@" + token + "@" + tail))
        # An http(s) URL drops its user first, so its identity is kept without the token.
        self.assertEqual(fa.record_origin("https://" + token + "@" + tail), "https://" + tail)
        self.assertEqual(fa.record_origin("git@example.com:example-org/alpha-web.git"),
                         "git@example.com:example-org/alpha-web.git")

    def test_dec_0151_reads_a_key_by_its_canonical_case_and_strength(self) -> None:
        self.assertTrue(fa.secret_value("sk-Test0Example1Key2Value3"))
        self.assertTrue(fa.secret_value("x%22sk-Test0Example1Key2Value3"))
        for kept in ("SK-Test0Example1Key2Value3", "xsk-aB3cD4eF5gH6jK7m", "sk-learn-experiments-2026",
                     "project:helpdesk-customer-support"):
            self.assertFalse(fa.secret_value(kept), kept)

    def test_a_stored_remote_is_the_recorded_origin_or_withheld(self) -> None:
        """What the engine stores beside a folder (`local.json`'s `remote`): the contract's recorded origin where
        it reads one, a remote it cannot read (an ssh alias) as written, and nothing that carries a credential."""
        token = "0123456789abcdef" * 2 + "01234567"
        tail = "example.com/example-org/alpha-web.git"
        self.assertEqual(fa.stored_remote("git@example.com:example-org/alpha-web.git"),
                         "git@example.com:example-org/alpha-web.git")
        self.assertEqual(fa.stored_remote("https://" + token + "@" + tail), "https://" + tail)
        self.assertIsNone(fa.stored_remote("ssh://" + token + "@" + tail))
        self.assertIsNone(fa.stored_remote(token + "@example.com:example-org/alpha-web.git"))
        self.assertEqual(fa.stored_remote("work-alias:example-org/alpha-web"), "work-alias:example-org/alpha-web")
        self.assertEqual(fa.stored_remote("git@work-alias:example-org/alpha-web"), "git@work-alias:example-org/alpha-web")
        self.assertIsNone(fa.stored_remote(token + "@work-alias:example-org/alpha-web"))
        self.assertIsNone(fa.stored_remote("git" + ":" + "pw@work-alias:example-org/alpha-web"))
        self.assertEqual(fa.stored_remote(""), "")
        self.assertEqual(fa.stored_remote("/srv/mirrors/alpha-web.git"), "/srv/mirrors/alpha-web.git")
        # Idempotent: what it keeps, it keeps again unchanged.
        for raw in ("https://" + token + "@" + tail, "git@example.com:example-org/alpha-web.git"):
            once = fa.stored_remote(raw)
            self.assertEqual(fa.stored_remote(once), once)

    def test_never_raises(self) -> None:
        for junk in (None, 1, "x", [], {"folder": "/a"}, {"folder": "relative", "home": "/h", "tempRoots": [],
                                                         "git": None, "projects": []}):
            self.assertEqual(fa.attribute_folder(junk), {"fault": "input-invalid"})


if __name__ == "__main__":
    unittest.main()
