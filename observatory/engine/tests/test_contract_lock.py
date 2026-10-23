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

    def test_every_commit_the_node_vectors_readme_names_is_the_pin(self) -> None:
        """The whole folder is at one commit (OBS-171): the estate vectors and fixtures
        move with the organization-enrollment files, never left at an older commit."""
        readme = (ROOT / "fabric/node-vectors/README.md").read_text(encoding="utf-8")
        named = re.findall(r"at commit\s+`([0-9a-f]{40})`", readme)
        self.assertGreaterEqual(len(named), 2, "the README names the estate set's commit and the OBS-161 one")
        self.assertEqual(set(named), {contract_lock.contract_pin(ROOT)["commit"]})

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
    sandbox runner's list of files to copy is the one other place allowed to. The
    repository's `tools/`, which vendor contract files and move the pin, are held to
    the same rule by the root test `tests/test_vendor_contract.py` (OBS-205): this
    suite runs in the portable sandbox, which holds the engine alone."""

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


# The cases' hashes are kept off the lines that name the contract, so this file is no drift itself.
OTHER = "20a818e648a4c09a60df0126d11626922e8b9094"
LINKED = "489737051828fafec92463df04b6a6fd3280c7b7"
NAME = "fabric-agent-contract"


class PinDriftIsPinChecksRule(unittest.TestCase):
    """`find_pin_drift` against the contract's own cases of `findPinDrift`
    (`test/consistency.test.ts`, G-11), with the contract's own example pin."""

    def drift(self, text: str) -> list:
        return contract_lock.find_pin_drift([("README.md", text)], PIN["commit"])

    def test_accepts_mentions_that_equal_the_pin_in_full_or_abbreviated(self) -> None:
        self.assertEqual(self.drift(f"Fabric Agent Contract `0.1.0` at `{PIN['commit']}`\ncontract pin {PIN['commit'][:7]}"), [])

    def test_flags_a_contract_commit_that_is_not_the_pin(self) -> None:
        self.assertEqual(self.drift(f"- contract commit: `{OTHER}`"), [("README.md", 1, OTHER)])

    def test_flags_a_tree_link_to_another_contract_revision(self) -> None:
        link = f"[contract](https://github.com/passioncode-ai/{NAME}/tree/{LINKED})"
        self.assertEqual(len(self.drift(link)), 1)

    def test_ignores_hashes_on_lines_that_are_not_about_the_contract(self) -> None:
        self.assertEqual(self.drift("build 8b80be9 shipped; sha256:" + "0" * 64), [])

    def test_reads_a_line_under_a_heading_that_names_the_contract_pin(self) -> None:
        text = f"## Contract pin\n\n- version: `0.1.0`\n- commit: `{OTHER}`\n\n## License\n\nbuilt at {OTHER[:7]}"
        self.assertEqual([line for _, line, _ in self.drift(text)], [4])

    def test_does_not_read_another_contract_of_the_same_word(self) -> None:
        self.assertEqual(self.drift("the persistence contract landed in 82bc950"), [])

    def test_takes_words_and_plain_numbers_for_no_commit(self) -> None:
        self.assertEqual(self.drift("fabric-agent-contract deadbeefcafe at 1234567890"), [])

    def test_a_hash_on_the_line_after_the_contracts_name_is_not_read(self) -> None:
        """Why the vendored READMEs put the commit on the line that names the contract:
        `pin:check` reads only that line, and a commit wrapped onto the next is never checked."""
        self.assertEqual(self.drift(f"from `passioncode-ai/fabric-agent-contract` at commit\n`{OTHER}`"), [])


