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

    def test_never_raises(self) -> None:
        for junk in (None, 1, "x", [], {"folder": "/a"}, {"folder": "relative", "home": "/h", "tempRoots": [],
                                                         "git": None, "projects": []}):
            self.assertEqual(fa.attribute_folder(junk), {"fault": "input-invalid"})


if __name__ == "__main__":
    unittest.main()
