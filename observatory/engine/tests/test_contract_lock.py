#!/usr/bin/env python3
"""The engine's two lock files, each under its own name (OBS-196).

`fabric-contract.lock.json` is the name the Fabric Agent Contract gives a
consumer's ONE pin of the contract (`schemas/contract-pin.schema.json`:
`contract`, `version`, `repository`, the full 40-character `commit`, and an
optional `profile`). Until OBS-196 the engine kept its own local-profile lock
under that name (`profile`, `version`, `schemaRelease`, `schemas`, `releases`),
so every reader keyed on the name took the profile lock for the pin, and the
contract's `pnpm pin:check` on `observatory/engine` exited 2: not a pin.

The profile lock is now `observatory-profile.lock.json`, the pin sits beside it
under the contract's name, and `contract_lock.py` is the one module that reads
either. These cases hold the shipped files to the pin schema, hold every reader
to that module, and watch the legacy fallback (the profile lock under its old
name, kept readable for one release) take only a file of the profile's shape.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import contract_lock  # noqa: E402

PROFILE = {"profile": "observatory-local-mcp", "version": "1.2.0", "schemaRelease": "v0.2.0",
           "schemas": [], "releases": []}
PIN = {"contract": "fabric-agent-contract", "version": "0.1.0",
       "repository": "https://github.com/passioncode-ai/fabric-agent-contract",
       "commit": "a5a27092ba0dcc5facfbeae8b359146dfb403e9a"}


def write(folder: pathlib.Path, name: str, doc: object) -> None:
    (folder / name).write_text(json.dumps(doc), encoding="utf-8")


class TheShippedFiles(unittest.TestCase):
    def test_the_profile_lock_has_its_own_name(self) -> None:
        path = ROOT / contract_lock.PROFILE_LOCK
        self.assertTrue(path.is_file(), path)
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc["profile"], "observatory-local-mcp")
        self.assertIn("releases", doc)
        self.assertNotIn("contract", doc, "the profile lock is not a pin")

    def test_the_pin_sits_beside_it_under_the_contracts_name(self) -> None:
        self.assertEqual(contract_lock.CONTRACT_PIN, "fabric-contract.lock.json")
        path = ROOT / contract_lock.CONTRACT_PIN
        self.assertTrue(path.is_file(), path)
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(contract_lock.pin_problems(doc), [])
        self.assertEqual(contract_lock.contract_pin(ROOT), doc)

    def test_the_pin_carries_no_member_the_schema_does_not_define(self) -> None:
        doc = json.loads((ROOT / contract_lock.CONTRACT_PIN).read_text(encoding="utf-8"))
        self.assertLessEqual(set(doc), set(contract_lock.PIN_MEMBERS))

    def test_the_pin_names_the_contract_version_the_manifest_declares(self) -> None:
        manifest = json.loads((ROOT / "fabric-agent.json").read_text(encoding="utf-8"))
        self.assertEqual(contract_lock.contract_pin(ROOT)["version"], manifest["contractVersion"])

    def test_the_pin_is_the_commit_the_node_vectors_were_last_vendored_at(self) -> None:
        """One pin per consumer: moving the vendored files moves the pin in the same
        change. `tools/vendor_org_vectors.py` rewrites both; this holds them equal."""
        readme = (ROOT / "fabric/node-vectors/README.md").read_text(encoding="utf-8")
        named = re.search(r"\(OBS-161 pin\)[^`]*`([0-9a-f]{40})`", readme)
        self.assertIsNotNone(named, "the node vectors README no longer names the OBS-161 commit")
        self.assertEqual(contract_lock.contract_pin(ROOT)["commit"], named.group(1))

    def test_the_profile_lock_reads_from_the_new_name(self) -> None:
        lock = contract_lock.profile_lock(ROOT)
        self.assertEqual(lock["profile"], "observatory-local-mcp")


class ThePinSchema(unittest.TestCase):
    """`contract-pin.schema.json`, checked in Python because the engine runs no pnpm.
    Each rule watched refusing, the contract's own negative fixture first."""

    def refused(self, doc: object, fragment: str) -> None:
        problems = contract_lock.pin_problems(doc)
        self.assertTrue(any(fragment in p for p in problems), problems)

    def test_the_contracts_positive_fixture_passes(self) -> None:
        self.assertEqual(contract_lock.pin_problems(PIN), [])
        self.assertEqual(contract_lock.pin_problems({**PIN, "profile": "mcp"}), [])

    def test_an_abbreviated_commit_is_refused(self) -> None:
        self.refused({**PIN, "commit": "a5a2709"}, "commit")

    def test_an_upper_case_commit_is_refused(self) -> None:
        self.refused({**PIN, "commit": PIN["commit"].upper()}, "commit")

    def test_another_contract_or_repository_is_refused(self) -> None:
        self.refused({**PIN, "contract": "some-other-contract"}, "contract")
        self.refused({**PIN, "repository": "https://example.invalid/fork"}, "repository")

    def test_a_version_that_is_not_semver_is_refused(self) -> None:
        self.refused({**PIN, "version": "0.1"}, "version")
        self.refused({**PIN, "version": "01.1.0"}, "version")

    def test_a_profile_outside_the_enum_is_refused(self) -> None:
        self.refused({**PIN, "profile": "observatory-local-mcp"}, "profile")

    def test_a_missing_member_is_refused(self) -> None:
        for member in ("contract", "version", "repository", "commit"):
            self.refused({k: v for k, v in PIN.items() if k != member}, member)

    def test_a_member_the_schema_does_not_define_is_refused(self) -> None:
        self.refused({**PIN, "schemas": []}, "schemas")

    def test_the_old_profile_lock_is_not_a_pin(self) -> None:
        self.refused(PROFILE, "contract")

    def test_a_document_that_is_not_an_object_is_refused(self) -> None:
        self.refused([PIN], "object")


