#!/usr/bin/env python3
"""Synthetic vault filesystem and accidental secret-output boundary checks."""
from __future__ import annotations
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
with tempfile.TemporaryDirectory(prefix="observatory-vault-import-") as temporary:
    with patch.dict(os.environ, {"OBSERVATORY_HOME": str(Path(temporary).resolve())}):
        import vault
        import use_secret


class VaultBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="observatory-vault-boundary-")
        self.root = Path(self.temp.name).resolve()
        self.store = self.root / "secrets/projects"
        self.home = self.root / "home"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("OBSERVATORY_")}
        self.env.update({"OBSERVATORY_HOME": str(self.home), "OBSERVATORY_VAULT_DIR": str(self.store),
                         "OBSERVATORY_DATA": str(self.root / "projects")})
        self.patches = [patch.multiple(vault, STORE=self.store, LEAKS=self.store / "leaks.jsonl",
                                      MOVES=self.store / "movements.jsonl"),
                        patch.multiple(use_secret, VAULT=self.store, AUDIT=self.root / "audit/events.jsonl"),
                        patch.multiple(use_secret.paths, DATA=self.root / "projects", SCRATCH=self.root / "raw")]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def cli(self, tool, args, value=None):
        return subprocess.run([sys.executable, str(ROOT / "tools" / tool), *args],
                              input=value, text=True, capture_output=True, env=self.env, timeout=20)

    def test_backup_script_is_found_at_the_root_or_in_bin(self):
        """ONBOARDING said `gateway_root`'s bin/ holds the script while the vault
        looked only at its root, and no document named the file."""
        gateway = self.root / "gateway"
        (gateway / "bin").mkdir(parents=True)
        self.assertIsNone(vault.backup_script(gateway))
        script = gateway / "bin" / "backup-secrets.sh"
        script.write_text("#!/bin/bash\necho synthetic backup\n")
        self.assertEqual(vault.backup_script(gateway), script)
        top = gateway / "backup-secrets.sh"
        top.write_text("#!/bin/bash\necho synthetic backup\n")
        self.assertEqual(vault.backup_script(gateway), top, "the root copy is the documented first place")

    def test_names_excludes_metadata_and_retired_archives(self):
        slot = self.store / "example/local/EXAMPLE_KEY"
        vault._atomic_write(slot, "synthetic-value")
        vault._atomic_write(slot.with_name("EXAMPLE_KEY.meta.json"), "{}")
        vault._atomic_write(slot.with_name("EXAMPLE_KEY.retired-example"), "retired-synthetic")
        output = io.StringIO()
        with patch.object(use_secret.paths, "REGISTRY", self.root / "registry"), contextlib.redirect_stdout(output):
            self.assertEqual(use_secret.cmd_names(types.SimpleNamespace(project="example")), 0)
        text = output.getvalue()
        self.assertIn("EXAMPLE_KEY", text)
        for hidden in ("meta.json", "retired-example", "synthetic-value", "retired-synthetic"):
            self.assertNotIn(hidden, text)
        self.assertIn("1 name(s)", text)

    def test_help_names_the_installed_path_and_stdin_from_a_file(self):
        """The doors' --help said `./tools/…` (a checkout path an installed
        engine does not have) and `pbpaste | ./tools/openrouter.py stash`,
        contradicting the rule that a value arrives from a protected file."""
        for tool in ("vault", "use_secret", "keyserver", "openrouter", "cloudflare", "revoke_key"):
            with self.subTest(tool=tool):
                p = subprocess.run([sys.executable, str(ROOT / "tools" / f"{tool}.py"), "--help"],
                                   cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=60)
                self.assertEqual(p.returncode, 0, p.stderr[-300:])
                self.assertNotIn("./tools/", p.stdout)
                self.assertNotIn("pbpaste", p.stdout)
                self.assertIn("project-observatory full-path", p.stdout)
        p = subprocess.run([sys.executable, str(ROOT / "tools/vault.py"), "put", "--help"], cwd=ROOT,
                           env=os.environ.copy(), capture_output=True, text=True, timeout=60)
        self.assertIn("folder name", p.stdout)
        self.assertIn("UPPER_SNAKE", p.stdout)

    def test_revoke_key_without_a_key_says_how_to_install_one(self):
        p = subprocess.run([sys.executable, str(ROOT / "tools/revoke_key.py"), "--list"], cwd=ROOT,
                           env={**os.environ, "OBSERVATORY_STATE": str(self.root / "state")},
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("< ", p.stderr, "the install step: the key file redirected to stdin")
        self.assertIn("project-observatory full-path", p.stderr)

    def test_cli_help_survives_exported_empty_module_docstring(self):
        output = io.StringIO()
        with patch.object(vault, "__doc__", ""), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped:
                vault.main(["vault.py", "--help"])
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("rotate", output.getvalue())

    def test_atomic_write_ignores_predictable_tmp_symlink(self):
        slot = self.store / "example/local/EXAMPLE_KEY"
        slot.parent.mkdir(parents=True)
        victim = self.root / "victim"
        victim.write_text("keep")
        slot.with_name(slot.name + ".tmp").symlink_to(victim)
        vault._atomic_write(slot, "synthetic-value")
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual(slot.read_text(), "synthetic-value")
        self.assertEqual(slot.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(slot.parent.glob(".vault-write-*")), [])

    def test_symlink_destination_and_parent_refused(self):
        external = self.root / "external"
        external.mkdir()
        victim = external / "value"
        victim.write_text("keep")
        self.store.mkdir(parents=True)
        (self.store / "linked").symlink_to(external, target_is_directory=True)
        for target in (self.store / "linked/value", self.store / "direct"):
            if target.name == "direct":
                target.symlink_to(victim)
            with self.assertRaises(ValueError):
                vault._atomic_write(target, "replacement")
        self.assertEqual(victim.read_text(), "keep")

    def test_concurrent_atomic_writes_are_complete_and_private(self):
        slot = self.store / "example/local/EXAMPLE_KEY"
        values = [(str(index) + "-") * 4096 for index in range(12)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as workers:
            list(workers.map(lambda value: vault._atomic_write(slot, value), values))
        self.assertIn(slot.read_text(), values)
        self.assertEqual(slot.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(slot.parent.glob(".vault-write-*")), [])

    def test_concurrent_first_put_has_one_winner(self):
        values = ["synthetic-concurrent-" + str(index) for index in range(5)]
        def run(value):
            return self.cli("vault.py", ["put", "example", "local", "EXAMPLE_KEY"], value)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as workers:
            results = list(workers.map(run, values))
        self.assertEqual(sum(result.returncode == 0 for result in results), 1)
        winner = next(value for value, result in zip(values, results) if result.returncode == 0)
        self.assertEqual((self.store / "example/local/EXAMPLE_KEY").read_text(), winner)
        for value, result in zip(values, results):
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_put_and_rotate_print_no_value_or_tail_and_keep_archives(self):
        old = "synthetic-original-" + "q7Z!"
        new = "synthetic-replacement-" + "r8Y?"
        first = self.cli("vault.py", ["put", "example", "local", "EXAMPLE_KEY"], old)
        second = self.cli("vault.py", ["rotate", "example", "local", "EXAMPLE_KEY"], new)
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        for value in (old, new, old[-4:], new[-4:]):
            self.assertNotIn(value, first.stdout + first.stderr + second.stdout + second.stderr)
        archives = list((self.store / "example/local").glob("EXAMPLE_KEY.retired-*"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_text(), old)
        self.assertNotIn(old, (self.store / "movements.jsonl").read_text())
        self.assertNotIn(new, (self.store / "movements.jsonl").read_text())

    def test_symlinked_metadata_or_journal_refuses_before_slot_write(self):
        slot = self.store / "example/local/EXAMPLE_KEY"
        slot.parent.mkdir(parents=True)
        slot.write_text("keep")
        slot.chmod(0o600)
        victim = self.root / "victim"
        victim.write_text("{}")
        victim.chmod(0o600)
        for link in (slot.with_name(slot.name + ".meta.json"), self.store / "movements.jsonl"):
            link.symlink_to(victim)
            result = self.cli("vault.py", ["put", "example", "local", "EXAMPLE_KEY", "--force"], "replacement")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(slot.read_text(), "keep")
            self.assertEqual(victim.read_text(), "{}")
            link.unlink()

    def test_dangling_metadata_and_bad_rotation_count_refuse_before_effects(self):
        slot = self.store / "example/local/EXAMPLE_KEY"
        self.assertEqual(self.cli("vault.py", ["put", "example", "local", "EXAMPLE_KEY"],
                                  "original-synthetic").returncode, 0)
        meta = slot.with_name(slot.name + ".meta.json")
        meta.unlink()
        meta.symlink_to(self.root / "absent")
        for args in (["put", "example", "local", "EXAMPLE_KEY", "--force"],
                     ["rotate", "example", "local", "EXAMPLE_KEY"]):
            result = self.cli("vault.py", args, "replacement-synthetic")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(slot.read_text(), "original-synthetic")
        meta.unlink()
        for count in ("bad", -1, True):
            vault._atomic_write(meta, json.dumps({"rotations": count}))
            result = self.cli("vault.py", ["rotate", "example", "local", "EXAMPLE_KEY"],
                              "replacement-synthetic")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(slot.read_text(), "original-synthetic")
            self.assertEqual(list(slot.parent.glob("*.retired-*")), [])

    def test_concurrent_rotations_preserve_metadata_count_and_all_archives(self):
        args = ["put", "example", "local", "EXAMPLE_KEY"]
        self.assertEqual(self.cli("vault.py", args, "initial-synthetic").returncode, 0)
        values = ["replacement-synthetic-" + str(index) for index in range(5)]
        def rotate(value):
            return self.cli("vault.py", ["rotate", "example", "local", "EXAMPLE_KEY"], value)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as workers:
            results = list(workers.map(rotate, values))
        self.assertTrue(all(result.returncode == 0 for result in results))
        slot = self.store / "example/local/EXAMPLE_KEY"
        self.assertEqual(json.loads(slot.with_name(slot.name + ".meta.json").read_text())["rotations"], 5)
        archived = [path.read_text() for path in slot.parent.glob("*.retired-*")]
        self.assertEqual(len(archived), 5)
        self.assertEqual(set(archived + [slot.read_text()]), set(values + ["initial-synthetic"]))

    def test_journal_and_audit_symlinks_do_not_modify_targets(self):
        victim = self.root / "victim"
        victim.write_text("keep")
        self.store.mkdir(parents=True)
        vault.MOVES.symlink_to(victim)
        with self.assertRaises(ValueError):
            vault.journal("test", "example/local/EXAMPLE_KEY")
        use_secret.AUDIT.parent.mkdir()
        use_secret.AUDIT.symlink_to(victim)
        with self.assertRaises(ValueError):
            use_secret.audit("test", "EXAMPLE_KEY", {})
        self.assertEqual(victim.read_text(), "keep")

    def test_use_secret_refuses_identifier_and_inventory_escape(self):
        for project, name in (("../other", "EXAMPLE_KEY"), ("example", "../../../outside")):
            with self.assertRaises(ValueError):
                use_secret.resolve(project, name)
        self.root.joinpath("raw").mkdir()
        (self.root / "raw/env.json").write_text(json.dumps({"files": [
            {"project": "example", "path": "../outside.env", "variables": [{"name": "EXAMPLE_KEY"}]}]}))
        with self.assertRaises(ValueError):
            use_secret.resolve("example", "EXAMPLE_KEY")

    def test_private_slot_symlink_or_permissions_refused(self):
        slot = self.store / "example/local/EXAMPLE_KEY"
        slot.parent.mkdir(parents=True)
        outside = self.root / "outside"
        outside.write_text("synthetic")
        slot.symlink_to(outside)
        with self.assertRaises(ValueError):
            use_secret.resolve("example", "EXAMPLE_KEY")
        slot.unlink()
        slot.write_text("synthetic")
        slot.chmod(0o644)
        with self.assertRaises(ValueError):
            use_secret.resolve("example", "EXAMPLE_KEY")

    def test_overlapping_secrets_never_partially_expose_longer_value(self):
        values = {"SHORT": "synthetic-prefix", "LONG": "synthetic-prefix-extended-value"}
        original = b"before synthetic-prefix-extended-value middle synthetic-prefix after"
        expected = "before «LONG» middle «SHORT» after".encode()
        for chunk_size in range(1, 45):
            with patch.object(use_secret, "CHUNK", chunk_size):
                output = io.BytesIO()
                use_secret.pump(io.BytesIO(original), output, values, 1)
                self.assertEqual(output.getvalue(), expected, chunk_size)
        self.assertEqual(use_secret.scrub(original, values), expected)

    def test_pipe_filters_both_streams_and_preserves_child_exit(self):
        value = "synthetic-output-secret-" + "x" * 35
        program = 'import os,sys;v=os.environ["EXAMPLE_KEY"];sys.stdout.write("a"*65530+v);sys.stderr.write(v);sys.exit(7)'
        result = self.cli("use_secret.py", ["pipe", "EXAMPLE_KEY", "--", sys.executable, "-c", program], value)
        self.assertEqual(result.returncode, 7)
        self.assertNotIn(value, result.stdout + result.stderr)
        self.assertIn("«EXAMPLE_KEY»", result.stdout)
        self.assertIn("«EXAMPLE_KEY»", result.stderr)
        audit = self.home / "store/logs/secret-use.jsonl"
        self.assertEqual(audit.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(value, audit.read_text())


if __name__ == "__main__":
    unittest.main()
