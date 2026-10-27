"""PB-091: local provider keys follow the selected state; two copies refuse instead of choosing.

paths.local_key and agent/providers.read_key, driven in a synthetic home with
OBSERVATORY_STATE redirected: legacy only, selected only, both and neither. No
value is printed; each case reports only where the key was read from.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-or-v1-" + "f" * 48   # synthetic; the shape the provider check expects


class LocalKeys(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.home, self.state = base / "runtime", base / "state"
        self.legacy = self.home / "store"
        self.legacy.mkdir(parents=True)
        self.state.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("OBSERVATORY_", "CLAUDE_MEM_", "OPENAI_", "OPENROUTER_"))}
        self.env.update(HOME=str(base / "user"), USERPROFILE=str(base / "user"), OBSERVATORY_HOME=str(self.home),
                        OBSERVATORY_STATE=str(self.state), PYTHONDONTWRITEBYTECODE="1")

    def put(self, where: Path) -> None:
        f = where / ".openrouter-key"
        f.write_text(KEY)
        f.chmod(0o600)

    def resolve(self) -> dict:
        src = ("import json, sys\nsys.path.insert(0, 'agent')\nimport paths, providers\n"
               "try:\n    key, where = providers.read_key()\n"
               "    print(json.dumps({'found': bool(key), 'where': where}))\n"
               "except providers.Fatal as exc:\n    print(json.dumps({'refused': str(exc)}))\n")
        p = subprocess.run([sys.executable, "-c", src], cwd=ROOT, env=self.env, text=True,
                           capture_output=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr[-500:])
        self.assertNotIn(KEY, p.stdout + p.stderr, "a value is never printed")
        return {**json.loads(p.stdout.strip().splitlines()[-1]), "stderr": p.stderr}

    def test_selected_only_is_read(self):
        self.put(self.state)
        r = self.resolve()
        self.assertTrue(r["found"])
        self.assertEqual(r["where"], str(self.state / ".openrouter-key"))

    def test_legacy_only_is_read_with_a_note_and_nothing_moves(self):
        self.put(self.legacy)
        r = self.resolve()
        self.assertEqual(r["where"], str(self.legacy / ".openrouter-key"))
        self.assertIn("legacy location", r["stderr"])
        self.assertFalse((self.state / ".openrouter-key").exists(), "nothing is copied or moved")

    def test_both_refuse_instead_of_choosing(self):
        self.put(self.state)
        self.put(self.legacy)
        r = self.resolve()
        self.assertIn("in both", r.get("refused", ""))

    def test_neither_names_the_selected_place(self):
        src = "import sys\nsys.path.insert(0, 'agent')\nimport providers\nprint(providers.key_status())\n"
        p = subprocess.run([sys.executable, "-c", src], cwd=ROOT, env=self.env, text=True,
                           capture_output=True, timeout=30)
        self.assertIn(str(self.state / ".openrouter-key"), p.stdout)

    def test_the_installer_refuses_while_a_legacy_key_exists(self):
        self.put(self.legacy)
        p = subprocess.run([sys.executable, "tools/install_key.py", "--for", "observatory"], cwd=ROOT,
                           env=self.env, input=KEY + "\n", text=True, capture_output=True, timeout=30)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("legacy location", p.stdout + p.stderr)
        self.assertFalse((self.state / ".openrouter-key").exists())
        self.assertNotIn(KEY, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
