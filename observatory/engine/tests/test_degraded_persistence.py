#!/usr/bin/env python3
"""A degradation that does not change for a day is a finding of its own (degradations.persistence).

2026-10-21: the Fabric attribution collector (OBS-160, shipped in 0.26.0) had written `0 aliases,
153 unattributed; degraded: access-refused` on every run for more than a day. The reason sat in its
receipt and reached the board only as one more `collector.degraded` info row among the structural
ones, so a capability that did nothing in production looked like every other partial source. Any
collector whose same degraded reason persists for more than PERSIST_HOURS is now a warning,
`collector.degraded_persistent`, generic to every receipt.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

import degradations as D  # noqa: E402

HOUR = 3600.0
REFUSED = {"fabric-attribution.json": [{"source": "fabric-hub", "reason": "access-refused",
                                        "detail": "Fabric refused the access request"}]}


class Persistence(unittest.TestCase):
    def test_a_reason_older_than_a_day_is_persistent_and_a_younger_one_is_not(self):
        record, persistent = D.persistence(REFUSED, {}, 1000.0)
        self.assertEqual(persistent, {})
        _, persistent = D.persistence(REFUSED, record, 1000.0 + (D.PERSIST_HOURS - 1) * HOUR)
        self.assertEqual(persistent, {})
        record, persistent = D.persistence(REFUSED, record, 1000.0 + (D.PERSIST_HOURS + 1) * HOUR)
        self.assertEqual(list(persistent), ["fabric-attribution.json"])
        (pair, since), = persistent["fabric-attribution.json"]
        self.assertEqual(since, 1000.0)
        self.assertIn("fabric-hub", pair)
        self.assertIn("access-refused", pair)

    def test_a_changed_reason_starts_its_own_clock_and_a_gone_one_is_forgotten(self):
        record, _ = D.persistence(REFUSED, {}, 0.0)
        other = {"fabric-attribution.json": [{"source": "fabric-hub", "reason": "hub-down"}]}
        record, persistent = D.persistence(other, record, 30 * HOUR)
        self.assertEqual(persistent, {})
        record, _ = D.persistence({}, record, 31 * HOUR)
        self.assertEqual(record["first_seen"], {})
        record, persistent = D.persistence(REFUSED, record, 32 * HOUR)
        self.assertEqual(persistent, {}, "a reason that came back starts again")

    def test_a_code_is_the_key_when_the_prose_carries_numbers(self):
        a = {"leak-scan.json": [{"source": "leak-scan:deadline-targets", "code": "deadline-targets",
                                 "reason": "after 732 of 4380 targets"}]}
        b = {"leak-scan.json": [{"source": "leak-scan:deadline-targets", "code": "deadline-targets",
                                 "reason": "after 900 of 4380 targets"}]}
        record, _ = D.persistence(a, {}, 0.0)
        _, persistent = D.persistence(b, record, 25 * HOUR)
        self.assertEqual(list(persistent), ["leak-scan.json"])

    def test_a_damaged_record_is_read_as_empty(self):
        for record in (None, [], {"first_seen": "x"}, {"first_seen": {"k": "nan"}}, {"first_seen": {"k": None}}):
            with self.subTest(record=record):
                new, persistent = D.persistence(REFUSED, record, 5.0)
                self.assertEqual(persistent, {})
                self.assertEqual(list(new["first_seen"].values()), [5.0])

    def test_the_record_holds_reasons_never_details(self):
        record, _ = D.persistence(REFUSED, {}, 0.0)
        self.assertNotIn("Fabric refused", json.dumps(record))

    def test_the_record_is_kept_across_runs_in_the_store(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw).resolve()
            with patch.object(D, "every_collector", return_value=REFUSED), \
                    patch.object(D.paths, "STATE", state):
                self.assertEqual(D.persistent(now=0.0), {})
                self.assertTrue((state / D.PERSISTENCE_FILE).is_file())
                self.assertEqual(list(D.persistent(now=25 * HOUR)), ["fabric-attribution.json"])
                (state / D.PERSISTENCE_FILE).write_text("{not json", encoding="utf-8")
                self.assertEqual(D.persistent(now=50 * HOUR), {}, "an unreadable record starts again")


class Finding(unittest.TestCase):
    def test_a_persistent_degradation_is_a_warning_with_its_reason_and_age(self):
        with patch("paths.config_file", side_effect=lambda name: ROOT / "defaults" / name):
            import build_findings as B
        rows = B.persistent_degradation_findings(
            {"fabric-attribution.json": [("fabric-hub — access-refused", 0.0)]}, now=30 * HOUR)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["type"], row["subject"], row["severity"]),
                         ("collector.degraded_persistent", "collector:fabric-attribution.json", "warning"))
        self.assertIn("access-refused", row["detail"])
        self.assertEqual(row["title_args"].get("hours"), "30")
        self.assertIn("store/raw/fabric-attribution.json", row["evidence"][0])


if __name__ == "__main__":
    unittest.main()
