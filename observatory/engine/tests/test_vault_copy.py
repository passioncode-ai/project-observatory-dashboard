#!/usr/bin/env python3
"""One vault slot copied into another secret store on the same machine (OBS-476).

Two temporary stores stand for this account's store and the other space's: the source is the vault
`OBSERVATORY_VAULT_DIR` names, the target a store folder passed as `--to-store`. Every command runs
`tools/vault.py` as a process, and its output is checked for the value after each test."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(ROOT / "tests"))
import tmp  # noqa: E402,F401  the sandbox defaults for a suite run by hand

VALUE_A = "synthetic-copy-value-alpha-0001"
VALUE_B = "synthetic-copy-value-bravo-0002"


class Copy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="observatory-vault-copy-")
        self.base = Path(self.temp.name).resolve()
        self.source_store = self.base / "mine" / "secrets"
        self.vault = self.source_store / "projects"
        self.target = self.base / "space" / "secrets"
        (self.base / "space").mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("OBSERVATORY_")}
        self.env.update({"OBSERVATORY_HOME": str(self.base / "home"), "OBSERVATORY_VAULT_DIR": str(self.vault),
                         "OBSERVATORY_STATE": str(self.base / "state")})
        self.outputs: list[str] = []

    def tearDown(self):
        text = "\n".join(self.outputs)
        for value in (VALUE_A, VALUE_B):
            self.assertNotIn(value, text, "a value reached a command's output")
        for journal in (self.vault / "movements.jsonl", self.target / "projects" / "movements.jsonl"):
            if journal.is_file():
                body = journal.read_text()
                self.assertNotIn(VALUE_A, body)
                self.assertNotIn(VALUE_B, body)
        self.temp.cleanup()

    def vault_cmd(self, *args: str, value: str | None = None) -> subprocess.CompletedProcess:
        p = subprocess.run([sys.executable, str(TOOLS / "vault.py"), *args], input=value, text=True,
                           capture_output=True, env=self.env, timeout=60)
        self.outputs.append(p.stdout + p.stderr)
        return p

    def put(self, name: str = "API_TOKEN", value: str = VALUE_A) -> None:
        p = self.vault_cmd("put", "alpha-web", "prod", name, value=value)
        self.assertEqual(p.returncode, 0, p.stderr)

    def copy(self, *extra: str, name: str = "API_TOKEN") -> tuple[int, dict]:
        p = self.vault_cmd("copy", "alpha-web", "prod", name, "--to-store", str(self.target), "--json", *extra)
        return p.returncode, json.loads(p.stdout)

    @staticmethod
    def journal(path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_a_copy_writes_the_target_keeps_the_source_and_journals_both(self):
        self.put()
        code, receipt = self.copy()
        self.assertEqual(code, 0, receipt)
        self.assertEqual(receipt["schema"], "vault-copy/1")
        self.assertEqual(receipt["outcome"], "copied")
        self.assertEqual(receipt["from"]["slot"], "alpha-web/prod/API_TOKEN")
        self.assertIs(receipt["from"]["kept"], True)
        self.assertEqual(receipt["to"]["store"], str(self.target))
        self.assertIs(receipt["to"]["storeCreated"], True)
        self.assertTrue(receipt["digest"]["match"])
        self.assertEqual(receipt["digest"]["key"], "one-time", "no estate key in the source store, none created")
        self.assertFalse((self.vault / ".estate-digest-key").exists())
        self.assertNotEqual(receipt["digest"]["from"], VALUE_A)
        target_slot = self.target / "projects/alpha-web/prod/API_TOKEN"
        self.assertEqual(target_slot.read_text().strip(), VALUE_A)
        self.assertEqual((self.vault / "alpha-web/prod/API_TOKEN").read_text().strip(), VALUE_A)
        if os.name != "nt":
            self.assertEqual(target_slot.stat().st_mode & 0o777, 0o600)
            for folder in (self.target, self.target / "projects", self.target / "projects/alpha-web",
                           self.target / "projects/alpha-web/prod"):
                self.assertEqual(folder.stat().st_mode & 0o777, 0o700, folder)
        mine = [r for r in self.journal(self.vault / "movements.jsonl") if r["event"] == "copied"]
        theirs = [r for r in self.journal(self.target / "projects/movements.jsonl") if r["event"] == "copied"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(len(theirs), 1)
        self.assertEqual((mine[0]["to_store"], mine[0]["to_slot"]), (str(self.target), "alpha-web/prod/API_TOKEN"))
        self.assertEqual((theirs[0]["from_store"], theirs[0]["from_slot"]),
                         (str(self.source_store), "alpha-web/prod/API_TOKEN"))
        self.assertNotIn("moved", {r["event"] for r in self.journal(self.vault / "movements.jsonl")})
        self.assertNotIn("digest", mine[0], "a one-time digest means nothing after the copy and is not journalled")
        meta = json.loads((self.target / "projects/alpha-web/prod/API_TOKEN.meta.json").read_text())
        self.assertEqual(meta["copied_from"]["slot"], "alpha-web/prod/API_TOKEN")

    def test_the_value_never_travels_in_argv_or_the_environment(self):
        source = (TOOLS / "vault.py").read_text()
        start = source.index("def copy_slot(")
        body = source[start:source.index("def cmd_copy(", start)]
        self.assertNotIn("subprocess.", body)
        self.assertNotIn("os.environ", body)
        self.assertNotIn("print(", body)

    def test_a_filled_target_is_refused_unless_replace_archives_it(self):
        self.put()
        self.assertEqual(self.copy()[0], 0)
        p = self.vault_cmd("rotate", "alpha-web", "prod", "API_TOKEN", value=VALUE_B)
        self.assertEqual(p.returncode, 0, p.stderr)
        code, refused = self.copy()
        self.assertEqual((code, refused["outcome"], refused["code"]), (2, "refused", "target-slot-filled"))
        self.assertIn("--replace", refused["reason"])
        self.assertEqual((self.target / "projects/alpha-web/prod/API_TOKEN").read_text().strip(), VALUE_A)
        code, receipt = self.copy("--replace")
        self.assertEqual((code, receipt["outcome"]), (0, "replaced"))
        self.assertTrue(receipt["to"]["archived"].startswith("API_TOKEN.retired-"))
        self.assertEqual((self.target / "projects/alpha-web/prod/API_TOKEN").read_text().strip(), VALUE_B)
        archive = self.target / "projects/alpha-web/prod" / receipt["to"]["archived"]
        self.assertEqual(archive.read_text().strip(), VALUE_A)
        theirs = [r for r in self.journal(self.target / "projects/movements.jsonl") if r["event"] == "copied"]
        self.assertEqual(theirs[-1]["archived"], receipt["to"]["archived"])

    def test_the_same_value_again_is_unchanged_and_journals_nothing(self):
        self.put()
        self.copy()
        before = (self.vault / "movements.jsonl").read_text()
        code, receipt = self.copy()
        self.assertEqual((code, receipt["outcome"], receipt["digest"]["match"]), (0, "unchanged", True))
        self.assertEqual((self.vault / "movements.jsonl").read_text(), before)

    def test_another_slot_name_and_the_estate_key(self):
        self.put()
        d = subprocess.run([sys.executable, str(TOOLS / "vault_digests.py"), "write", "--machine", "machine-a"],
                           text=True, capture_output=True, env=self.env, timeout=60)
        self.outputs.append(d.stdout + d.stderr)
        self.assertEqual(d.returncode, 0, d.stderr)
        code, receipt = self.copy("--to-slot", "beta-api/stage/SERVICE_TOKEN")
        self.assertEqual(code, 0, receipt)
        self.assertEqual(receipt["to"]["slot"], "beta-api/stage/SERVICE_TOKEN")
        self.assertEqual(receipt["digest"]["key"], "estate")
        self.assertRegex(receipt["digest"]["keyId"], r"^[0-9a-f]{16}$")
        manifest = json.loads((self.base / "state/vault-digests/local.json").read_text())
        mine = next(v for k, v in manifest["slots"].items() if k == "alpha-web/prod/API_TOKEN")
        self.assertEqual(receipt["digest"]["from"], mine["digest"], "comparable with full vault-digests")
        row = [r for r in self.journal(self.vault / "movements.jsonl") if r["event"] == "copied"][-1]
        self.assertEqual((row["digest"], row["key_id"]), (mine["digest"], receipt["digest"]["keyId"]))
        self.assertTrue((self.target / "projects/beta-api/stage/SERVICE_TOKEN").is_file())

    def test_expiry_and_provenance_travel_the_binding_does_not(self):
        self.put()
        sys.path.insert(0, str(TOOLS))
        code = ("import sys; sys.path.insert(0, %r); import vault\n"
                "vault.record_expiry('alpha-web', 'prod', 'API_TOKEN', '2030-01-01T00:00:00Z', tool='openrouter.py')\n"
                % str(TOOLS))
        p = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.vault_cmd("bind", "alpha-web", "prod", "API_TOKEN", "--header-for",
                                        "https://mcp.example.com").returncode, 0)
        code, receipt = self.copy()
        self.assertEqual(code, 0, receipt)
        self.assertIn("expires_at", receipt["carried"])
        meta = json.loads((self.target / "projects/alpha-web/prod/API_TOKEN.meta.json").read_text())
        self.assertEqual(meta["expires_at"], "2030-01-01T00:00:00Z")
        self.assertNotIn("header_for", meta)

    def test_refusals_name_their_reason(self):
        code, refused = self.copy()
        self.assertEqual((code, refused["code"]), (2, "source-empty"))
        self.put()
        p = self.vault_cmd("copy", "alpha-web", "prod", "API_TOKEN", "--to-store", "relative/store", "--json")
        self.assertEqual(json.loads(p.stdout)["code"], "target-not-absolute")
        p = self.vault_cmd("copy", "alpha-web", "prod", "API_TOKEN", "--to-store", str(self.source_store), "--json")
        self.assertEqual(json.loads(p.stdout)["code"], "same-store")
        p = self.vault_cmd("copy", "alpha-web", "prod", "API_TOKEN", "--to-store", str(self.vault), "--json")
        self.assertEqual(json.loads(p.stdout)["code"], "target-is-vault-folder")
        p = self.vault_cmd("copy", "alpha-web", "prod", "API_TOKEN", "--to-store", str(self.vault) + "/..", "--json")
        self.assertEqual(json.loads(p.stdout)["code"], "same-store", "another spelling of this store")
        p = self.vault_cmd("copy", "alpha-web", "prod", "API_TOKEN", "--to-store",
                           str(self.base / "absent" / "secrets"), "--json")
        self.assertEqual(json.loads(p.stdout)["code"], "target-parent-missing")
        p = self.vault_cmd("copy", "alpha-web", "prod", "API_TOKEN", "--to-store",
                           str(self.vault / "alpha-web" / "inner"), "--json")
        self.assertEqual(json.loads(p.stdout)["code"], "nested-stores")
        p = self.vault_cmd("copy", "alpha-web", "prod", "API_TOKEN", "--to-store", str(self.target),
                           "--to-slot", "a/b", "--json")
        self.assertEqual(json.loads(p.stdout)["code"], "target-slot-invalid")
        p = self.vault_cmd("copy", "alpha-web", "prod", "API_TOKEN", "--to-store", str(self.target),
                           "--to-slot", "lowercase", "--json")
        self.assertEqual(json.loads(p.stdout)["code"], "target-slot-invalid")
        (self.base / "file-store").write_text("not a folder")
        p = self.vault_cmd("copy", "alpha-web", "prod", "API_TOKEN", "--to-store", str(self.base / "file-store"),
                           "--json")
        self.assertEqual(json.loads(p.stdout)["code"], "target-not-folder")
        self.assertFalse(self.target.exists(), "nothing written by a refusal")

    def test_a_refusal_that_reads_the_value_leaves_no_trace(self):
        self.put()
        # A two-line value cannot be put through the vault; it is written into the slot file directly.
        slot = self.vault / "alpha-web/prod/API_TOKEN"
        slot.write_text(VALUE_A + "\n" + VALUE_B + "\n")
        code, refused = self.copy()
        self.assertEqual((code, refused["code"]), (2, "source-unreadable"))
        self.assertFalse(self.target.exists(), "a refusal leaves no store, vault folder or lock file behind")

    def test_a_refusal_under_the_locks_undoes_what_it_created(self):
        """The slot changes while the copy waits for the locks: the second check refuses, and what this call made
        in the target store (its folder, vault folder and lock file) is removed again."""
        self.put()
        code = ("import sys; sys.path.insert(0, %r); import vault\n"
                "real = vault._mutation_lock\n"
                "import contextlib\n"
                "@contextlib.contextmanager\n"
                "def racing(store=None):\n"
                "    with real(store):\n"
                "        (vault.STORE / 'alpha-web/prod/API_TOKEN').unlink(missing_ok=True)\n"
                "        yield\n"
                "vault._mutation_lock = racing\n"
                "try:\n"
                "    vault.copy_slot('alpha-web', 'prod', 'API_TOKEN', %r)\n"
                "except vault.CopyRefused as exc:\n"
                "    print(exc.code)\n" % (str(TOOLS), str(self.target)))
        p = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=60)
        self.outputs.append(p.stdout + p.stderr)
        self.assertEqual(p.stdout.strip(), "source-empty", p.stderr)
        self.assertFalse(self.target.exists())

    @unittest.skipUnless(hasattr(os, "symlink") and os.name != "nt", "a link stands for the vault folder")
    def test_a_linked_vault_folder_in_the_target_is_refused(self):
        self.put()
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.target.mkdir()
        os.symlink(elsewhere, self.target / "projects")
        code, refused = self.copy()
        self.assertEqual((code, refused["code"]), (2, "target-link"))
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_another_spelling_by_case_is_the_same_store_where_the_volume_ignores_case(self):
        self.put()
        upper = self.source_store.parent / self.source_store.name.upper()
        if not upper.exists():
            self.skipTest("this volume tells upper from lower case")
        code, refused = self.copy_to(str(upper))
        self.assertEqual((code, refused["code"]), (2, "same-store"))

    def copy_to(self, store: str) -> tuple[int, dict]:
        p = self.vault_cmd("copy", "alpha-web", "prod", "API_TOKEN", "--to-store", store, "--to-slot", "OTHER_TOKEN",
                           "--json")
        return p.returncode, json.loads(p.stdout)

    @unittest.skipUnless(hasattr(os, "symlink") and os.name != "nt", "a link stands for the store")
    def test_a_linked_store_is_refused(self):
        self.put()
        real = self.base / "real-store"
        real.mkdir()
        os.symlink(real, self.base / "space" / "linked")
        p = self.vault_cmd("copy", "alpha-web", "prod", "API_TOKEN", "--to-store",
                           str(self.base / "space" / "linked"), "--json")
        self.assertEqual(json.loads(p.stdout)["code"], "target-link")

    def test_an_open_leak_is_not_spread(self):
        self.put()
        self.assertEqual(self.vault_cmd("leak", "alpha-web", "prod", "API_TOKEN", "--where",
                                        "fixture transcript of a test session").returncode, 0)
        code, refused = self.copy()
        self.assertEqual((code, refused["code"]), (2, "source-has-open-leak"))
        self.assertFalse(self.target.exists())

    def test_a_store_of_another_account_is_refused_with_its_reason(self):
        """Ownership is read from the folder; the check is the one `copy_slot` runs, with the owner check forced by
        a fake euid, since a test cannot make a folder another account's."""
        self.put()
        self.target.mkdir()
        sys.path.insert(0, str(TOOLS))
        code = ("import os, sys; sys.path.insert(0, %r); import vault\n"
                "os.geteuid = lambda: os.stat(%r).st_uid + 1\n"
                "try:\n"
                "    vault.copy_slot('alpha-web', 'prod', 'API_TOKEN', %r)\n"
                "except vault.CopyRefused as exc:\n"
                "    print(exc.code)\n" % (str(TOOLS), str(self.target), str(self.target)))
        if not hasattr(os, "geteuid"):
            self.skipTest("no account ids on this platform")
        p = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=60)
        self.outputs.append(p.stdout + p.stderr)
        self.assertEqual(p.stdout.strip(), "target-other-account", p.stderr)
        self.assertFalse((self.target / "projects").exists())

    def test_a_human_refusal_names_its_code(self):
        self.put()
        p = self.vault_cmd("copy", "alpha-web", "prod", "API_TOKEN", "--to-store", str(self.source_store))
        self.assertEqual(p.returncode, 2)
        self.assertIn("copy refused (same-store)", p.stderr)
        self.assertIn("nothing was written", p.stderr)

    def test_the_human_receipt_says_what_happened_without_the_value(self):
        self.put()
        p = self.vault_cmd("copy", "alpha-web", "prod", "API_TOKEN", "--to-store", str(self.target))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("copied alpha-web/prod/API_TOKEN", p.stdout)
        self.assertIn("value hidden; the source stays", p.stdout)
        self.assertIn("match", p.stdout)
        self.assertIn("record `copied`", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
