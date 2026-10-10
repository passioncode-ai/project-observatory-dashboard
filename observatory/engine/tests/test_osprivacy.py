#!/usr/bin/env python3
"""`osprivacy` says who the engine runs as on every OS (docs/design/WINDOWS-LINUX.md, W2b)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import osprivacy  # noqa: E402


class RealHome(unittest.TestCase):
    def test_the_real_home_is_an_existing_folder(self):
        home = osprivacy.real_home()
        self.assertIsNotNone(home)
        self.assertTrue(home.is_dir(), home)

    def test_the_environment_does_not_move_it(self):
        # A test runner points HOME (USERPROFILE on Windows) at a sandbox; the account's real
        # home is what tells the two apart, so it must not follow the variable.
        before = osprivacy.real_home()
        with tempfile.TemporaryDirectory() as sandbox, \
                mock.patch.dict(os.environ, {"HOME": sandbox, "USERPROFILE": sandbox}):
            self.assertEqual(osprivacy.real_home(), before)
            self.assertNotEqual(Path.home().resolve(), before.resolve())


if __name__ == "__main__":
    unittest.main(verbosity=2)
