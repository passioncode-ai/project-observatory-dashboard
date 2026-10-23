#!/usr/bin/env python3
"""The two tools that vendor contract files and move the pin (OBS-205).

`tools/vendor_org_vectors.py` moves the engine's one pin of the contract with the
files it vendors; `tools/vendor_contract_schemas.py` keeps the interop schemas at
that pin, with the `$ref` closure of them. Both read the pin through
`contract_lock` and check it, and read every file, before they write anything: a
missing or invalid pin, or a README they do not recognise, leaves the tree as it was.
These cases run both against a contract checkout made here, from the engine's own
vendored copies, so they need git and no network. The last class extends the engine's
one-reader test (`observatory/engine/tests/test_contract_lock.py`) to this repository's
`tools/`: CI runs these root tests beside the engine's portable suites, which see only
the engine.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
ROOT = REPO / "observatory" / "engine"
TOOLS = REPO / "tools"


def load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location("test_vendor_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract_lock = load(ROOT / "contract_lock.py", "contract_lock")
# Every test's git environment: configuration shut out, no background maintenance (the engine tests' helper).
git_env = load(ROOT / "tests" / "git_env.py", "git_env")


def snapshot(folder: pathlib.Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


@unittest.skipUnless(shutil.which("git"), "git is absent")
class VendoringMovesThePinWithTheFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.vectors = load(TOOLS / "vendor_org_vectors.py", "vendor_org_vectors")
        self.schemas = load(TOOLS / "vendor_contract_schemas.py", "vendor_contract_schemas")
        self._dir = tempfile.TemporaryDirectory()
        base = pathlib.Path(self._dir.name)
        self.engine = base / "engine"
        (self.engine / "fabric" / "node-vectors").mkdir(parents=True)
        shutil.copy2(ROOT / contract_lock.CONTRACT_PIN, self.engine / contract_lock.CONTRACT_PIN)
        shutil.copy2(ROOT / "fabric" / "node-vectors" / "README.md", self.engine / "fabric" / "node-vectors")
        for folder in self.schemas.FOLDERS:
            shutil.copytree(ROOT / folder, self.engine / folder)
        # A contract checkout holding, at its one commit, what the engine vendors from it.
        self.contract = base / "contract"
        for name, source in self.vectors.FILES.items():
            target = self.contract / source
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "fabric" / "node-vectors" / name, target)
        semantic = self.contract / "fixtures" / "semantic"
        semantic.mkdir(parents=True, exist_ok=True)
        for path in (ROOT / "fabric" / "node-vectors" / "semantic").glob("estate-*.json"):
            shutil.copy2(path, semantic / path.name)
        (self.contract / "schemas").mkdir(exist_ok=True)
        for folder in self.schemas.FOLDERS:
            for path in (ROOT / folder).glob("*.schema.json"):
                shutil.copy2(path, self.contract / "schemas" / path.name)
        self.commit = self.commit_contract("vendored files")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.contract), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                               "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True,
                              env=git_env.git_env()).stdout.strip()

    def commit_contract(self, message: str) -> str:
        if not (self.contract / ".git").exists():
            self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def pin(self) -> dict:
        return json.loads((self.engine / contract_lock.CONTRACT_PIN).read_text(encoding="utf-8"))

    def refused(self, error: str) -> None:
        before = snapshot(self.engine)
        with self.assertRaises(SystemExit) as caught:
            self.vectors.vendor(self.contract, self.commit, self.engine)
        self.assertEqual(json.loads(str(caught.exception.code))["error"], error)
        self.assertEqual(snapshot(self.engine), before, "a refused run wrote something")

    def test_a_vendored_commit_moves_the_pin_and_every_readme_with_it(self) -> None:
        result = self.vectors.vendor(self.contract, self.commit, self.engine)
        self.assertEqual(result["commit"], self.commit)
        self.assertEqual(self.pin()["commit"], self.commit)
        self.assertEqual(contract_lock.pin_problems(self.pin()), [])
        self.assertIn(contract_lock.CONTRACT_PIN, result["changed"])
        for folder in self.schemas.FOLDERS:
            readme = (self.engine / folder / "README.md").read_text(encoding="utf-8")
            self.assertIn("`passioncode-ai/fabric-agent-contract` at commit `%s`" % self.commit, readme)
        # The pin is the last write: the README of every folder now names the commit the pin does.
        self.assertEqual(contract_lock.find_pin_drift(
            [(p.name, p.read_text(encoding="utf-8")) for p in self.engine.rglob("README.md")], self.commit), [])

    def test_a_missing_pin_writes_nothing(self) -> None:
        (self.engine / contract_lock.CONTRACT_PIN).unlink()
        self.refused("pin-missing")

    def test_an_invalid_pin_writes_nothing(self) -> None:
        (self.engine / contract_lock.CONTRACT_PIN).write_text(json.dumps({**self.pin(), "commit": "1f31e5a"}),
                                                              encoding="utf-8")
        self.refused("pin-invalid")

    def test_a_pin_that_is_not_json_writes_nothing(self) -> None:
        (self.engine / contract_lock.CONTRACT_PIN).write_text("{", encoding="utf-8")
        self.refused("pin-invalid")

    def test_a_schema_readme_it_does_not_recognise_writes_nothing(self) -> None:
        readme = self.engine / "fabric" / "interop-schemas" / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8").replace("at commit `", "at `"), encoding="utf-8")
        self.refused("readme-unrecognised")

    def test_a_commit_the_checkout_lacks_writes_nothing(self) -> None:
        self.commit = "0" * 39 + "1"
        self.refused("commit-unreadable")

    def test_the_schemas_take_a_new_ref_with_them(self) -> None:
        """The `$ref` closure: a schema that starts to refer to another brings it along."""
        usage = self.contract / "schemas" / "interop-job.schema.json"
        doc = json.loads(usage.read_text(encoding="utf-8"))
        doc.setdefault("$defs", {})["added"] = {"$ref": "added.schema.json"}
        usage.write_text(json.dumps(doc), encoding="utf-8")
        (self.contract / "schemas" / "added.schema.json").write_text('{"type": "string"}', encoding="utf-8")
        commit = self.commit_contract("a new reference")
        files = self.schemas.plan(self.contract, commit, self.engine)
        added = self.engine / "fabric" / "interop-schemas" / "added.schema.json"
        self.assertIn(added, files)
        self.schemas.write(files, self.engine)
        readme = (self.engine / "fabric" / "interop-schemas" / "README.md").read_text(encoding="utf-8")
        self.assertIn("| `added.schema.json` | `%s` |" % hashlib.sha256(added.read_bytes()).hexdigest(), readme)

    def test_the_schema_tool_reads_the_pin_through_the_module_and_refuses_a_bad_one(self) -> None:
        self.assertEqual(self.schemas.pinned_commit(self.engine), self.pin()["commit"])
        (self.engine / contract_lock.CONTRACT_PIN).write_text(json.dumps({**self.pin(), "contract": "other"}),
                                                              encoding="utf-8")
        with self.assertRaises(self.schemas.VendorError) as caught:
            self.schemas.pinned_commit(self.engine)
        self.assertEqual(caught.exception.detail["error"], "pin-invalid")
        (self.engine / contract_lock.CONTRACT_PIN).unlink()
        with self.assertRaises(self.schemas.VendorError) as caught:
            self.schemas.pinned_commit(self.engine)
        self.assertEqual(caught.exception.detail["error"], "pin-missing")

    def test_a_failed_write_leaves_the_old_file_whole(self) -> None:
        """A write goes through a temporary file and a rename (the #348 review's LOW 1)."""
        target = self.engine / "fabric" / "node-vectors" / "README.md"
        before = target.read_bytes()

        class Boom(Exception):
            pass

        real = self.vectors.os.fsync

        def failing(fd):
            raise Boom()

        self.vectors.os.fsync = failing
        try:
            with self.assertRaises(Boom):
                self.vectors.write_atomic(target, b"partial")
        finally:
            self.vectors.os.fsync = real
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual([p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_vendoring_twice_changes_nothing_the_second_time(self) -> None:
        self.vectors.vendor(self.contract, self.commit, self.engine)
        self.assertEqual(self.vectors.vendor(self.contract, self.commit, self.engine)["changed"], [])


class TheToolsReadThePinThroughOneModule(unittest.TestCase):
    """The engine's one-reader rule (OBS-196), over this repository's `tools/`: no tool
    holds either lock's file name as a string, and the two that vendor contract files
    take the pin from `contract_lock.contract_pin`, which checks it (OBS-205)."""

    def test_no_tool_names_either_lock(self) -> None:
        names = (contract_lock.CONTRACT_PIN, contract_lock.PROFILE_LOCK)
        offenders = []
        for path in sorted(TOOLS.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            offenders += [f"{path.relative_to(REPO).as_posix()} holds {name!r}" for name in names
                          if re.search(r"[\"']" + re.escape(name) + r"[\"']", text)]
        self.assertEqual(offenders, [])

    def test_the_vendoring_tools_take_the_pin_from_the_module(self) -> None:
        for tool in ("vendor_org_vectors.py", "vendor_contract_schemas.py"):
            text = (TOOLS / tool).read_text(encoding="utf-8")
            self.assertIn("contract_lock.py", text, tool)
            self.assertIn(".contract_pin(", text, tool)


if __name__ == "__main__":
    unittest.main()
