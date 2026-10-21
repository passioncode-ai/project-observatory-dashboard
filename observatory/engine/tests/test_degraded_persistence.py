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
        a = {"example.json": [{"source": "example:queue", "code": "queue-behind",
                               "reason": "after 732 of 4380 items"}]}
        b = {"example.json": [{"source": "example:queue", "code": "queue-behind",
                               "reason": "after 900 of 4380 items"}]}
        record, _ = D.persistence(a, {}, 0.0)
        _, persistent = D.persistence(b, record, 25 * HOUR)
        self.assertEqual(list(persistent), ["example.json"])

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


class Cadence(unittest.TestCase):
    """At the tick's own cadence the first sighting must hold: a record that stored `now` on every
    run would never reach a day (review of #328, L4)."""

    def test_every_thirty_minutes_for_a_day_and_an_hour(self):
        record, persistent, t = {}, {}, 0.0
        while t <= (D.PERSIST_HOURS + 1) * HOUR:
            record, persistent = D.persistence(REFUSED, record, t)
            t += HOUR / 2
        self.assertEqual(list(persistent), ["fabric-attribution.json"])
        self.assertEqual(persistent["fabric-attribution.json"][0][1], 0.0)


class Structural(unittest.TestCase):
    """Review of #328, M2: the info-level `collector.degraded` rows are mostly structural, and a day of
    them is no news. Those never become a pushed warning; anything else, a new collector's included,
    does, and the case this rule was built for still fires."""

    LIVE = {  # the shapes a 2026-10-21 workspace carried, names made synthetic
        "spend.json": [{"source": "spend:openrouter:key:example-a", "reason": "a retired key with no usage snapshot"}],
        "disk-guard.json": [{"source": "holders", "reason": "what processes hold could not be read; no path is removed"}],
        "git-hygiene.json": [{"source": "local:alpha-web", "reason": "registered checkout could not be read"},
                             {"source": "repository:example-org/beta-api", "reason": "branch classification unavailable"},
                             {"source": "worktree-registration", "reason": "registration budget exhausted"},
                             {"source": "local:gamma-app", "reason": "no default branch could be resolved: origin/HEAD is unset"},
                             {"source": "git-hygiene", "reason": "the step's time ran out after 88 of 146 checkouts; 61 carry"}],
        "local.json": [{"source": "folder:alpha-web", "reason": "remote: `git remote get-url` exited 128"}],
        "worktree-registration.json": [{"source": "worktree-registration", "reason": "registration budget exhausted"},
                                       {"source": "local:alpha-web", "reason": "worktree registration unavailable"}],
        "openrouter.json": [{"source": "openrouter:listing", "reason": "stopped after 12 pages"},
                            {"source": "openrouter:gateway", "reason": "its key is not among the newest 21200 keys the account lists, and the listing goes further"}],
        "leak-scan.json": [{"source": "leak-scan:deadline-targets", "code": "deadline-targets", "reason": "cut"},
                           {"source": "leak-scan:source-unreadable", "code": "source-unreadable", "reason": "holes"}],
    }

    def day(self, current):
        record, _ = D.persistence(current, {}, 0.0)
        return D.persistence(current, record, 25 * HOUR)[1]

    def test_structural_degradations_never_persist(self):
        self.assertEqual(self.day(self.LIVE), {})

    def test_the_access_refusal_still_fires_beside_them(self):
        self.assertEqual(list(self.day({**self.LIVE, **REFUSED})), ["fabric-attribution.json"])

    def test_a_real_failure_from_a_structural_source_still_fires(self):
        current = {"openrouter.json": [{"source": "openrouter:listing",
                                        "reason": "no provisioning key, so the account's keys cannot be listed at all"}],
                   "spend.json": [{"source": "spend:openrouter:key:example-a", "reason": "GET /activity: refused"}],
                   "disk-guard.json": [{"source": "holders", "reason": "something else entirely"}]}
        self.assertEqual(sorted(self.day(current)), ["disk-guard.json", "openrouter.json", "spend.json"])

    def test_a_new_collector_and_an_unclassified_source_fire_by_default(self):
        current = {"new-collector.json": [{"source": "provider", "reason": "every request refused"}],
                   "openrouter.json": [{"source": "openrouter:provisioning", "reason": "the key was refused"}]}
        self.assertEqual(sorted(self.day(current)), ["new-collector.json", "openrouter.json"])

    def test_a_row_marked_structural_at_its_source_does_not(self):
        self.assertEqual(self.day({"new-collector.json": [{"source": "x", "reason": "y", "structural": True}]}), {})

    def test_every_receipt_kept_off_the_push_channel_is_structural_whole(self):
        import notify_findings
        for kind in notify_findings.NOT_PUSHED:
            receipt = D.NOT_PUSHED_RECEIPTS.get(kind)
            self.assertIsNotNone(receipt, f"{kind} is not pushed: name the receipt it is built from")
            self.assertTrue(D.structural(receipt, {"source": "anything"}), receipt)


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
