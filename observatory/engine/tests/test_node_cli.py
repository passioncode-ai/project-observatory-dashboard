#!/usr/bin/env python3
"""`project-observatory full node` (OBS-114, RM-21 P5 slice 6): this machine's device keys from a terminal, run through
the dispatcher in a temporary workspace. Only public keys are printed; a second `--init` changes nothing."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tmp as tmpdir                                   # noqa: E402

try:
    import cryptography                                 # noqa: F401  the engine's `full` extra
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False


@unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')
class NodeCommand(unittest.TestCase):
    def setUp(self):
        self.home = Path(tmpdir.mkdtemp()).resolve() / 'workspace'
        self.env = {**os.environ, 'OBSERVATORY_HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1'}
        self.env.pop('OBSERVATORY_NODE', None)
        init = self.run_cli('init')
        self.assertEqual(init.returncode, 0, init.stdout + init.stderr)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'observatory.py'), *args], cwd=ROOT, env=self.env,
                              text=True, capture_output=True, timeout=120, stdin=subprocess.DEVNULL)

    @staticmethod
    def answer(process) -> dict:
        """The tool's JSON, under the dispatcher's step header."""
        text = process.stdout
        return json.loads(text[text.index('{'):text.rindex('}') + 1])

    def test_init_then_show(self):
        missing = self.run_cli('node', '--show')
        self.assertEqual(missing.returncode, 1)
        self.assertEqual(self.answer(missing)['error'], 'no-device-keys')
        first = self.run_cli('node', '--init')
        self.assertEqual(first.returncode, 0, first.stderr)
        identity = self.answer(first)
        self.assertRegex(identity['device'], r'^m-[0-9a-f]{32}$')
        self.assertEqual([k['kid'] for k in identity['deviceKeys']],
                         [identity['device'] + '#sig', identity['device'] + '#kem'])
        again = self.answer(self.run_cli('node', '--init'))
        shown = self.answer(self.run_cli('node', '--show'))
        self.assertEqual(again, identity)
        self.assertEqual(shown, identity)
        for output in (first.stdout, first.stderr):
            self.assertNotIn('PRIVATE', output)
        folder = self.home / 'node' / 'device'
        self.assertEqual(oct(folder.stat().st_mode & 0o777), '0o700')
        self.assertEqual(sorted(p.name for p in folder.iterdir() if not p.name.startswith('.')),
                         ['device.json', 'kem.pem', 'sig.pem'])

    def test_a_folder_others_may_write_is_refused_by_name(self):
        self.assertEqual(self.run_cli('node', '--init').returncode, 0)
        os.chmod(self.home / 'node' / 'device', 0o777)
        refused = self.run_cli('node', '--show')
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(self.answer(refused)['error'], 'device-keys-refused')

    def test_one_option_exactly(self):
        self.assertNotEqual(self.run_cli('node').returncode, 0)
        self.assertNotEqual(self.run_cli('node', '--init', '--show').returncode, 0)
        self.assertNotEqual(self.run_cli('node', '--folder=/elsewhere').returncode, 0)


if __name__ == '__main__':
    unittest.main()