class PinChecksWalk(unittest.TestCase):
    """The files `pin:check` reads: its cases for dated records, `.git`, `node_modules`,
    file types and `--exclude` (`test/pin-check.test.ts`)."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._dir.name)
        write(self.root, contract_lock.CONTRACT_PIN, PIN)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def put(self, files: dict[str, str]) -> None:
        for relative, text in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def found(self, excluded: tuple[str, ...] = ()) -> list[str]:
        return sorted(path for path, _, _ in contract_lock.pin_drift(self.root, excluded))

    def test_skips_changelogs_handoffs_evidence_and_task_pipeline_runs(self) -> None:
        mention = f"fabric-agent-contract {OTHER}"
        self.put({"CHANGELOG.md": mention, "docs/handoffs/a.md": mention, "docs/evidence/run/proof.md": mention,
                  ".task-pipeline/run/state.json": mention, "README.md": f"Fabric Agent Contract `{PIN['commit'][:7]}`"})
        self.assertEqual(self.found(), [])

    def test_knows_a_dated_record_by_its_path(self) -> None:
        dated = ["CHANGELOG", "CHANGELOG.md", "a/b/CHANGELOG.txt", "docs/handoffs", "docs/handoffs/x.md",
                 "docs/evidence/a/b.json", ".task-pipeline/x"]
        self.assertEqual([p for p in dated if not contract_lock.is_dated_record(p)], [])
        other = ["README.md", "docs/handoffs-old/x.md", "docs/specification/x.md", "changelog.md", "src/evidence/x.ts"]
        self.assertEqual([p for p in other if contract_lock.is_dated_record(p)], [])

    def test_reads_only_text_files_outside_git_and_node_modules(self) -> None:
        mention = f"fabric-agent-contract {OTHER}"
        self.put({".git/notes.md": mention, "node_modules/kit/README.md": mention, "logo.png": mention,
                  "deep/node_modules/x.md": mention})
        self.assertEqual(self.found(), [])
        self.put({"src/a.py": f"# fabric-agent-contract {OTHER}", "notes.txt": mention})
        self.assertEqual(self.found(), ["notes.txt", "src/a.py"])

    def test_takes_exclude_paths(self) -> None:
        self.put({"vendor/notes.md": f"fabric-agent-contract {OTHER}"})
        self.assertEqual(self.found(), ["vendor/notes.md"])
        self.assertEqual(self.found(("vendor/",)), [])
        self.assertEqual(self.found(("vendor",)), [])

    def test_never_reads_the_pin_itself(self) -> None:
        self.assertEqual(contract_lock.pin_check_files(self.root), [])


class TheEngineNamesOneContractRevision(unittest.TestCase):
    """G-11 on this folder, as `pnpm --dir <contract checkout> pin:check observatory/engine`
    runs it with the engine's excludes: zero drift (OBS-205)."""

    def test_pin_check_finds_no_drift_in_the_engine(self) -> None:
        drift = contract_lock.pin_drift(ROOT)
        self.assertEqual(drift, [], "\n".join(f"{p}:{n} names contract revision {f}" for p, n, f in drift))

    def test_every_exclude_names_a_path_that_exists_and_says_why(self) -> None:
        for path, why in contract_lock.PIN_CHECK_EXCLUDES.items():
            self.assertTrue((ROOT / path).exists(), path)
            self.assertGreater(len(why), 20, path)

    def test_a_vendored_folder_names_the_pin_or_a_listed_commit(self) -> None:
        """A README may name its commit where `pin:check` does not read it; this check
        reads every id in it. A folder off the pin is listed, with why, in
        `VENDORED_AT_OTHER_COMMITS`; a listed commit the README no longer names fails too."""
        listed = {folder: set(commits) for folder, commits in contract_lock.VENDORED_AT_OTHER_COMMITS.items()}
        self.assertEqual(contract_lock.vendored_off_pin(ROOT), listed)
        for folder, commits in contract_lock.VENDORED_AT_OTHER_COMMITS.items():
            for commit, why in commits.items():
                self.assertGreater(len(why), 40, (folder, commit))

    def test_the_schemas_vendored_at_the_pin_name_it_on_the_contracts_line(self) -> None:
        commit = contract_lock.contract_pin(ROOT)["commit"]
        for folder in ("fabric/interop-schemas",):
            readme = (ROOT / folder / "README.md").read_text(encoding="utf-8")
            named = re.search(r"`passioncode-ai/fabric-agent-contract` at commit `([0-9a-f]{40})`", readme)
            self.assertIsNotNone(named, folder)
            self.assertEqual(named.group(1), commit, folder)


if __name__ == "__main__":
    unittest.main()
