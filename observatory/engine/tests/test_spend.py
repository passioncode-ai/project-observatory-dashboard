#!/usr/bin/env python3
"""The estate's month (OBS-56): collectors/scan_spend.py sums each stashed account's activity for the current UTC
month, never touching a value, and names an account it could not read; build_findings raises spend.estate_month at
the alert ratio and makes it critical at the limit.

Each project's month and last 30 days (J-11, OBS-93): the scan reads every key of the door's ledger by its hash and
sums it under the ledger row's project; a key with no project, and spend on hashes no ledger row claims, is its own
`unattributed` total and is never guessed into a project. A window that cannot be read whole is null, never a lower
figure. A key revoked or rotated inside a window keeps its spend with its project through the door's tombstone.
Every name of one registered project, every vault folder of it included, is one bucket keyed by its registry id.
The scan honours the step's deadline and an unreachable provider by naming what it did not read.
`tools/spend.py project [P] --json` answers from that file.
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
        import vault_project

        def activity(key):
            answer = answers[key]
            if isinstance(answer, int):
                raise urllib.error.HTTPError("https://openrouter.ai/api/v1/activity", answer, "x", {}, io.BytesIO())
            return answer

        def no_key_reads(admin, key_hash):
            raise AssertionError("the estate scan read a key by its hash with an empty ledger")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "spend.json"
            # Isolated: an empty ledger and no key read, so a direct run never sends a real hash anywhere.
            with patch.object(openrouter, "admins", lambda: [(k, Path(k)) for k in answers]), \
                    patch.object(openrouter, "read_admin", lambda label: (label, label)), \
                    patch.object(openrouter, "ledger", lambda: {"issued": {}}), \
                    patch.object(scan_spend, "activity", activity), \
                    patch.object(scan_spend, "key_activity", no_key_reads), \
                    patch.object(vault_project, "load_projects", lambda: []), \
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
        self.assertEqual(doc["degraded"], [{"source": "spend:openrouter:acct-b", "reason": "GET /activity HTTP 403",
                                            "scope": "account"}])
        self.assertNotIn("acct-b", json.dumps(doc["accounts"]))
        # The account not read leaves its residual unknown: the unattributed total is too.
        self.assertIsNone(doc["unattributed"]["month_usd"])

    def test_a_malformed_account_row_is_not_counted_and_the_scan_still_writes(self):
        doc = self.run_scan({"acct-a": [{"date": f"{MONTH}-01", "usage": 1}, {"date": f"{MONTH}-01", "usage": "x"},
                                        "not-a-row"]})
        self.assertEqual(doc["total_usd"], 1.0)
        self.assertTrue(doc["partial"])
        self.assertIn("2 activity row(s) not well formed", doc["degraded"][0]["reason"])


#: A fixed clock for the per-project windows: the month is October, the last 30 completed days run
#: 2026-09-14 .. 2026-10-13.
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
SEP, OCT = "2026-09-20 00:00:00", "2026-10-05 00:00:00"


def ledger_row(account: str, key_hash, project=None, **extra) -> dict:
    return {"account": account, "hash": key_hash, "project": project, "destination": "vault:x/prod/K",
            "limit_usd": 10, **extra}


def tomb(name, key_hash, project, day, reason="revoked", account="acct-a") -> dict:
    return {"name": name, "account": account, "hash": key_hash, "project": project, "retired_on": day,
            "reason": reason}


class ProjectSpend(unittest.TestCase):
    """Two projects, three keys, one account: per-project totals and what no project claims."""

    LEDGER = {"issued": {
        "alpha-agent": ledger_row("acct-a", "h-alpha", "alpha-web"),
        "beta-agent": ledger_row("acct-a", "h-beta", "beta-api"),
        "old-tool": ledger_row("acct-a", "h-claimed", None, claimed=True, destination="external:somewhere"),
    }}
    KEYS = {
        "h-alpha": [{"date": OCT, "usage": 2.0}, {"date": SEP, "usage": 1.0},
                    {"date": "2026-08-01 00:00:00", "usage": 50.0}],   # outside both windows
        "h-beta": [{"date": OCT, "usage": 0.5}],
        "h-claimed": [{"date": SEP, "usage": 0.25}],
    }
    # The account's own activity: the three keys plus 1.25 this month on a hash no ledger row holds.
    ACCOUNT = [{"date": OCT, "usage": 2.0 + 0.5 + 1.25}, {"date": SEP, "usage": 1.0 + 0.25}]

    def scan(self, ledger=None, keys=None, account=None, projects=None, remaining=None) -> dict:
        import openrouter
        import scan_spend
        import step_budget
        import vault_project
        keys = self.KEYS if keys is None else keys
        asked: list[str] = []

        def key_activity(admin, key_hash):
            asked.append(key_hash)
            answer = keys[key_hash]
            if isinstance(answer, BaseException):
                raise answer
            if isinstance(answer, int):
                raise RuntimeError(f"openrouter answered HTTP {answer}; provider response withheld")
            return answer

        def read_ledger():
            doc = self.LEDGER if ledger is None else ledger
            if isinstance(doc, Exception):
                raise doc
            return json.loads(json.dumps(doc))
        clock = iter(remaining) if remaining is not None else None
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "spend.json"
            with patch.object(openrouter, "admins", lambda: [("acct-a", Path("acct-a"))]), \
                    patch.object(openrouter, "read_admin", lambda label: (label, "admin-" + str(label))), \
                    patch.object(openrouter, "ledger", read_ledger), \
                    patch.object(scan_spend, "activity", lambda key: self.ACCOUNT if account is None else account), \
                    patch.object(scan_spend, "key_activity", key_activity), \
                    patch.object(scan_spend, "utcnow", lambda: NOW), \
                    patch.object(step_budget, "remaining",
                                 (lambda: next(clock, 0.0)) if clock else (lambda: float("inf"))), \
                    patch.object(vault_project, "load_projects", (lambda: None) if projects == "unreadable"
                                 else (lambda: projects or [])), \
                    patch("configuration.enabled", lambda name: True):
                self.assertEqual(scan_spend.main(["scan_spend.py", str(out)]), 0)
            doc = json.loads(out.read_text())
            doc["_asked"] = asked
            return doc

    def test_two_projects_get_their_month_and_last_30_days_and_the_rest_is_unattributed(self):
        doc = self.scan()
        self.assertEqual(doc["schema_version"], 2)
        self.assertEqual(doc["month"], "2026-10")
        self.assertEqual(doc["window"], {"last30_from": "2026-09-14", "last30_through": "2026-10-13"})
        self.assertEqual(doc["projects"]["alpha-web"],
                         {"month_usd": 2.0, "last30_usd": 3.0, "keys": 1, "retired_keys": 0,
                          "folders": ["alpha-web"], "through": "2026-10-05", "partial": False, "unread": []})
        beta = doc["projects"]["beta-api"]
        self.assertEqual((beta["month_usd"], beta["last30_usd"], beta["keys"], beta["partial"]),
                         (0.5, 0.5, 1, False))
        un = doc["unattributed"]
        # The claimed key (no project) and the 1.25 spent on a hash no ledger row holds.
        self.assertEqual((un["month_usd"], un["last30_usd"], un["keys"]), (1.25, 1.5, 1))
        self.assertEqual((un["unledgered_month_usd"], un["unledgered_last30_usd"]), (1.25, 1.25))
        self.assertFalse(un["partial"])
        self.assertFalse(doc["partial"])
        self.assertFalse(doc["attribution_partial"])
        # The estate's month is still the accounts' own figure, as before.
        self.assertEqual(doc["total_usd"], 3.75)
        self.assertEqual(doc["accounts"][0]["last30_usd"], 5.0)

    def test_a_revoked_key_keeps_its_spend_with_its_project(self):
        """The round-1 repro: k1 $2 and k2 $4 on one project, k2 revoked. Before the tombstone the project read
        $2 and whole, and the $4 went to unattributed."""
        ledger = {"issued": {"k1": ledger_row("acct-a", "h-k1", "alpha-web")},
                  "retired": [tomb("k2", "h-k2", "alpha-web", "2026-10-08")]}
        doc = self.scan(ledger=ledger, keys={"h-k1": [{"date": OCT, "usage": 2.0}], "h-k2": [{"date": OCT, "usage": 4.0}]},
                        account=[{"date": OCT, "usage": 6.0}])
        alpha = doc["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["partial"], alpha["keys"], alpha["retired_keys"]),
                         (6.0, False, 1, 1))
        self.assertEqual(doc["unattributed"]["month_usd"], 0.0)

    def test_the_door_leaves_the_tombstone_the_scan_reads(self):
        """End to end: `openrouter.revoke_key` on a real ledger file, then the scan on what it left."""
        import openrouter
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp).resolve() / "ledger.json"
            with patch.object(openrouter, "LEDGER", led), \
                    patch.object(openrouter, "read_admin", lambda label: (label, "admin")), \
                    patch.object(openrouter, "_request", lambda *a, **k: {"data": {"hash": "h-k2", "label": "x"}}), \
                    patch.object(openrouter, "_journal", lambda *a, **k: None), \
                    patch.object(openrouter.paths, "SCRATCH", Path(tmp).resolve()):
                openrouter.save_ledger({"issued": {"k1": ledger_row("acct-a", "h-k1", "alpha-web"),
                                                   "k2": ledger_row("acct-a", "h-k2", "alpha-web")}})
                openrouter.revoke_key("k2")
                left = json.loads(led.read_text())
        self.assertNotIn("k2", left["issued"])
        (stone,) = left["retired"]
        self.assertEqual({k: stone[k] for k in ("name", "hash", "project", "reason", "account")},
                         {"name": "k2", "hash": "h-k2", "project": "alpha-web", "reason": "revoked",
                          "account": "acct-a"})
        self.assertRegex(stone["retired_on"], r"^\d{4}-\d{2}-\d{2}$")
        stone["retired_on"] = "2026-10-08"        # inside the fixed clock's month
        doc = self.scan(ledger=left, keys={"h-k1": [{"date": OCT, "usage": 2.0}], "h-k2": [{"date": OCT, "usage": 4.0}]},
                        account=[{"date": OCT, "usage": 6.0}])
        self.assertEqual((doc["projects"]["alpha-web"]["month_usd"], doc["projects"]["alpha-web"]["partial"]),
                         (6.0, False))

    def test_a_completed_rotation_leaves_a_tombstone_for_its_predecessor(self):
        import openrouter
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp).resolve() / "ledger.json"

            def fake(path, key, payload=None, method=None):
                if path == "/keys/h-old" and method is None:
                    return {"data": {"name": "k1", "hash": "h-old"}}
                if path == "/keys" and payload:
                    return {"data": {"hash": "h-new", "label": "y"}, "key": "sk-or-v1-" + "n" * 40}
                return {"data": {}}
            with patch.object(openrouter, "LEDGER", led), \
                    patch.object(openrouter, "read_admin", lambda label: (label, "admin")), \
                    patch.object(openrouter, "_request", fake), \
                    patch.object(openrouter, "deliver", lambda value, to, **o: "slot"), \
                    patch.object(openrouter, "_journal", lambda *a, **k: None):
                openrouter.save_ledger({"issued": {"k1": ledger_row("acct-a", "h-old", "alpha-web", rotations=0)}})
                openrouter.rotate_one("k1")
                left = json.loads(led.read_text())
        row = left["issued"]["k1"]
        self.assertEqual((row["hash"], row["rotations"], row["tombstoned_rotations"]), ("h-new", 1, 1))
        self.assertEqual([(t["hash"], t["reason"], t["project"]) for t in left["retired"]],
                         [("h-old", "rotated", "alpha-web")])

    def test_a_tombstone_without_a_hash_nulls_the_windows_it_covers(self):
        ledger = {**self.LEDGER, "retired": [tomb("lost", None, "alpha-web", "2026-10-02", "forgotten-pending")]}
        alpha = self.scan(ledger=ledger)["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["last30_usd"], alpha["partial"]), (None, None, True))
        self.assertEqual(alpha["unread"][0]["key"], "lost")

    def test_a_tombstone_from_before_both_windows_is_not_read(self):
        ledger = {**self.LEDGER, "retired": [tomb("ancient", "h-ancient", "alpha-web", "2026-08-01")]}
        doc = self.scan(ledger=ledger)
        self.assertNotIn("h-ancient", doc["_asked"])
        self.assertEqual(doc["projects"]["alpha-web"]["retired_keys"], 0)

    def test_an_unreadable_key_makes_its_project_partial_and_never_a_lower_total(self):
        doc = self.scan(keys={**self.KEYS, "h-beta": 500})
        beta = doc["projects"]["beta-api"]
        self.assertIsNone(beta["month_usd"])
        self.assertIsNone(beta["last30_usd"])
        self.assertTrue(beta["partial"])
        self.assertEqual([u["key"] for u in beta["unread"]], ["beta-agent"])
        self.assertEqual(doc["projects"]["alpha-web"]["month_usd"], 2.0)
        # What no ledger row holds cannot be told apart from beta's unread spend either.
        self.assertIsNone(doc["unattributed"]["unledgered_month_usd"])
        self.assertIsNone(doc["unattributed"]["month_usd"])
        self.assertTrue(doc["unattributed"]["partial"])
        # The estate total is whole: one key the attribution could not read is not an account unread.
        self.assertFalse(doc["partial"])
        self.assertTrue(doc["attribution_partial"])
        self.assertIn(("spend:openrouter:key:beta-agent", "attribution"),
                      [(d["source"], d["scope"]) for d in doc["degraded"]])

    def test_malformed_key_rows_and_a_cut_body_make_the_key_unread(self):
        import http.client
        for answer in ([{"date": OCT, "usage": "lots"}], ["not-a-row"], http.client.IncompleteRead(b"x")):
            with self.subTest(answer=type(answer).__name__):
                doc = self.scan(keys={**self.KEYS, "h-beta": answer})
                self.assertIsNone(doc["projects"]["beta-api"]["month_usd"])
                self.assertEqual(doc["projects"]["alpha-web"]["month_usd"], 2.0)

    def test_a_pending_row_without_a_hash_is_unread_not_zero(self):
        ledger = json.loads(json.dumps(self.LEDGER))
        ledger["issued"]["beta-agent"].update(hash=None, pending=True)
        beta = self.scan(ledger=ledger)["projects"]["beta-api"]
        self.assertIsNone(beta["month_usd"])
        self.assertIn("no provider hash", beta["unread"][0]["reason"])

    def test_a_rotation_without_a_tombstone_nulls_the_windows_it_falls_in(self):
        ledger = json.loads(json.dumps(self.LEDGER))
        ledger["issued"]["alpha-agent"].update(rotated_on="2026-09-20", rotations=1)
        alpha = self.scan(ledger=ledger)["projects"]["alpha-web"]
        self.assertEqual(alpha["month_usd"], 2.0)
        self.assertIsNone(alpha["last30_usd"])
        self.assertTrue(alpha["partial"])
        self.assertEqual(alpha["unread"][0]["windows"], ["last30"])

    def test_two_rotations_one_untracked_still_nulls_the_windows_from_the_earlier(self):
        """Rotated on 09-20 before tombstones existed, again on 10-10 with one: the 10-10 predecessor is read, and
        the untracked one, on or before 09-20, still nulls every window that starts by then."""
        ledger = json.loads(json.dumps(self.LEDGER))
        ledger["issued"]["alpha-agent"].update(rotated_on="2026-10-10", rotations=2, tombstoned_rotations=1,
                                               tombstones_since="2026-09-20")
        ledger["retired"] = [tomb("alpha-agent", "h-pred", "alpha-web", "2026-10-10", "rotated")]
        doc = self.scan(ledger=ledger, keys={**self.KEYS, "h-pred": [{"date": OCT, "usage": 4.0}]},
                        account=[{"date": OCT, "usage": 2.0 + 0.5 + 1.25 + 4.0}, {"date": SEP, "usage": 1.25}])
        alpha = doc["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["last30_usd"]), (6.0, None))
        self.assertIn("h-pred", doc["_asked"])

    def test_every_rotation_tracked_reads_whole(self):
        ledger = json.loads(json.dumps(self.LEDGER))
        ledger["issued"]["alpha-agent"].update(rotated_on="2026-10-10", rotations=1, tombstoned_rotations=1,
                                               tombstones_since="2026-10-10")
        ledger["retired"] = [tomb("alpha-agent", "h-pred", "alpha-web", "2026-10-10", "rotated")]
        alpha = self.scan(ledger=ledger, keys={**self.KEYS, "h-pred": [{"date": SEP, "usage": 4.0}]},
                          account=[{"date": OCT, "usage": 3.75}, {"date": SEP, "usage": 5.25}])["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["last30_usd"], alpha["partial"]), (2.0, 7.0, False))

    def test_a_pending_rotation_counts_its_predecessor(self):
        ledger = json.loads(json.dumps(self.LEDGER))
        ledger["issued"]["alpha-agent"].update(
            rotated_on="2026-10-10", rotations=1, rotation_status="successor-delivered-predecessor-pending",
            predecessor_hash="h-old")
        alpha = self.scan(ledger=ledger, keys={**self.KEYS, "h-old": [{"date": OCT, "usage": 4.0}]},
                          account=[{"date": OCT, "usage": 2.0 + 0.5 + 1.25 + 4.0},
                                   {"date": SEP, "usage": 1.25}])["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["last30_usd"], alpha["partial"]), (6.0, 7.0, False))

    def test_the_deadline_stops_the_reads_and_names_what_was_not_reached(self):
        # The account read and the first key fit; the deadline arrives before the second key.
        doc = self.scan(remaining=[1e9, 1e9, 1.0])
        self.assertEqual(doc["_asked"], ["h-alpha"])
        self.assertEqual(doc["projects"]["alpha-web"]["month_usd"], 2.0)
        self.assertIsNone(doc["projects"]["beta-api"]["month_usd"])
        self.assertIn("deadline", doc["projects"]["beta-api"]["unread"][0]["reason"])
        self.assertIn("spend:deadline", [d["source"] for d in doc["degraded"]])
        self.assertEqual(doc["total_usd"], 3.75)

    def test_an_unreachable_provider_breaks_off_the_reads(self):
        import openrouter
        doc = self.scan(keys={**self.KEYS, "h-beta": openrouter.Unreachable("openrouter unreachable: URLError")})
        self.assertEqual(doc["_asked"], ["h-alpha", "h-beta"])     # nothing asked after the first unreachable
        self.assertIsNone(doc["projects"]["beta-api"]["month_usd"])
        self.assertIsNone(doc["unattributed"]["month_usd"])
        self.assertIn("unreachable", doc["unattributed"]["unread"][0]["reason"])

    def test_an_unreadable_ledger_attributes_nothing_and_says_so(self):
        doc = self.scan(ledger=RuntimeError("Issued-key ledger is unreadable; refusing to replace it"))
        self.assertEqual(doc["projects"], {})
        self.assertIsNone(doc["unattributed"]["month_usd"])
        self.assertFalse(doc["partial"])
        self.assertTrue(doc["attribution_partial"])
        self.assertIn("spend:openrouter:ledger", [d["source"] for d in doc["degraded"]])
        self.assertEqual(doc["total_usd"], 3.75)

    def test_one_project_named_two_ways_is_one_project(self):
        ledger = json.loads(json.dumps(self.LEDGER))
        ledger["issued"]["beta-agent"]["project"] = "project:alpha-web"
        registry = [{"id": "project:alpha-web", "local_folders": ["/work/alpha-web"]}]
        doc = self.scan(ledger=ledger, projects=registry)
        self.assertEqual(sorted(doc["projects"]), ["project:alpha-web"])
        alpha = doc["projects"]["project:alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["keys"], alpha["folders"]), (2.5, 2, ["alpha-web"]))

    def test_a_project_with_two_vault_folders_is_one_bucket(self):
        ledger = json.loads(json.dumps(self.LEDGER))
        ledger["issued"]["beta-agent"]["project"] = "alpha-site"
        registry = [{"id": "project:alpha-web", "local_folders": ["/work/alpha-web", "/work/alpha-site"]}]
        alpha = self.scan(ledger=ledger, projects=registry)["projects"]["project:alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["keys"], alpha["folders"]),
                         (2.5, 2, ["alpha-site", "alpha-web"]))

    def test_an_unreadable_registry_nulls_every_project(self):
        doc = self.scan(projects="unreadable")
        for name in ("alpha-web", "beta-api"):
            self.assertEqual((doc["projects"][name]["month_usd"], doc["projects"][name]["partial"]), (None, True))
        self.assertIn("spend:registry", [d["source"] for d in doc["degraded"]])
        self.assertEqual(doc["unattributed"]["month_usd"], 1.25)

    def test_no_hash_reaches_the_file(self):
        doc = self.scan(keys={**self.KEYS, "h-beta": 500})
        doc.pop("_asked")
        text = json.dumps(doc)
        for h in ("h-alpha", "h-beta", "h-claimed"):
            self.assertNotIn(h, text)


class ProjectCommand(unittest.TestCase):
    """`tools/spend.py project [P] --json`: the launcher's recentSpendUsd input, read from the scan."""

    def doc(self, **over) -> dict:
        return {"schema_version": 2, "scanned_at": NOW.isoformat(), "month": "2026-10",
                "window": {"last30_from": "2026-09-14", "last30_through": "2026-10-13"},
                "accounts": [], "total_usd": 3.75, "partial": False, "attribution_partial": True, "degraded": [],
                "projects": {"alpha-web": {"month_usd": 2.0, "last30_usd": 3.0, "keys": 1, "retired_keys": 1,
                                           "folders": ["alpha-web"], "through": "2026-10-13", "partial": False,
                                           "unread": []},
                             "beta-api": {"month_usd": None, "last30_usd": None, "keys": 1, "partial": True,
                                          "unread": [{"key": "beta-agent", "reason": "HTTP 500",
                                                      "windows": ["month", "last30"]}]}},
                "unattributed": {"month_usd": 1.25, "last30_usd": 1.5, "keys": 1, "unledgered_month_usd": 1.25,
                                 "unledgered_last30_usd": 1.25, "partial": False, "unread": []},
                **over}

    def run_cmd(self, argv, doc=None, projects=None) -> tuple[int, dict]:
        import spend
        import vault_project
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "spend.json"
            if doc is not None:
                f.write_text(json.dumps(doc))
            out = io.StringIO()
            with patch.object(spend, "utcnow", lambda: NOW), \
                    patch.object(vault_project, "load_projects",
                                 (lambda: None) if projects == "unreadable" else (lambda: projects or [])), \
                    patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                code = spend.main(["project", *argv, "--file", str(f), "--json"])
            return code, json.loads(out.getvalue())

    def test_one_project(self):
        code, ans = self.run_cmd(["alpha-web"], self.doc())
        self.assertEqual(code, 0)
        self.assertEqual((ans["project"], ans["monthUsd"], ans["last30Usd"], ans["partial"], ans["fresh"]),
                         ("alpha-web", 2.0, 3.0, False, True))
        self.assertEqual((ans["retiredKeys"], ans["through"], ans["folders"]), (1, "2026-10-13", ["alpha-web"]))
        self.assertEqual(ans["degraded"], [])

    def test_a_registry_id_or_any_folder_finds_the_whole_project(self):
        registry = [{"id": "project:alpha-web", "local_folders": ["/work/alpha-web", "/work/alpha-site"]}]
        doc = self.doc(projects={"project:alpha-web": {"month_usd": 2.5, "last30_usd": 3.5, "keys": 2,
                                                       "folders": ["alpha-site", "alpha-web"], "partial": False,
                                                       "unread": []}})
        for name in ("project:alpha-web", "alpha-web", "alpha-site"):
            with self.subTest(name=name):
                code, ans = self.run_cmd([name], doc, projects=registry)
                self.assertEqual((code, ans["project"], ans["monthUsd"]), (0, "project:alpha-web", 2.5))

    def test_a_scan_keyed_by_folder_is_still_found_by_the_registry_id(self):
        code, ans = self.run_cmd(["project:alpha-web"], self.doc(),
                                 projects=[{"id": "project:alpha-web", "local_folders": ["/work/alpha-web"]}])
        self.assertEqual((code, ans["monthUsd"]), (0, 2.0))

    def test_a_name_two_projects_claim_is_refused_never_picked(self):
        code, ans = self.run_cmd(["shared"], self.doc(), projects=[
            {"id": "project:alpha-web", "local_folders": ["/work/shared"]},
            {"id": "project:beta-api", "name": "shared", "local_folders": ["/work/beta-api"]}])
        self.assertEqual(code, 2)
        self.assertIn("ambiguous", ans["error"])
        self.assertNotIn("monthUsd", ans)

    def test_an_unreadable_registry_answers_null(self):
        for doc, projects in ((self.doc(), "unreadable"),
                              (self.doc(degraded=[{"source": "spend:registry", "reason": "x",
                                                   "scope": "attribution"}]), None)):
            with self.subTest(projects=projects):
                code, ans = self.run_cmd(["alpha-web"], doc, projects=projects)
                self.assertEqual(code, 0)
                self.assertEqual((ans["monthUsd"], ans["last30Usd"], ans["partial"]), (None, None, True))
                self.assertIn("spend:registry", [d["source"] for d in ans["degraded"]])

    def test_a_partial_project_is_null_with_the_key_named(self):
        code, ans = self.run_cmd(["beta-api"], self.doc())
        self.assertEqual(code, 0)
        self.assertIsNone(ans["monthUsd"])
        self.assertTrue(ans["partial"])
        self.assertEqual(ans["unread"][0]["key"], "beta-agent")

    def test_every_project_and_the_unattributed_total(self):
        code, ans = self.run_cmd([], self.doc())
        self.assertEqual(code, 0)
        self.assertEqual(sorted(ans["projects"]), ["alpha-web", "beta-api"])
        self.assertEqual(ans["unattributed"]["monthUsd"], 1.25)
        self.assertEqual(ans["totalUsd"], 3.75)

    def test_a_project_with_no_key_in_the_ledger_is_zero_and_says_why(self):
        code, ans = self.run_cmd(["gamma-cli"], self.doc())
        self.assertEqual((code, ans["monthUsd"], ans["keys"]), (0, 0.0, 0))
        self.assertIn("no key", ans["note"])

    def test_no_scan_is_degraded_and_exit_one(self):
        code, ans = self.run_cmd(["alpha-web"], None)
        self.assertEqual(code, 1)
        self.assertIsNone(ans["monthUsd"])
        self.assertEqual(ans["degraded"][0]["source"], "spend:scan")

    def test_a_scan_before_per_project_figures_is_degraded(self):
        old = self.doc(schema_version=1)
        old.pop("projects"), old.pop("unattributed")
        code, ans = self.run_cmd(["alpha-web"], old)
        self.assertEqual(code, 1)
        self.assertIsNone(ans["monthUsd"])

    def test_a_stale_scan_says_so(self):
        code, ans = self.run_cmd(["alpha-web"], self.doc(scanned_at="2026-10-12T00:00:00+00:00"))
        self.assertEqual(code, 0)
        self.assertFalse(ans["fresh"])
        self.assertIn("spend:stale", [d["source"] for d in ans["degraded"]])

    def test_an_unreadable_scan_partial_is_carried(self):
        code, ans = self.run_cmd(["alpha-web"], self.doc(partial=True, degraded=[
            {"source": "spend:openrouter:ledger", "reason": "unreadable"}]))
        self.assertEqual(code, 0)
        self.assertIn("spend:openrouter:ledger", [d["source"] for d in ans["degraded"]])


class EstateFinding(unittest.TestCase):
    def findings(self, spent: float, config: dict | None = None, **extra) -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            scratch, home = base / "raw", base / "ws"
            scratch.mkdir()
            (scratch / "spend.json").write_text(json.dumps({
                "month": MONTH, "scanned_at": datetime.now(timezone.utc).isoformat(), "total_usd": spent,
                "partial": False, "degraded": [], "accounts": [{"account": "acct-a", "usd": spent}], **extra}))
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

    def test_the_partial_detail_names_accounts_only(self):
        """A key the per-project figures could not read (scope attribution) is not an account the total missed."""
        (f,) = self.findings(900, partial=True, degraded=[
            {"source": "spend:openrouter:acct-b", "reason": "GET /activity HTTP 403", "scope": "account"},
            {"source": "spend:openrouter:key:beta-agent", "reason": "HTTP 500", "scope": "attribution"}])
        self.assertIn("spend:openrouter:acct-b", f["detail"])
        self.assertNotIn("beta-agent", f["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
