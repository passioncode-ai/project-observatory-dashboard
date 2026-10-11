#!/usr/bin/env python3
"""The estate's month (OBS-56): collectors/scan_spend.py sums each stashed account's activity for the current UTC
month, never touching a value, and names an account it could not read; build_findings raises spend.estate_month at
the alert ratio and makes it critical at the limit.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "collectors"))
PY = sys.executable
MONTH = datetime.now(timezone.utc).strftime("%Y-%m")


class ScanSpend(unittest.TestCase):
    def run_scan(self, answers: dict) -> dict:
        import openrouter
        import scan_spend

        def activity(key):
            answer = answers[key]
            if isinstance(answer, int):
                raise urllib.error.HTTPError("https://openrouter.ai/api/v1/activity", answer, "x", {}, io.BytesIO())
            return answer
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "spend.json"
            with patch.object(openrouter, "admins", lambda: [(k, Path(k)) for k in answers]), \
                    patch.object(openrouter, "read_admin", lambda label: (label, label)), \
                    patch.object(scan_spend, "activity", activity), \
                    patch("configuration.enabled", lambda name: True):
                self.assertEqual(scan_spend.main(["scan_spend.py", str(out)]), 0)
            return json.loads(out.read_text())

    def test_the_month_is_summed_per_account(self):
        doc = self.run_scan({
            "acct-a": [{"date": f"{MONTH}-01 00:00:00", "usage": 1.5}, {"date": f"{MONTH}-03 00:00:00", "usage": 2.25},
                       {"date": "2020-01-31 00:00:00", "usage": 99}],
            "acct-b": [{"date": f"{MONTH}-02 00:00:00", "usage": 0.25}]})
        self.assertEqual(doc["month"], MONTH)
        self.assertEqual({a["account"]: a["usd"] for a in doc["accounts"]}, {"acct-a": 3.75, "acct-b": 0.25})
        self.assertEqual(doc["total_usd"], 4.0)
        self.assertFalse(doc["partial"])
        self.assertEqual(doc["accounts"][0]["through"], f"{MONTH}-03")

    def test_an_account_that_does_not_answer_is_named_and_the_total_is_partial(self):
        doc = self.run_scan({"acct-a": [{"date": f"{MONTH}-01", "usage": 1}], "acct-b": 403})
        self.assertTrue(doc["partial"])
        self.assertEqual(doc["total_usd"], 1.0)
        self.assertEqual(doc["degraded"], [{"source": "spend:openrouter:acct-b", "reason": "GET /activity HTTP 403"}])
        self.assertNotIn("acct-b", json.dumps(doc["accounts"]))


class EstateFinding(unittest.TestCase):
    def findings(self, spent: float, config: dict | None = None) -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            scratch, home = base / "raw", base / "ws"
            scratch.mkdir()
            (scratch / "spend.json").write_text(json.dumps({
                "month": MONTH, "scanned_at": datetime.now(timezone.utc).isoformat(), "total_usd": spent,
                "partial": False, "degraded": [], "accounts": [{"account": "acct-a", "usd": spent}]}))
            env = {**os.environ, "OBSERVATORY_SCRATCH": str(scratch), "OBSERVATORY_HOME": str(home),
                   "OBSERVATORY_SYSTEM_SETUP": "0"}
            subprocess.run([PY, str(ROOT / "observatory.py"), "init"], env=env, capture_output=True, timeout=300)
            if config is not None:
                (home / "config" / "meter.json").write_text(json.dumps(config))
            code = ("import importlib.util, json, sys\n"
                    f"sys.path.insert(0, {str(ROOT)!r})\n"
                    f"spec = importlib.util.spec_from_file_location('bf', {str(ROOT / 'tools/build_findings.py')!r})\n"
                    "bf = importlib.util.module_from_spec(spec); spec.loader.exec_module(bf)\n"
                    "print(json.dumps([f for f in bf.collect() if f['type'] == 'spend.estate_month']))\n")
            p = subprocess.run([PY, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
            self.assertEqual(p.returncode, 0, p.stderr[-1500:])
            return json.loads(p.stdout.strip().splitlines()[-1])

    def test_below_the_alert_nothing_is_said(self):
        self.assertEqual(self.findings(700), [])

    def test_at_eighty_percent_a_warning_and_at_the_limit_critical(self):
        warning = self.findings(800)
        self.assertEqual([f["severity"] for f in warning], ["warning"])
        self.assertIn("$800.00", warning[0]["title"])
        self.assertEqual([f["severity"] for f in self.findings(1000)], ["critical"])

    def test_the_limit_and_ratio_come_from_the_meters_configuration(self):
        self.assertEqual([f["severity"] for f in self.findings(150, {"estateMonthUsd": 200, "alertRatio": 0.5})],
                         ["warning"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