class TheLegacyName(unittest.TestCase):
    """The profile lock stays readable under `fabric-contract.lock.json` for one
    release, and only while that file still has the profile's shape."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_a_tree_from_before_the_rename_still_reads(self) -> None:
        write(self.root, contract_lock.CONTRACT_PIN, PROFILE)
        self.assertEqual(contract_lock.profile_lock(self.root)["profile"], "observatory-local-mcp")
        self.assertIsNone(contract_lock.contract_pin(self.root),
                          "a profile lock under the pin's name is never read as the pin")

    def test_the_new_name_wins_when_both_are_present(self) -> None:
        write(self.root, contract_lock.PROFILE_LOCK, {**PROFILE, "version": "9.9.9"})
        write(self.root, contract_lock.CONTRACT_PIN, PROFILE)
        self.assertEqual(contract_lock.profile_lock(self.root)["version"], "9.9.9")

    def test_a_pin_under_the_old_name_is_never_read_as_the_profile(self) -> None:
        write(self.root, contract_lock.CONTRACT_PIN, PIN)
        with self.assertRaises(FileNotFoundError) as caught:
            contract_lock.profile_lock(self.root)
        self.assertIn(contract_lock.PROFILE_LOCK, str(caught.exception))

    def test_no_file_at_all_names_the_file_it_wanted(self) -> None:
        with self.assertRaises(FileNotFoundError) as caught:
            contract_lock.profile_lock(self.root)
        self.assertIn(contract_lock.PROFILE_LOCK, str(caught.exception))
        self.assertIsNone(contract_lock.contract_pin(self.root))


class EveryReaderGoesThroughOneModule(unittest.TestCase):
    """The defect was a reader keyed on a file NAME. No engine source holds either
    name as a string any more (a comment may still name a file in backticks); the
    sandbox runner's list of files to copy is the one other place allowed to."""

    ALLOWED = {"contract_lock.py", "tests/test_contract_lock.py", "tests/run_portable.py"}

    def test_no_other_source_names_either_lock(self) -> None:
        names = (contract_lock.CONTRACT_PIN, contract_lock.PROFILE_LOCK)
        offenders = []
        for path in sorted(ROOT.rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if relative in self.ALLOWED or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            offenders += [f"{relative} holds {name!r}" for name in names
                          if re.search(r"[\"']" + re.escape(name) + r"[\"']", text)]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
