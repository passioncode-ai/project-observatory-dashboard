"""Run the J-1 evidence regression in its own synthetic process."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class J1EvidenceSandbox(unittest.TestCase):
    def test_synthetic_evidence_regressions(self):
        with tempfile.TemporaryDirectory(prefix="j1-evidence-home-") as home:
            env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR") if key in os.environ}
            env.update(HOME=home, PYTHONDONTWRITEBYTECODE="1", OBSERVATORY_HOME=str(Path(home) / "workspace"))
            result = subprocess.run([sys.executable, str(ROOT / "observatory/engine/tests/test_j1_evidence.py"), "-q"],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-5000:])
