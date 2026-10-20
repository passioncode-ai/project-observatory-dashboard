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
import threading
import time
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
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

        def activity(key, timeout=None):
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


def tomb(name, key_hash, project, day, reason="revoked", account="acct-a", by_day=None, reconciled=True,
         through=None, **extra) -> dict:
    """A tombstone as the door writes it: with a usage snapshot (`by_day`, reconciled or not) or without one."""
    out = {"name": name, "account": account, "hash": key_hash, "project": project, "retired_on": day,
           "reason": reason, "deleted": True, "read_at": f"{day}T10:00:00+00:00", **extra}
    if by_day is None:
        return {**out, "usage": "unread", "usage_error": "RuntimeError"}
    return {**out, "usage": "read", "usage_by_day": by_day, "reconciled": reconciled,
            "activity_through": through or (max(by_day) if by_day else None)}


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


#: The door's clock in the door tests: Wednesday 2026-10-14, noon UTC (the week began on Monday 10-12).
AT = None   # set below, once `utc` exists


def clock(*moments):
    """A stand-in for `openrouter.datetime` whose now() walks through `moments` (the last one sticks)."""
    import datetime as real
    import types
    seq = list(moments)

    class FakeDT(real.datetime):
        @classmethod
        def now(cls, tz=None):
            return seq.pop(0) if len(seq) > 1 else seq[0]
    return types.SimpleNamespace(datetime=FakeDT, timezone=real.timezone, timedelta=real.timedelta, date=real.date)


def provider_for(key_hash: str, rows: list, daily=0.0, monthly="sum", weekly="sum", name="k2", today="2026-10-14"):
    """A fake provider for one key: its activity rows, and its own counters. "sum" makes a counter agree with the
    rows of the current month or week (from Monday) plus `daily`; a number sets it; None leaves it out."""
    from datetime import date as d_
    t = d_.fromisoformat(today)
    monday = (t - timedelta(days=t.weekday())).isoformat()

    def agreed(first):
        return round(sum(r["usage"] for r in rows if first <= r["date"][:10] < today) + (daily or 0.0), 9)
    counters = {"usage_daily": daily, "usage_monthly": agreed(today[:8] + "01") if monthly == "sum" else monthly,
                "usage_weekly": agreed(monday) if weekly == "sum" else weekly}

    def provider(path, key, payload=None, method=None):
        if path == f"/keys/{key_hash}" and method is None:
            return {"data": {"name": name, "hash": key_hash,
                             **{k: v for k, v in counters.items() if v is not None}}}
        if path.startswith(f"/activity?api_key_hash={key_hash}"):
            return {"data": rows}
        return {"data": {}}
    return provider


def door_revoke(provider, when, row=None) -> tuple[list, dict]:
    """`openrouter.revoke_key("k2")` on a real ledger file holding k1 and k2, at `when`: (calls, the ledger)."""
    import openrouter
    calls: list = []

    def watched(path, key, payload=None, method=None):
        calls.append((method or "GET", path))
        return provider(path, key, payload, method)
    with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(when)):
        door = Door(tmp, {"k1": ledger_row("acct-a", "h-k1", "alpha-web"),
                          "k2": row or ledger_row("acct-a", "h-k2", "alpha-web")}, watched)
        try:
            openrouter.revoke_key("k2")
            left = door.ledger()
        finally:
            door.close()
    return calls, left


AT = utc(2026, 10, 14, 12, 0)


class Door:
    """`tools/openrouter.py` on a ledger file in a temporary folder, against a fake provider."""

    def __init__(self, tmp: str, issued: dict, provider):
        import openrouter
        self.m = openrouter
        self.led = Path(tmp).resolve() / "ledger.json"
        self.patches = [patch.object(openrouter, "LEDGER", self.led),
                        patch.object(openrouter, "read_admin", lambda label: (label, "admin")),
                        patch.object(openrouter, "_request", provider),
                        patch.object(openrouter, "deliver", lambda value, to, **o: "slot"),
                        patch.object(openrouter, "_journal", lambda *a, **k: None),
                        patch.object(openrouter.paths, "SCRATCH", Path(tmp).resolve())]
        for p_ in self.patches:
            p_.start()
        openrouter.save_ledger(issued if "issued" in issued else {"issued": issued})

    def ledger(self) -> dict:
        return json.loads(self.led.read_text())

    def close(self) -> None:
        for p_ in self.patches:
            p_.stop()


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
        timeouts: list = []

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
                    patch.object(scan_spend, "activity",
                                 lambda key, timeout=None: (timeouts.append(timeout),
                                                            self.ACCOUNT if account is None else account)[1]), \
                    patch.object(scan_spend, "key_activity", key_activity), \
                    patch.object(scan_spend, "utcnow", lambda: globals()["NOW"]), \
                    patch.object(step_budget, "remaining",
                                 (lambda: next(clock, 0.0)) if clock else (lambda: float("inf"))), \
                    patch.object(vault_project, "load_projects", (lambda: None) if projects == "unreadable"
                                 else (lambda: projects or [])), \
                    patch("configuration.enabled", lambda name: True):
                self.assertEqual(scan_spend.main(["scan_spend.py", str(out)]), 0)
            doc = json.loads(out.read_text())
            doc["_asked"] = asked
            doc["_timeouts"] = timeouts
            return doc

    def scan_at(self, now, **kw) -> dict:
        global NOW
        saved, NOW = NOW, now
        try:
            return self.scan(**kw)
        finally:
            NOW = saved

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
                  "retired": [tomb("k2", "h-k2", "alpha-web", "2026-10-08", by_day={"2026-10-05": 4.0})]}
        doc = self.scan(ledger=ledger, keys={"h-k1": [{"date": OCT, "usage": 2.0}]},
                        account=[{"date": OCT, "usage": 6.0}])
        alpha = doc["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["partial"], alpha["keys"], alpha["retired_keys"]),
                         (6.0, False, 1, 1))
        self.assertEqual(doc["unattributed"]["month_usd"], 0.0)
        # The deleted key is never asked for: the provider keeps no history of it (404, measured 2026-10-15).
        self.assertNotIn("h-k2", doc["_asked"])

    def test_after_its_snapshot_a_deleted_key_spends_nothing(self):
        """Retired on 10-08 with a snapshot through 10-08: the days after count 0, and the window is whole."""
        ledger = {"issued": {}, "retired": [tomb("k2", "h-k2", "alpha-web", "2026-10-08",
                                                 by_day={"2026-09-20": 1.0, "2026-10-08": 0.5})]}
        alpha = self.scan(ledger=ledger, keys={}, account=[{"date": SEP, "usage": 1.0},
                                                           {"date": "2026-10-08", "usage": 0.5}])["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["last30_usd"], alpha["partial"]), (0.5, 1.5, False))

    def test_an_unreconciled_snapshot_nulls_the_windows_holding_its_gap(self):
        ledger = {"issued": {}, "retired": [tomb("k2", "h-k2", "alpha-web", "2026-10-08",
                                                 by_day={"2026-09-20": 1.0}, reconciled=False)]}
        alpha = self.scan(ledger=ledger, keys={})["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["last30_usd"], alpha["partial"]), (None, None, True))

    def test_a_tombstone_without_a_snapshot_nulls_its_windows(self):
        ledger = {**self.LEDGER, "retired": [tomb("k2", "h-k2", "alpha-web", "2026-10-08")]}
        doc = self.scan(ledger=ledger)
        alpha = doc["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["last30_usd"], alpha["partial"]), (None, None, True))
        self.assertIn("no usage snapshot", alpha["unread"][0]["reason"])
        self.assertNotIn("h-k2", doc["_asked"])

    def test_a_hash_a_tombstone_and_a_live_row_share_is_not_read_as_zero(self):
        ledger = {**self.LEDGER, "retired": [tomb("old", "h-alpha", "alpha-web", "2026-10-08",
                                                  by_day={"2026-10-05": 2.0})]}
        doc = self.scan(ledger=ledger)
        alpha = doc["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["partial"]), (None, True))
        self.assertIn("held by another ledger entry", alpha["unread"][0]["reason"])
        self.assertNotIn("h-alpha", doc["_asked"])

    def test_a_ledger_entry_that_is_not_an_object_nulls_every_project(self):
        for ledger in ({"issued": {**self.LEDGER["issued"], "broken": "not-a-row"}},
                       {**self.LEDGER, "retired": "not-a-list"},
                       {**self.LEDGER, "retired": [42]}):
            with self.subTest(ledger=str(ledger)[-30:]):
                doc = self.scan(ledger=ledger)
                for name in ("alpha-web", "beta-api"):
                    self.assertIsNone(doc["projects"][name]["month_usd"])
                self.assertIsNone(doc["unattributed"]["month_usd"])
                self.assertIn("spend:openrouter:ledger", [d["source"] for d in doc["degraded"]])

    def test_the_account_read_is_cut_to_the_deadline(self):
        doc = self.scan(remaining=[1e9, 50.0, 1e9, 1e9, 1e9])
        self.assertEqual(doc["_timeouts"], [35.0])
        self.assertEqual(self.scan()["_timeouts"], [60.0])

    def test_the_door_snapshots_a_revoked_key_and_the_project_reads_whole(self):
        """Reconciled: the activity days plus usage_daily equal the provider's month and week counters."""
        rows = [{"date": OCT, "usage": 4.0}, {"date": "2026-10-13 00:00:00", "usage": 0.25}]
        calls, left = door_revoke(provider_for("h-k2", rows, daily=0.5), when=AT)
        stone = left["retired"][0]
        self.assertEqual({k: stone[k] for k in ("name", "hash", "project", "reason", "usage", "reconciled",
                                                "deleted", "retired_on")},
                         {"name": "k2", "hash": "h-k2", "project": "alpha-web", "reason": "revoked",
                          "usage": "read", "reconciled": True, "deleted": True, "retired_on": "2026-10-14"})
        self.assertEqual(stone["usage_by_day"], {"2026-10-05": 4.0, "2026-10-13": 0.25, "2026-10-14": 0.5})
        self.assertEqual((stone["usage_monthly"], stone["usage_weekly"], stone["usage_daily"]), (4.75, 0.75, 0.5))
        # The last request before the DELETE is the key's own row; the activity report came first.
        methods = [(m, p.split("?")[0]) for m, p in calls]
        # The whole exchange, in order: the row by hash, the activity report, the row again (its counters,
        # the last read), then the DELETE. Nothing else reads the key in between.
        self.assertEqual(methods, [("GET", "/keys/h-k2"), ("GET", "/activity"), ("GET", "/keys/h-k2"),
                                   ("DELETE", "/keys/h-k2")])
        doc = self.scan_at(utc(2026, 10, 16, 12), ledger=left, keys={"h-k1": [{"date": OCT, "usage": 2.0}]},
                           account=[{"date": OCT, "usage": 6.0}, {"date": "2026-10-13", "usage": 0.25},
                                    {"date": "2026-10-14", "usage": 0.5}])
        alpha = doc["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["partial"]), (6.75, False))
        self.assertNotIn("h-k2", doc["_asked"])

    def test_a_snapshot_that_does_not_reconcile_nulls_the_days_after_its_activity(self):
        """The activity report lags: 10-13 (3.0) is in the month counter but not yet in /activity."""
        rows = [{"date": "2026-10-10 00:00:00", "usage": 1.0}]
        _calls, left = door_revoke(provider_for("h-k2", rows, daily=0.1, monthly=4.1, weekly=3.1), when=AT)
        stone = left["retired"][0]
        self.assertEqual((stone["reconciled"], stone["activity_through"]), (False, "2026-10-10"))
        self.assertNotIn("2026-10-14", stone["usage_by_day"])          # today is not vouched for
        alpha = self.scan_at(utc(2026, 10, 16, 12), ledger=left, keys={"h-k1": []},
                             account=[{"date": "2026-10-10", "usage": 1.0}, {"date": "2026-10-13", "usage": 3.0},
                                      {"date": "2026-10-14", "usage": 0.1}])["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["last30_usd"], alpha["partial"]), (None, None, True))
        self.assertIn("did not reconcile", alpha["unread"][-1]["reason"])

    def test_a_snapshot_without_usage_daily_does_not_cover_today(self):
        """The month and week counters agree with the activity days, but today's own counter is missing."""
        rows = [{"date": OCT, "usage": 4.0}]
        _calls, left = door_revoke(provider_for("h-k2", rows, daily=None, monthly=4.0, weekly=0.0), when=AT)
        stone = left["retired"][0]
        self.assertFalse(stone["reconciled"])
        self.assertNotIn("2026-10-14", stone["usage_by_day"])
        alpha = self.scan_at(utc(2026, 10, 16, 12), ledger=left, keys={"h-k1": []},
                             account=[{"date": OCT, "usage": 4.0}])["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["partial"]), (None, True))

    def test_an_unreadable_snapshot_still_revokes_and_the_project_reads_null(self):
        def provider(path, key, payload=None, method=None):
            if path == "/keys/h-k2" and method is None:
                return {"data": {"name": "k2", "hash": "h-k2"}}
            if path.startswith("/activity"):
                raise RuntimeError("openrouter answered HTTP 500; provider response withheld")
            return {"data": {}}
        calls, left = door_revoke(provider, when=AT)
        self.assertNotIn("k2", left["issued"])
        self.assertIn(("DELETE", "/keys/h-k2"), calls)
        stone = left["retired"][0]
        self.assertEqual((stone["usage"], stone["usage_error"]), ("unread", "RuntimeError"))
        alpha = self.scan_at(utc(2026, 10, 16, 12), ledger=left, keys={"h-k1": [{"date": OCT, "usage": 2.0}]},
                             account=[{"date": OCT, "usage": 6.0}])["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["partial"]), (None, True))

    def test_a_failed_write_before_the_delete_deletes_nothing_and_a_retry_snapshots(self):
        """Round 4 P4: the snapshot is written to the row before the DELETE; when that write fails, no DELETE."""
        import openrouter
        provider = provider_for("h-k2", [{"date": OCT, "usage": 4.0}], daily=0.0)
        calls: list = []

        def watched(path, key, payload=None, method=None):
            calls.append((method or "GET", path))
            return provider(path, key, payload, method)
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            door = Door(tmp, {"k2": ledger_row("acct-a", "h-k2", "alpha-web")}, watched)
            try:
                with patch.object(openrouter, "save_ledger", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        openrouter.revoke_key("k2")
                self.assertNotIn(("DELETE", "/keys/h-k2"), calls)
                openrouter.revoke_key("k2")
                left = door.ledger()
            finally:
                door.close()
        self.assertEqual(left["retired"][0]["usage"], "read")

    def test_a_failure_after_the_delete_keeps_the_snapshot_for_the_retry(self):
        """The DELETE succeeded and the ledger write after it failed: the retry finds the key gone (404) and
        retires it with the snapshot written before the DELETE."""
        import openrouter
        state = {"deleted": False}
        base = provider_for("h-k2", [{"date": OCT, "usage": 4.0}], daily=0.0)

        def provider(path, key, payload=None, method=None):
            if path == "/keys/h-k2" and method == "DELETE":
                state["deleted"] = True
                return {"data": {}}
            if state["deleted"] and (path == "/keys/h-k2" or path.startswith("/activity")):
                raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
            return base(path, key, payload, method)
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            door = Door(tmp, {"k2": ledger_row("acct-a", "h-k2", "alpha-web")}, provider)
            try:
                real = openrouter.save_ledger
                writes = {"n": 0}

                def save_once_then_fail(doc):
                    writes["n"] += 1
                    if writes["n"] > 1:
                        raise OSError("disk full")
                    real(doc)
                with patch.object(openrouter, "save_ledger", save_once_then_fail):
                    with self.assertRaises(OSError):
                        openrouter.revoke_key("k2")
                self.assertTrue(state["deleted"])
                openrouter.revoke_key("k2")
                left = door.ledger()
            finally:
                door.close()
        stone = left["retired"][0]
        self.assertEqual((stone["usage"], stone["reconciled"]), ("read", True))
        self.assertNotIn("k2", left["issued"])

    def test_a_completed_rotation_leaves_a_tombstone_for_its_predecessor(self):
        import openrouter
        base = provider_for("h-old", [{"date": SEP, "usage": 1.0}], daily=0.0, name="k1")

        def provider(path, key, payload=None, method=None):
            if path == "/keys" and payload:
                return {"data": {"hash": "h-new", "label": "y"}, "key": "sk-or-v1-" + "n" * 40}
            return base(path, key, payload, method)
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            door = Door(tmp, {"k1": ledger_row("acct-a", "h-old", "alpha-web", rotations=1,
                                               rotated_on="2026-09-01")}, provider)
            try:
                door.m.rotate_one("k1")
                left = door.ledger()
            finally:
                door.close()
        row = left["issued"]["k1"]
        self.assertEqual((row["hash"], row["rotations"], row["tombstoned_rotations"], row["rotation_status"]),
                         ("h-new", 2, 1, "complete"))
        # The first tracked rotation records the day of the last untracked one, not today.
        self.assertEqual(row["tombstones_since"], "2026-09-01")
        self.assertNotIn("predecessor_snapshot", row)
        (stone,) = left["retired"]
        self.assertEqual((stone["hash"], stone["reason"], stone["project"], stone["usage"], stone["reconciled"]),
                         ("h-old", "rotated", "alpha-web", "read", True))
        self.assertNotIn("untracked_through", stone)            # the row carries its own horizon

    def test_a_failed_rename_after_the_delete_keeps_the_tombstone(self):
        """Round 4 P5: the predecessor is deleted, then the successor's rename fails."""
        import openrouter
        base = provider_for("h-old", [{"date": OCT, "usage": 4.0}], daily=0.0, name="k1")

        def provider(path, key, payload=None, method=None):
            if path == "/keys" and payload:
                return {"data": {"hash": "h-new", "label": "y"}, "key": "sk-or-v1-" + "n" * 40}
            if method == "PATCH":
                raise RuntimeError("openrouter answered HTTP 500; provider response withheld")
            return base(path, key, payload, method)
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            door = Door(tmp, {"k1": ledger_row("acct-a", "h-old", "alpha-web")}, provider)
            try:
                with self.assertRaises(RuntimeError):
                    door.m.rotate_one("k1")
                left = door.ledger()
            finally:
                door.close()
        self.assertEqual([(t["hash"], t["usage"]) for t in left["retired"]], [("h-old", "read")])
        self.assertEqual(left["issued"]["k1"]["rotation_status"], "predecessor-retired-rename-pending")

    def test_revoking_a_row_with_an_untracked_rotation_keeps_its_project_null(self):
        """Round 4 P10: the row's horizon goes with it into the tombstone, even with a reconciled snapshot."""
        rows = [{"date": "2026-10-08 00:00:00", "usage": 1.0}]
        row = ledger_row("acct-a", "h-k2", "alpha-web", rotations=1, rotated_on="2026-10-05")
        _calls, left = door_revoke(provider_for("h-k2", rows, daily=0.0), when=AT, row=row)
        stone = left["retired"][0]
        self.assertEqual((stone["reconciled"], stone["untracked_through"]), (True, "2026-10-05"))
        doc = self.scan_at(utc(2026, 10, 16, 12), ledger=left, keys={"h-k1": []},
                           account=[{"date": "2026-10-03", "usage": 7.0}, {"date": "2026-10-08", "usage": 1.0}])
        self.assertEqual((doc["projects"]["alpha-web"]["month_usd"], doc["projects"]["alpha-web"]["partial"]),
                         (None, True))
        self.assertIn("spend:openrouter:key:k2", [d["source"] for d in doc["degraded"]])

    def test_revoking_a_row_mid_rotation_keeps_its_project_null(self):
        """Round 4 P9: a rotation left predecessor_hash; revoking the successor carries it into the tombstone."""
        rows = [{"date": "2026-10-08 00:00:00", "usage": 1.0}]
        row = ledger_row("acct-a", "h-k2", "alpha-web", rotations=1, rotated_on="2026-10-09",
                         tombstoned_rotations=0, predecessor_hash="h-pred",
                         rotation_status="successor-delivered-predecessor-pending")
        _calls, left = door_revoke(provider_for("h-k2", rows, daily=0.0), when=AT, row=row)
        stone = left["retired"][0]
        self.assertEqual((stone["predecessor_hash"], stone["predecessor_usage"]), ("h-pred", "unread"))
        alpha = self.scan_at(utc(2026, 10, 16, 12), ledger=left, keys={"h-k1": []},
                             account=[{"date": "2026-10-03", "usage": 7.0},
                                      {"date": "2026-10-08", "usage": 1.0}])["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["partial"]), (None, True))

    def test_an_issue_over_a_row_with_a_hash_keeps_that_hash_as_a_tombstone(self):
        """The provider has no key by the name, and the row's key answers 404: its hash is kept as a tombstone,
        deleted, with no snapshot (no revoke kept one), so the project reads null while it is kept."""
        import openrouter

        def provider(path, key, payload=None, method=None):
            if path.startswith("/keys?") or path.startswith("/keys/h-gone"):
                if path.startswith("/keys/h-gone"):
                    raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
                return {"data": []}
            if path == "/keys" and payload:
                return {"data": {"hash": "h-fresh", "label": "z"}, "key": "sk-or-v1-" + "f" * 40}
            return {"data": {}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            door = Door(tmp, {"k2": ledger_row("acct-a", "h-gone", "alpha-web")}, provider)
            try:
                with patch.object(openrouter, "vault_destination", lambda to: to):
                    door.m.issue_key("k2", 5.0, "acct-a", "observatory", "alpha-web")
                left = door.ledger()
            finally:
                door.close()
        self.assertEqual(left["issued"]["k2"]["hash"], "h-fresh")
        (stone,) = left["retired"]
        self.assertEqual((stone["hash"], stone["reason"], stone["deleted"]), ("h-gone", "replaced-by-issue", True))
        alpha = self.scan_at(utc(2026, 10, 16, 12), ledger=left, keys={"h-fresh": []},
                             account=[])["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["partial"]), (None, True))

    def test_a_forgotten_pending_row_leaves_a_tombstone_without_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            door = Door(tmp, {"k3": ledger_row("acct-a", None, "alpha-web", pending=True)},
                        lambda *a, **k: {"data": {}})
            try:
                door.m.forget_pending_key("k3", "deleted at the provider by name")
                left = door.ledger()
            finally:
                door.close()
        (stone,) = left["retired"]
        self.assertEqual((stone["name"], stone["hash"], stone["reason"], stone["usage"]),
                         ("k3", None, "forgotten-pending", "unread"))

    def test_tombstones_are_kept_62_days_and_unreadable_ones_always(self):
        import openrouter
        today = datetime.now(timezone.utc).date()
        doc = {"issued": {}, "retired": [
            {"name": "d31", "retired_on": (today - timedelta(days=31)).isoformat()},   # the windows still need it
            {"name": "d61", "retired_on": (today - timedelta(days=61)).isoformat()},
            {"name": "d62", "retired_on": (today - timedelta(days=62)).isoformat()},
            {"name": "d63", "retired_on": (today - timedelta(days=63)).isoformat()},
            {"name": "no-day", "hash": "h-x", "project": "alpha-web"},                  # round 4 P6
            {"name": "bad-day", "retired_on": "someday"},
            "not-an-object"]}
        openrouter._retire(doc, "new", {"account": "acct-a", "project": "alpha-web"}, "h-new", "revoked")
        self.assertEqual([r if isinstance(r, str) else r["name"] for r in doc["retired"]],
                         ["d31", "d61", "d62", "no-day", "bad-day", "not-an-object", "new"])

    def test_a_retired_list_that_is_not_a_list_is_refused_before_any_delete(self):
        """The fake holds the key, so only the refusal stands between the call and a DELETE (round 4, M6)."""
        calls: list = []
        provider = provider_for("h-k2", [{"date": OCT, "usage": 1.0}], daily=0.0)

        def watched(path, key, payload=None, method=None):
            calls.append(method)
            return provider(path, key, payload, method)
        with tempfile.TemporaryDirectory() as tmp:
            door = Door(tmp, {"issued": {"k2": ledger_row("acct-a", "h-k2", "alpha-web")}, "retired": {"x": 1}},
                        watched)
            try:
                with self.assertRaises(RuntimeError):
                    door.m.revoke_key("k2")
            finally:
                door.close()
        self.assertNotIn("DELETE", calls)

    def rotate_two_at_once(self, first, second) -> dict:
        """Two rotations started together: `first(door)` and, 50 ms later, `second(door)`."""
        import threading
        import time

        def provider(path, key, payload=None, method=None):
            if path == "/keys" and payload:
                time.sleep(0.3)                     # both rotations are mid-flight together
                return {"data": {"hash": "h-" + payload["name"]}, "key": "sk-or-v1-" + "n" * 40}
            if path.startswith("/keys/h-") and method is None and "-next" not in path:
                return {"data": {"name": path.split("/keys/h-")[1], "hash": path.split("/keys/")[1]}}
            if path.startswith("/activity"):
                return {"data": []}
            return {"data": {}}
        with tempfile.TemporaryDirectory() as tmp:
            door = Door(tmp, {"k1": ledger_row("acct-a", "h-k1", "alpha-web"),
                              "k2": ledger_row("acct-a", "h-k2", "beta-api")}, provider)
            try:
                errors: list = []

                def run(fn, delay):
                    time.sleep(delay)
                    try:
                        fn(door)
                    except Exception as exc:        # noqa: BLE001 - reported below
                        errors.append(exc)
                threads = [threading.Thread(target=run, args=(first, 0.0)),
                           threading.Thread(target=run, args=(second, 0.05))]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(30)
                left = door.ledger()
            finally:
                door.close()
        self.assertEqual(errors, [])
        return left

    def test_two_rotations_at_once_both_land(self):
        """Each rotation reads the ledger, calls the provider and writes the ledger whole; without the ledger
        lock the second write dropped the first one's successor."""
        left = self.rotate_two_at_once(lambda d: d.m.rotate_one("k2"), lambda d: d.m.rotate_one("k1"))
        self.assertEqual({n: left["issued"][n]["hash"] for n in ("k1", "k2")},
                         {"k1": "h-k1-next", "k2": "h-k2-next"})
        self.assertEqual(sorted(t["hash"] for t in left["retired"]), ["h-k1", "h-k2"])

    def test_the_rotate_command_reads_the_ledger_under_the_lock(self):
        """Round 4 M14: `cmd_rotate` reads the ledger before it waits for the lock; passing that copy to the
        rotation would write back the ledger as it was before the other rotation landed."""
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            left = self.rotate_two_at_once(lambda d: d.m.rotate_one("k2"), lambda d: d.m.cmd_rotate("k1", False))
        self.assertEqual({n: left["issued"][n]["hash"] for n in ("k1", "k2")},
                         {"k1": "h-k1-next", "k2": "h-k2-next"})

    def test_the_lock_refusal_names_every_locked_verb(self):
        import openrouter
        import oslocks
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp).resolve() / "ledger.json"
            with patch.object(openrouter, "LEDGER", led), patch.object(openrouter, "POLICY_LOCK_WAIT_S", 0.25):
                fd = os.open(led.parent / ".openrouter-policy.lock", os.O_RDWR | os.O_CREAT, 0o600)
                result: list = []

                def other():           # another thread's open file description holds it
                    try:
                        with openrouter._ledger_lock():
                            pass
                    except RuntimeError as exc:
                        result.append(str(exc))
                try:
                    oslocks.flock(fd, oslocks.LOCK_EX)
                    import threading
                    t = threading.Thread(target=other)
                    t.start()
                    t.join(10)
                finally:
                    oslocks.flock(fd, oslocks.LOCK_UN)
                    os.close(fd)
        self.assertEqual(len(result), 1)
        self.assertIn("for 0.2 s", result[0])
        for verb in openrouter.LOCKED_VERBS:
            self.assertIn(verb, result[0])
        self.assertGreaterEqual(openrouter.POLICY_LOCK_WAIT_S, 120.0)

    def test_a_tombstone_without_a_hash_nulls_the_windows_it_covers(self):
        ledger = {**self.LEDGER, "retired": [tomb("lost", None, "alpha-web", "2026-10-02", "forgotten-pending")]}
        alpha = self.scan(ledger=ledger)["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["last30_usd"], alpha["partial"]), (None, None, True))
        self.assertEqual(alpha["unread"][0]["key"], "lost")

    def test_a_tombstone_from_before_both_windows_is_not_read(self):
        ledger = {**self.LEDGER, "retired": [tomb("ancient", "h-ancient", "alpha-web", "2026-08-01",
                                                  by_day={"2026-08-01": 9.0})]}
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
        ledger["retired"] = [tomb("alpha-agent", "h-pred", "alpha-web", "2026-10-10", "rotated",
                                  by_day={"2026-10-05": 4.0})]
        doc = self.scan(ledger=ledger, account=[{"date": OCT, "usage": 2.0 + 0.5 + 1.25 + 4.0},
                                                {"date": SEP, "usage": 1.25}])
        alpha = doc["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["last30_usd"]), (6.0, None))
        self.assertIn("before tombstones existed", " ".join(d["reason"] for d in doc["degraded"]
                                                            if d["source"] == "spend:openrouter:key:alpha-agent"))

    def test_every_rotation_tracked_reads_whole(self):
        ledger = json.loads(json.dumps(self.LEDGER))
        ledger["issued"]["alpha-agent"].update(rotated_on="2026-10-10", rotations=1, tombstoned_rotations=1,
                                               tombstones_since="2026-10-10")
        ledger["retired"] = [tomb("alpha-agent", "h-pred", "alpha-web", "2026-10-10", "rotated",
                                  by_day={"2026-09-20": 4.0})]
        alpha = self.scan(ledger=ledger, account=[{"date": OCT, "usage": 3.75},
                                                  {"date": SEP, "usage": 5.25}])["projects"]["alpha-web"]
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
        # The account read (its check and its timeout) and the first key fit; the deadline arrives before the second.
        doc = self.scan(remaining=[1e9, 1e9, 1e9, 1.0])
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


class RoundFourProbes(unittest.TestCase):
    """The adversarial probes of verification round 4 on #285 (P1-P10), kept as regression tests: each one is a
    way a retired key's spend could be lost, counted twice, or hidden behind a whole, lower figure."""
    def scan_at(self, now, **kw):
        return ProjectSpend().scan_at(now, **kw)

    def test_P1_activity_lag_yesterday_silently_lost(self):
        """Revoke at 00:30 UTC; /activity for the key does not hold yesterday yet (the lag the scan's own
        `through` documents). usage_daily is read, so covers_through = today and the scan reads the project whole."""
        import openrouter

        def provider(path, key, payload=None, method=None):
            if path == "/keys/h-k2" and method is None:
                return {"data": {"name": "k2", "hash": "h-k2", "usage_daily": 0.1}}
            if path.startswith("/activity?api_key_hash=h-k2"):
                return {"data": [{"date": "2026-10-10 00:00:00", "usage": 1.0}]}   # 10-14 (3.0) not yet reported
            return {"data": {}}
        with tempfile.TemporaryDirectory() as tmp:
            door = Door(tmp, {"k1": ledger_row("acct-a", "h-k1", "alpha-web"),
                              "k2": ledger_row("acct-a", "h-k2", "alpha-web")}, provider)
            try:
                with patch.object(openrouter, "datetime", clock(utc(2026, 10, 15, 0, 30))):
                    openrouter.revoke_key("k2")
                left = door.ledger()
            finally:
                door.close()
        stone = left["retired"][0]
        doc = self.scan_at(utc(2026, 10, 16, 12), ledger=left, keys={"h-k1": [{"date": "2026-10-05 00:00:00", "usage": 2.0}]},
                           account=[{"date": "2026-10-05 00:00:00", "usage": 2.0}, {"date": "2026-10-10 00:00:00", "usage": 1.0},
                                    {"date": "2026-10-14 00:00:00", "usage": 3.0}, {"date": "2026-10-15 00:00:00", "usage": 0.1}])
        alpha = doc["projects"]["alpha-web"]
        # True spend of alpha: 2.0 + 1.0 + 3.0 + 0.1 = 6.1. A defect-free scan says 6.1 or null.
        self.assertTrue(alpha["month_usd"] is None or abs(alpha["month_usd"] - 6.1) < 1e-6,
                        f"silent undercount: {alpha['month_usd']} partial={alpha['partial']}")

    def test_P2_rotate_snapshot_today_from_stale_row_crossing_midnight(self):
        """rotate reads the predecessor's row (usage_daily of 10-14) before mint+delivery; the snapshot is stamped
        after delivery, past midnight, so 10-14's usage_daily lands on 10-15 while /activity already holds 10-14."""
        import openrouter

        def provider(path, key, payload=None, method=None):
            if path == "/keys/h-old" and method is None:
                return {"data": {"name": "k1", "hash": "h-old", "usage_daily": 5.0}}
            if path.startswith("/activity?api_key_hash=h-old"):
                return {"data": [{"date": "2026-10-14 00:00:00", "usage": 5.0}]}
            if path == "/keys" and payload:
                return {"data": {"hash": "h-new", "label": "y"}, "key": "sk-or-v1-" + "n" * 40}
            return {"data": {}}
        with tempfile.TemporaryDirectory() as tmp:
            door = Door(tmp, {"k1": ledger_row("acct-a", "h-old", "alpha-web")}, provider)
            try:
                # today() (rotated_on) at 23:59:50, the snapshot after a 15 s delivery, _retire after it.
                with patch.object(openrouter, "datetime", clock(utc(2026, 10, 14, 23, 59, 50), utc(2026, 10, 15, 0, 0, 5))):
                    openrouter.rotate_one("k1")
                left = door.ledger()
            finally:
                door.close()
        stone = left["retired"][0]
        doc = self.scan_at(utc(2026, 10, 16, 12), ledger=left, keys={"h-new": []},
                           account=[{"date": "2026-10-14 00:00:00", "usage": 5.0}])
        alpha = doc["projects"]["alpha-web"]
        self.assertTrue(alpha["month_usd"] is None or abs(alpha["month_usd"] - 5.0) < 1e-6,
                        f"double count: {alpha['month_usd']} partial={alpha['partial']}")

    def test_P3_rotate_stale_usage_daily_misses_spend_during_delivery(self):
        import openrouter
        reads = {"n": 0}

        def provider(path, key, payload=None, method=None):
            if path == "/keys/h-old" and method is None:
                reads["n"] += 1
                return {"data": {"name": "k1", "hash": "h-old", "usage_daily": 0.2 if reads["n"] == 1 else 0.9}}
            if path.startswith("/activity?api_key_hash=h-old"):
                return {"data": []}
            if path == "/keys" and payload:
                return {"data": {"hash": "h-new", "label": "y"}, "key": "sk-or-v1-" + "n" * 40}
            return {"data": {}}
        with tempfile.TemporaryDirectory() as tmp:
            door = Door(tmp, {"k1": ledger_row("acct-a", "h-old", "alpha-web")}, provider)
            try:
                openrouter.rotate_one("k1")
                left = door.ledger()
            finally:
                door.close()
        stone = left["retired"][0]
        self.assertGreater(reads["n"], 1, "the predecessor's usage_daily was not re-read before the DELETE")

    def test_P4_revoke_crash_after_delete_loses_snapshot(self):
        import openrouter
        state = {"deleted": False}

        def provider(path, key, payload=None, method=None):
            if path == "/keys/h-k2" and method == "DELETE":
                state["deleted"] = True
                return {"data": {}}
            if path == "/keys/h-k2" and method is None:
                if state["deleted"]:
                    raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
                return {"data": {"name": "k2", "hash": "h-k2", "usage_daily": 0.5}}
            if path.startswith("/activity?api_key_hash=h-k2"):
                if state["deleted"]:
                    raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
                return {"data": [{"date": "2026-10-05 00:00:00", "usage": 4.0}]}
            return {"data": {}}
        with tempfile.TemporaryDirectory() as tmp:
            door = Door(tmp, {"k2": ledger_row("acct-a", "h-k2", "alpha-web")}, provider)
            try:
                real = openrouter.save_ledger
                with patch.object(openrouter, "save_ledger", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        openrouter.revoke_key("k2")
                mid = door.ledger()
                openrouter.revoke_key("k2")     # the documented retry
                left = door.ledger()
            finally:
                door.close()
        self.assertEqual(left["retired"][0]["usage"], "read", "the snapshot read before the DELETE was lost")

    def test_P5_rotate_patch_failure_after_delete_loses_snapshot(self):
        import openrouter

        def provider(path, key, payload=None, method=None):
            if path == "/keys/h-old" and method is None:
                return {"data": {"name": "k1", "hash": "h-old", "usage_daily": 0.0}}
            if path.startswith("/activity?api_key_hash=h-old"):
                return {"data": [{"date": "2026-10-05 00:00:00", "usage": 4.0}]}
            if path == "/keys" and payload:
                return {"data": {"hash": "h-new", "label": "y"}, "key": "sk-or-v1-" + "n" * 40}
            if method == "PATCH":
                raise RuntimeError("openrouter answered HTTP 500; provider response withheld")
            return {"data": {}}
        with tempfile.TemporaryDirectory() as tmp:
            door = Door(tmp, {"k1": ledger_row("acct-a", "h-old", "alpha-web")}, provider)
            try:
                with self.assertRaises(RuntimeError):
                    openrouter.rotate_one("k1")
                left = door.ledger()
            finally:
                door.close()
        rec = left["issued"]["k1"]
        self.assertTrue(left.get("retired"), "predecessor deleted, its snapshot kept nowhere")

    def test_P6_prune_drops_a_tombstone_without_retired_on(self):
        import openrouter
        doc = {"issued": {}, "retired": [{"name": "x", "hash": "h-x", "project": "alpha-web", "usage": "read",
                                          "usage_by_day": {"2026-10-05": 9.0}}]}
        openrouter._retire(doc, "new", {"account": "a", "project": "p"}, "h-n", "revoked")
        self.assertIn("x", [r.get("name") for r in doc["retired"]])

    @unittest.skipIf(os.name == "nt", "the holder process takes the lock with fcntl")
    def test_P7_lock_threads_and_reentrancy_and_processes(self):
        import openrouter
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(openrouter, "LEDGER", Path(tmp) / "l.json"), patch.object(openrouter, "POLICY_LOCK_WAIT_S", 0.5):
                order = []
                def a():
                    with openrouter._ledger_lock():
                        with openrouter._ledger_lock():      # re-entrant
                            order.append("a-in"); time.sleep(0.3); order.append("a-out")
                def b():
                    time.sleep(0.05)
                    with openrouter._ledger_lock():
                        order.append("b-in")
                ts_ = [threading.Thread(target=a), threading.Thread(target=b)]
                [t.start() for t in ts_]; [t.join() for t in ts_]
                self.assertEqual(order, ["a-in", "a-out", "b-in"])
                # another process holds it: we refuse after the wait
                lockf = Path(tmp) / ".openrouter-policy.lock"
                holder = subprocess.Popen([sys.executable, "-c",
                    "import fcntl,os,sys,time; fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT, 0o600); fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(3)", str(lockf)],
                    stdout=subprocess.PIPE, text=True)
                holder.stdout.readline()
                t0 = time.monotonic()
                with self.assertRaises(RuntimeError) as cm:
                    with openrouter._ledger_lock():
                        pass
                holder.kill(); holder.wait(); holder.stdout.close()
                self.assertEqual(getattr(openrouter._LOCK_STATE, "depth", 0), 0)

    def test_P8_no_value_in_tombstone_or_degraded(self):
        import openrouter
        value = "sk-or-v1-" + "s" * 64

        def provider(path, key, payload=None, method=None):
            if path == "/keys/h-old" and method is None:
                return {"data": {"name": "k1", "hash": "h-old", "usage_daily": 0.0, "key": value}}
            if path.startswith("/activity?api_key_hash=h-old"):
                raise RuntimeError("openrouter answered HTTP 500; provider response withheld")
            if path == "/keys" and payload:
                return {"data": {"hash": "h-new", "label": "y", "key": value}, "key": value}
            return {"data": {}}
        with tempfile.TemporaryDirectory() as tmp:
            door = Door(tmp, {"k1": ledger_row("acct-a", "h-old", "alpha-web")}, provider)
            try:
                openrouter.rotate_one("k1")
                text = door.led.read_text()
            finally:
                door.close()
        self.assertNotIn(value, text)
        self.assertNotIn("admin", json.loads(text)["retired"][0].get("usage_error", ""))



class RoundFourProbesRevoke(unittest.TestCase):
    scan_at = RoundFourProbes.scan_at

    def revoke(self, row):
        import openrouter

        def provider(path, key, payload=None, method=None):
            if path == "/keys/h-cur" and method is None:
                return {"data": {"name": "k1", "hash": "h-cur", "usage_daily": 0.0}}
            if path.startswith("/activity?api_key_hash=h-cur"):
                return {"data": [{"date": "2026-10-08 00:00:00", "usage": 1.0}]}
            return {"data": {}}
        with tempfile.TemporaryDirectory() as tmp:
            door = Door(tmp, {"k1": row}, provider)
            try:
                with patch.object(openrouter, "datetime", clock(utc(2026, 10, 10, 12))):
                    before = self.scan_at(utc(2026, 10, 10, 12), ledger=door.ledger(),
                                          keys={"h-cur": [{"date": "2026-10-08 00:00:00", "usage": 1.0}], "h-pred": 404},
                                          account=[{"date": "2026-10-03 00:00:00", "usage": 7.0},
                                                   {"date": "2026-10-08 00:00:00", "usage": 1.0}])
                    openrouter.revoke_key("k1")
                left = door.ledger()
            finally:
                door.close()
        after = self.scan_at(utc(2026, 10, 14, 12), ledger=left, keys={},
                             account=[{"date": "2026-10-03 00:00:00", "usage": 7.0}, {"date": "2026-10-08 00:00:00", "usage": 1.0}])
        return before["projects"]["alpha-web"], after["projects"]["alpha-web"], after

    def test_P10_revoking_a_row_with_an_untracked_rotation_unhides_a_lower_total(self):
        """Rotated 10-05 before tombstones existed (predecessor spent 7.0 on 10-03); the scan nulls alpha.
        Revoke the row: the guard leaves with it, and alpha reads 1.0, whole."""
        b, a, doc = self.revoke(ledger_row("acct-a", "h-cur", "alpha-web", rotations=1, rotated_on="2026-10-05"))
        self.assertTrue(a["month_usd"] is None, f"lower figure marked whole: {a['month_usd']}")

    def test_P9_revoking_a_row_mid_rotation_drops_the_predecessor(self):
        """A rotation left predecessor_hash (h-pred, already deleted, spent 7.0 on 10-03); revoke as cleanup."""
        b, a, doc = self.revoke(ledger_row("acct-a", "h-cur", "alpha-web", rotations=1, rotated_on="2026-10-09",
                                           tombstoned_rotations=0, predecessor_hash="h-pred",
                                           rotation_status="successor-delivered-predecessor-pending"))
        self.assertTrue(a["month_usd"] is None, f"lower figure marked whole: {a['month_usd']}")


class Clock:
    """A clock the fake provider can move: `now()` answers `t`, and a provider may set `t` mid-call."""

    def __init__(self, t):
        self.t = t

    def module(self):
        import datetime as real
        import types
        outer = self

        class FakeDT(real.datetime):
            @classmethod
            def now(cls, tz=None):
                return outer.t
        return types.SimpleNamespace(datetime=FakeDT, timezone=real.timezone, timedelta=real.timedelta,
                                     date=real.date)


def snapshot_of(rows, when, today, **counters):
    """`openrouter.usage_snapshot` of one key, at `when`, against `provider_for(rows, today=today, **counters)`."""
    import openrouter
    provider = provider_for("h-k2", rows, today=today, **counters)
    with patch.object(openrouter, "_request", provider), patch.object(openrouter, "datetime", clock(when)):
        return openrouter.usage_snapshot("admin", "h-k2")


def row(day: str, usd: float) -> dict:
    return {"date": f"{day} 00:00:00", "usage": usd}


class Reconciliation(unittest.TestCase):
    """One case per clause of the rule that makes a usage snapshot whole (`openrouter.usage_snapshot`), so that
    dropping or loosening any clause turns a case red (round 5, R5-2). The door's clock is Wednesday 2026-10-14;
    the week began on Monday 10-12 and the month on 10-01."""

    ROWS = [row("2026-10-05", 4.0), row("2026-10-12", 0.25), row("2026-10-13", 0.5)]

    def snap(self, rows=None, when=None, today="2026-10-14", **counters) -> dict:
        return snapshot_of(self.ROWS if rows is None else rows, when or AT, today, **counters)

    def test_counters_that_agree_reconcile(self):
        s = self.snap(daily=0.1)
        self.assertEqual((s["reconciled"], s["usage_monthly"], s["usage_weekly"]), (True, 4.85, 0.85))
        self.assertEqual(s["usage_by_day"]["2026-10-14"], 0.1)

    def test_a_week_that_disagrees_alone_does_not(self):
        week = round(0.25 + 0.5 + 0.1 + 0.5, 9)
        self.assertFalse(self.snap(daily=0.1, weekly=week)["reconciled"])

    def test_a_month_that_disagrees_alone_does_not_whichever_side(self):
        for month in (4.85 + 0.5, 4.85 - 0.5):          # the counter above and below the activity days
            with self.subTest(month=month):
                self.assertFalse(self.snap(daily=0.1, monthly=month)["reconciled"])

    def test_the_tolerance_is_a_hundredth_of_a_cent(self):
        self.assertTrue(self.snap(daily=0.1, monthly=4.85 + 0.00009, weekly=0.85 + 0.00009)["reconciled"])
        self.assertFalse(self.snap(daily=0.1, monthly=4.85 + 0.00011)["reconciled"])
        self.assertFalse(self.snap(daily=0.1, weekly=0.85 + 0.00011)["reconciled"])

    def test_the_week_begins_on_monday(self):
        """A Sunday (10-11) belongs to the week before: the provider's weekly counter does not hold it."""
        s = self.snap(rows=self.ROWS + [row("2026-10-11", 2.0)], daily=0.1)
        self.assertEqual((s["reconciled"], s["usage_weekly"]), (True, 0.85))

    def test_a_today_row_in_the_activity_report_is_not_added_to_the_counter_check(self):
        s = self.snap(rows=self.ROWS + [row("2026-10-14", 0.07)], daily=0.1)
        self.assertTrue(s["reconciled"])
        self.assertEqual(s["usage_by_day"]["2026-10-14"], 0.1)          # today is the counter's, not the row's

    def test_today_comes_from_the_last_read_even_across_midnight(self):
        """The activity report is read at 23:59:59 on 10-13; the key's row, the last read, after midnight."""
        import openrouter
        moving = Clock(utc(2026, 10, 13, 23, 59, 59))
        base = provider_for("h-k2", self.ROWS, daily=0.1, today="2026-10-14")

        def provider(path, key, payload=None, method=None):
            if path == "/keys/h-k2" and method is None:
                moving.t = utc(2026, 10, 14, 0, 0, 1)
            return base(path, key, payload, method)
        with patch.object(openrouter, "_request", provider), patch.object(openrouter, "datetime", moving.module()):
            s = openrouter.usage_snapshot("admin", "h-k2")
        self.assertEqual((s["reconciled"], s["read_at"][:10]), (True, "2026-10-14"))
        self.assertIn("2026-10-14", s["usage_by_day"])

    def test_a_day_the_report_may_miss_and_no_counter_checks_is_not_vouched_for(self):
        """Tuesday 2026-12-01: the month began today and the week on Monday 11-30. A two-day lag hides 11-29,
        which neither counter holds (round 5, probe G); a report that reaches it reconciles."""
        when = utc(2026, 12, 1, 6)
        lagged = snapshot_of([row("2026-11-20", 1.0)], when, "2026-12-01", daily=0.5)
        self.assertFalse(lagged["reconciled"])
        caught_up = snapshot_of([row("2026-11-20", 1.0), row("2026-11-29", 3.0)], when, "2026-12-01", daily=0.5)
        self.assertTrue(caught_up["reconciled"])

    def test_monday_the_first_reconciles_only_when_the_report_reaches_yesterday(self):
        when = utc(2029, 1, 1, 12)
        self.assertTrue(snapshot_of([row("2028-12-31", 2.0)], when, "2029-01-01", daily=0.0)["reconciled"])
        self.assertFalse(snapshot_of([row("2028-12-20", 2.0)], when, "2029-01-01", daily=0.0)["reconciled"])

    def test_an_empty_report_with_spend_in_the_month_does_not_reconcile(self):
        s = self.snap(rows=[], daily=0.0, weekly=0.0, monthly=3.0)
        self.assertEqual((s["reconciled"], s["activity_through"]), (False, None))

    def test_a_key_that_spent_nothing_reconciles(self):
        self.assertTrue(self.snap(rows=[], daily=0.0)["reconciled"])

    def test_a_missing_counter_does_not_reconcile(self):
        for missing in ("daily", "weekly", "monthly"):
            with self.subTest(missing=missing):
                self.assertFalse(self.snap(**{"daily": 0.1, missing: None})["reconciled"])


class StoneReading(unittest.TestCase):
    """How the scan reads a tombstone's snapshot and carried state (round 5)."""

    def scan(self, **kw) -> dict:
        return ProjectSpend().scan(**kw)

    def scan_at(self, now, **kw) -> dict:
        return ProjectSpend().scan_at(now, **kw)

    def test_an_unreconciled_stone_from_an_empty_report_nulls_every_window_to_its_day(self):
        stone = tomb("k2", "h-k2", "alpha-web", "2026-10-08", by_day={}, reconciled=False)
        stone["activity_through"] = None
        alpha = self.scan(ledger={"issued": {}, "retired": [stone]}, keys={})["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["last30_usd"]), (None, None))

    def test_a_lagged_stone_read_on_its_own_retirement_day_is_null(self):
        """Retired 10-14, its report reaching 10-10: scanned at noon on 10-14, the gap 10-11..10-13 is in both
        windows, though the retirement day itself is not yet a completed day."""
        stone = tomb("k2", "h-k2", "alpha-web", "2026-10-14", by_day={"2026-10-10": 1.0}, reconciled=False)
        alpha = self.scan_at(utc(2026, 10, 14, 12), ledger={"issued": {}, "retired": [stone]},
                             keys={})["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["last30_usd"]), (None, None))

    def test_a_stone_without_a_reconciled_field_is_not_whole(self):
        stone = tomb("k2", "h-k2", "alpha-web", "2026-10-08", by_day={"2026-10-05": 1.0})
        stone.pop("reconciled")
        alpha = self.scan(ledger={"issued": {}, "retired": [stone]}, keys={})["projects"]["alpha-web"]
        self.assertEqual(alpha["month_usd"], None)

    def test_a_stone_not_known_to_be_deleted_is_read_by_its_hash_not_its_snapshot(self):
        stone = tomb("k9", "h-k9", "alpha-web", "2026-10-02", reason="replaced-by-issue",
                     by_day={"2026-10-01": 1.0})
        stone["deleted"] = None
        doc = self.scan(ledger={"issued": {}, "retired": [stone]},
                        keys={"h-k9": [row("2026-10-01", 1.0), row("2026-10-12", 2.0)]},
                        account=[row("2026-10-01", 1.0), row("2026-10-12", 2.0)])
        self.assertIn("h-k9", doc["_asked"])
        self.assertEqual((doc["projects"]["alpha-web"]["month_usd"], doc["projects"]["alpha-web"]["partial"]),
                         (3.0, False))

    def test_a_stone_not_known_to_be_deleted_is_followed_into_the_next_month(self):
        """Round 5, probe K: retired 10-02, before both November windows; it is still read, and its spend in
        November stays with its project."""
        stone = tomb("k9", "h-k9", "alpha-web", "2026-10-02", reason="replaced-by-issue")
        stone["deleted"] = None
        led = {"issued": {}, "retired": [stone]}
        doc = self.scan_at(utc(2026, 11, 5, 12), ledger=led, keys={"h-k9": [row("2026-11-03", 2.0)]},
                           account=[row("2026-11-03", 2.0)])
        self.assertEqual((doc["projects"]["alpha-web"]["month_usd"], doc["unattributed"]["month_usd"]), (2.0, 0.0))
        gone = self.scan_at(utc(2026, 11, 5, 12), ledger=led, keys={"h-k9": 404}, account=[row("2026-11-03", 2.0)])
        self.assertEqual((gone["projects"]["alpha-web"]["month_usd"], gone["projects"]["alpha-web"]["last30_usd"]),
                         (None, None))
        self.assertIn("gone at the provider", gone["projects"]["alpha-web"]["unread"][0]["reason"])


class IssueOverAHashedRow(unittest.TestCase):
    """`issue` over a ledger row that still holds a hash (round 5, R5-1/R5-7)."""

    def issue(self, provider, row_):
        import openrouter
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)), \
                patch.object(openrouter, "vault_destination", lambda to: to):
            door = Door(tmp, {"k2": row_}, provider)
            try:
                try:
                    openrouter.issue_key("k2", 5.0, "acct-a", "observatory", "alpha-web")
                    outcome = None
                except ValueError as exc:
                    outcome = str(exc)
                left = door.ledger()
            finally:
                door.close()
        return outcome, left

    @staticmethod
    def provider(alive: bool):
        def p(path, key, payload=None, method=None):
            if path.startswith("/keys?"):
                return {"data": []}
            if path == "/keys/h-k2" and method is None:
                if alive:
                    return {"data": {"name": "renamed-by-hand", "hash": "h-k2"}}
                raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
            if path == "/keys" and payload:
                return {"data": {"hash": "h-fresh", "label": "z"}, "key": "sk-or-v1-" + "f" * 40}
            return {"data": {}}
        return p

    def test_a_key_still_live_under_another_name_is_refused(self):
        outcome, left = self.issue(self.provider(True), ledger_row("acct-a", "h-k2", "alpha-web"))
        self.assertIn("still live at the provider", outcome)
        self.assertEqual((left["issued"]["k2"]["hash"], left.get("retired", [])), ("h-k2", []))

    def test_a_key_gone_at_the_provider_is_retired_with_the_snapshot_its_revoke_kept(self):
        kept = {"usage": "read", "reconciled": True, "usage_by_day": {"2026-10-05": 4.0},
                "activity_through": "2026-10-05", "read_at": "2026-10-13T09:00:00+00:00"}
        outcome, left = self.issue(self.provider(False),
                                   ledger_row("acct-a", "h-k2", "alpha-web", retiring_snapshot=kept))
        self.assertIsNone(outcome)
        (stone,) = left["retired"]
        self.assertEqual((stone["deleted"], stone["usage"], stone["usage_by_day"]), (True, "read", {"2026-10-05": 4.0}))
        self.assertEqual(left["issued"]["k2"]["hash"], "h-fresh")


class KeyserverThreads(unittest.TestCase):
    def test_a_request_waiting_does_not_hold_the_next_one(self):
        """A door verb may wait for the ledger lock; the key page must still answer meanwhile (round 5, R5-8)."""
        import http.server
        import socketserver
        import urllib.request
        import keyserver
        self.assertTrue(issubclass(keyserver.Server, socketserver.ThreadingMixIn))
        release = threading.Event()

        class Slow(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/slow":
                    release.wait(10)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
        srv = keyserver.Server(("127.0.0.1", 0), Slow, "t")
        port = srv.server_address[1]
        runner = threading.Thread(target=srv.serve_forever, daemon=True)
        runner.start()
        try:
            slow = threading.Thread(target=lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/slow", timeout=15).read())
            slow.start()
            time.sleep(0.2)
            t0 = time.monotonic()
            self.assertEqual(urllib.request.urlopen(f"http://127.0.0.1:{port}/fast", timeout=5).read(), b"ok")
            self.assertLess(time.monotonic() - t0, 2.0)
        finally:
            release.set()
            slow.join(15)
            srv.shutdown()
            srv.server_close()


class Fake:
    """A stateful provider: keys by hash with activity rows and counters; POST /keys mints; DELETE deletes."""

    def __init__(self, keys: dict, today: str, fail_delete_after: set | None = None, fail_patch=False):
        self.keys = keys            # hash -> {"name", "rows": [(day, usd)], "daily": float, "deleted": bool}
        self.today = today
        self.n = 0
        self.fail_delete_after = fail_delete_after or set()
        self.fail_patch = fail_patch
        self.calls = []

    def counters(self, k):
        t = date.fromisoformat(self.today)
        monday = (t - timedelta(days=t.weekday())).isoformat()
        month = self.today[:8] + "01"
        s = lambda first: round(sum(u for d, u in k["rows"] if first <= d < self.today) + k["daily"], 9)
        return {"usage_daily": k["daily"], "usage_weekly": s(monday), "usage_monthly": s(month)}

    def __call__(self, path, key, payload=None, method=None):
        self.calls.append((method or ("POST" if payload else "GET"), path))
        if path == "/keys" and payload:
            self.n += 1
            h = f"h-new{self.n}"
            self.keys[h] = {"name": payload["name"], "rows": [], "daily": 0.0, "deleted": False}
            return {"data": {"hash": h, "label": f"lab{self.n}"}, "key": "sk-or-v1-" + str(self.n) * 40}
        if path.startswith("/keys/"):
            h = path[len("/keys/"):]
            k = self.keys.get(h)
            if not k or k["deleted"]:
                raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
            if method == "DELETE":
                k["deleted"] = True
                if h in self.fail_delete_after:
                    raise RuntimeError("openrouter request failed: TimeoutError")   # deleted, answer lost
                return {"data": {}}
            if method == "PATCH":
                if self.fail_patch:
                    raise RuntimeError("openrouter answered HTTP 500; provider response withheld")
                k["name"] = payload["name"]
                return {"data": {}}
            return {"data": {"name": k["name"], "hash": h, **self.counters(k)}}
        if path.startswith("/activity?api_key_hash="):
            h = path.split("=", 1)[1].split("&")[0]
            k = self.keys.get(h)
            if not k or k["deleted"]:
                raise RuntimeError("openrouter answered HTTP 404; provider response withheld")
            return {"data": [{"date": d + " 00:00:00", "usage": u} for d, u in k["rows"]]}
        return {"data": {}}


def scan(now, ledger, fake: Fake, account):
    """Run the real scan over `ledger`, reading keys through the fake provider."""
    import scan_spend
    s = ProjectSpend()
    keys = {}
    for h, k in fake.keys.items():
        keys[h] = 404 if k["deleted"] else [{"date": d + " 00:00:00", "usage": u} for d, u in k["rows"]]
    return s.scan_at(now, ledger=ledger, keys=keys, account=account)


class RoundFiveProbes(unittest.TestCase):
    """The adversarial probes of verification round 5 on #285 (A-K), kept as regression tests. B and K assert
    the fixed behaviour: a retried rotation deletes its predecessor, and an issue over a live renamed key is
    refused."""
    def test_A_rotate_retry_after_lost_delete_answer_loses_predecessor_spend(self):
        """DELETE of the predecessor succeeds but its answer is lost (timeout). The row keeps predecessor_hash and
        predecessor_snapshot. The operator retries `rotate k1` (the error says: inspect, then retry)."""
        import openrouter
        fake = Fake({"h-old": {"name": "k1", "rows": [("2026-10-05", 4.0)], "daily": 0.0, "deleted": False}},
                    today="2026-10-14", fail_delete_after={"h-old"})
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            door = Door(tmp, {"k1": ledger_row("acct-a", "h-old", "alpha-web")}, fake)
            try:
                with self.assertRaises(RuntimeError):
                    openrouter.rotate_one("k1")
                mid = door.ledger()
                # The documented remedy without a retry: the scan of the mid state.
                d_mid = scan(utc(2026, 10, 16, 12), mid, fake, [{"date": "2026-10-05", "usage": 4.0}])
                openrouter.rotate_one("k1")   # the retry
                left = door.ledger()
            finally:
                door.close()
        doc = scan(utc(2026, 10, 16, 12), left, fake, [{"date": "2026-10-05", "usage": 4.0}])
        alpha, un = doc["projects"]["alpha-web"], doc["unattributed"]
        self.assertTrue(alpha["month_usd"] is None or abs(alpha["month_usd"] - 4.0) < 1e-6,
                        f"whole and lower: alpha {alpha['month_usd']} partial={alpha['partial']}, "
                        f"unattributed {un['month_usd']}")

    def test_B_rotate_real_delete_failure_then_retry_leaves_predecessor_live_and_unread(self):
        """The DELETE really fails (5xx, key still live). Retry: find_key reads the disk row's hash (the
        successor), so the predecessor h-old is never deleted and its hash leaves the ledger."""
        import openrouter
        fake = Fake({"h-old": {"name": "k1", "rows": [("2026-10-05", 4.0)], "daily": 0.0, "deleted": False}},
                    today="2026-10-14")
        orig = fake.__call__

        state = {"fail": True}

        def provider(path, key, payload=None, method=None):
            if method == "DELETE" and path == "/keys/h-old" and state["fail"]:
                state["fail"] = False
                raise RuntimeError("openrouter answered HTTP 502; provider response withheld")
            return orig(path, key, payload, method)
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            door = Door(tmp, {"k1": ledger_row("acct-a", "h-old", "alpha-web")}, provider)
            try:
                with self.assertRaises(RuntimeError):
                    openrouter.rotate_one("k1")
                openrouter.rotate_one("k1")
                left = door.ledger()
            finally:
                door.close()
        self.assertTrue(fake.keys["h-old"]["deleted"], "the retry did not delete the predecessor")
        self.assertIn("h-old", [t["hash"] for t in left["retired"]])
        doc = scan(utc(2026, 10, 16, 12), left, fake, [{"date": "2026-10-05", "usage": 4.0}])
        alpha = doc["projects"]["alpha-web"]
        self.assertTrue(alpha["month_usd"] is None or abs(alpha["month_usd"] - 4.0) < 1e-6,
                        f"whole and lower: {alpha['month_usd']}")

    def test_C_empty_activity_with_monthly_spend(self):
        import openrouter

        def provider(path, key, payload=None, method=None):
            if path == "/keys/h-k2" and method is None:
                return {"data": {"name": "k2", "hash": "h-k2", "usage_daily": 0.0, "usage_weekly": 0.0,
                                 "usage_monthly": 3.0}}
            if path.startswith("/activity?api_key_hash=h-k2"):
                return {"data": []}
            return {"data": {}}
        _c, left = door_revoke(provider, when=AT)
        st = left["retired"][0]
        doc = scan(utc(2026, 10, 16, 12), left, Fake({"h-k1": {"name": "k1", "rows": [], "daily": 0, "deleted": False}},
                                                    "2026-10-16"), [{"date": "2026-10-03", "usage": 3.0}])
        alpha = doc["projects"]["alpha-web"]
        self.assertEqual((alpha["month_usd"], alpha["last30_usd"]), (None, None))

    def test_D_zero_spend_key(self):
        _c, left = door_revoke(provider_for("h-k2", [], daily=0.0), when=AT)
        st = left["retired"][0]
        self.assertTrue(st["reconciled"])

    def test_E_monday_the_first_never_reconciles(self):
        """2029-01-01 is a Monday. A complete activity report and agreeing counters: still not reconciled."""
        rows = [{"date": "2028-12-31 00:00:00", "usage": 2.0}]
        when = utc(2029, 1, 1, 12)
        _c, left = door_revoke(provider_for("h-k2", rows, daily=0.0, today="2029-01-01"), when=when)
        st = left["retired"][0]
        self.assertTrue(st["reconciled"])
        for day in (10, 20, 31):
            doc = scan(utc(2029, 1, day, 12), left, Fake({"h-k1": {"name": "k1", "rows": [], "daily": 0,
                                                                   "deleted": False}}, "2029-01-%02d" % day),
                       [{"date": "2028-12-31", "usage": 2.0}])
            a = doc["projects"]["alpha-web"]
            self.assertEqual(a["partial"], False)

    def test_F_month_boundary_retire_on_the_first_tuesday(self):
        """2026-12-01 is a Tuesday. Activity lags: yesterday (11-30, 3.0) not in the report."""
        rows = [{"date": "2026-11-20 00:00:00", "usage": 1.0}]

        def provider(path, key, payload=None, method=None):
            if path == "/keys/h-k2" and method is None:
                return {"data": {"name": "k2", "hash": "h-k2", "usage_daily": 0.5, "usage_weekly": 3.5,
                                 "usage_monthly": 0.5}}
            if path.startswith("/activity?api_key_hash=h-k2"):
                return {"data": rows}
            return {"data": {}}
        _c, left = door_revoke(provider, when=utc(2026, 12, 1, 6))
        st = left["retired"][0]
        self.assertFalse(st["reconciled"])

    def test_G_two_day_lag_outside_both_checks(self):
        """Tuesday 2026-12-01. The report lags two days: 11-29 (Sun, 3.0) missing; 11-30 (Mon) spent 0.
        Neither the month (12-01..) nor the week (11-30..) holds 11-29."""
        rows = [{"date": "2026-11-20 00:00:00", "usage": 1.0}]

        def provider(path, key, payload=None, method=None):
            if path == "/keys/h-k2" and method is None:
                return {"data": {"name": "k2", "hash": "h-k2", "usage_daily": 0.5, "usage_weekly": 0.5,
                                 "usage_monthly": 0.5}}
            if path.startswith("/activity?api_key_hash=h-k2"):
                return {"data": rows}
            return {"data": {}}
        _c, left = door_revoke(provider, when=utc(2026, 12, 1, 6))
        st = left["retired"][0]
        self.assertFalse(st["reconciled"])
        doc = scan(utc(2026, 12, 3, 12), left, Fake({"h-k1": {"name": "k1", "rows": [], "daily": 0,
                                                              "deleted": False}}, "2026-12-03"),
                   [{"date": "2026-11-20", "usage": 1.0}, {"date": "2026-11-29", "usage": 3.0},
                    {"date": "2026-12-01", "usage": 0.5}])
        a = doc["projects"]["alpha-web"]
        self.assertIsNone(a["last30_usd"])

    def test_H_float_accumulation_30_days(self):
        rows = [{"date": (date(2026, 10, 1) + timedelta(days=i)).isoformat() + " 00:00:00", "usage": 0.1}
                for i in range(13)]
        rows += [{"date": (date(2026, 9, 14) + timedelta(days=i)).isoformat() + " 00:00:00", "usage": 0.1}
                 for i in range(17)]
        many = [{"date": r["date"], "usage": 1e-7 * 3} for r in rows for _ in range(30)]
        _c, left = door_revoke(provider_for("h-k2", rows + many, daily=0.3), when=AT)
        self.assertTrue(left["retired"][0]["reconciled"])

    def test_I_negative_refund(self):
        rows = [{"date": "2026-10-05 00:00:00", "usage": 4.0}, {"date": "2026-10-06 00:00:00", "usage": -1.0}]
        _c, left = door_revoke(provider_for("h-k2", rows, daily=0.0), when=AT)
        st = left["retired"][0]
        doc = scan(utc(2026, 10, 16, 12), left, Fake({"h-k1": {"name": "k1", "rows": [], "daily": 0,
                                                              "deleted": False}}, "2026-10-16"),
                   [{"date": "2026-10-05", "usage": 4.0}, {"date": "2026-10-06", "usage": -1.0}])
        self.assertEqual(doc["projects"]["alpha-web"]["month_usd"], 3.0)

    def test_J_issue_replaced_key_still_live_nulls_project_for_window(self):
        """An issue over a row whose key was renamed at the provider is impossible (find by hash); measure the
        null horizon of a deleted=None stone instead."""
        stone = tomb("k9", "h-k9", "alpha-web", "2026-10-02", reason="replaced-by-issue", by_day={})
        stone["deleted"] = None
        led = {"issued": {"k1": ledger_row("acct-a", "h-k1", "alpha-web")}, "retired": [stone]}
        for when in (utc(2026, 10, 16), utc(2026, 11, 1, 1), utc(2026, 11, 2, 1)):
            doc = scan(when, led, Fake({"h-k1": {"name": "k1", "rows": [], "daily": 0, "deleted": False},
                                        "h-k9": {"name": "k9", "rows": [], "daily": 0, "deleted": True}},
                                       when.date().isoformat()), [])
            a = doc["projects"]["alpha-web"]
            self.assertEqual((a["month_usd"], a["last30_usd"]), (None, None))



class RoundFiveProbesIssue(unittest.TestCase):
    def test_K_issue_over_renamed_live_key_then_month_rolls(self):
        """The key k2 was renamed in the provider's dashboard and still serves someone. `issue k2` sees no key
        called k2 (find by hash returns the renamed row), mints a new one, and leaves a deleted=None stone.
        In November the stone is filtered out (retired before both windows) while the old key keeps spending."""
        import openrouter
        fake = Fake({"h-k2": {"name": "renamed-by-hand", "rows": [("2026-10-01", 1.0)], "daily": 0.0,
                              "deleted": False}}, today="2026-10-02")
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(utc(2026, 10, 2, 9))), \
                patch.object(openrouter, "today", lambda: "2026-10-02"), \
                patch.object(openrouter, "vault_destination", lambda to: to):
            door = Door(tmp, {"k2": ledger_row("acct-a", "h-k2", "alpha-web")}, fake)
            try:
                with self.assertRaises(ValueError) as refused:
                    openrouter.issue_key("k2", 5.0, "acct-a", "vault:x/prod/K", "alpha-web")
                left = door.ledger()
            finally:
                door.close()
        self.assertIn("still live at the provider", str(refused.exception))
        self.assertEqual(left["issued"]["k2"]["hash"], "h-k2")
        fake.keys["h-k2"]["rows"] += [("2026-11-03", 2.0)]
        doc = scan(utc(2026, 11, 5, 12), left, fake, [{"date": "2026-10-01", "usage": 1.0},
                                                      {"date": "2026-11-03", "usage": 2.0}])
        a = doc["projects"]["alpha-web"]
        self.assertEqual((a["month_usd"], a["partial"]), (2.0, False))


class RotationRetry(unittest.TestCase):
    """A rotation a failure left part done is finished by the retry, which then rotates afresh: the old
    predecessor is retired once, and the value the row holds afterwards is new (rounds 5 and 6)."""

    def retry(self, provider_wrap=None, **fake_kw) -> tuple[dict, dict, dict, "Fake"]:
        import openrouter
        fake = Fake({"h-old": {"name": "k1", "rows": [("2026-10-05", 4.0)], "daily": 0.0, "deleted": False}},
                    "2026-10-14", **fake_kw)
        provider = provider_wrap(fake) if provider_wrap else fake
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            door = Door(tmp, {"k1": ledger_row("acct-a", "h-old", "alpha-web")}, provider)
            try:
                with self.assertRaises(RuntimeError):
                    openrouter.rotate_one("k1")
                mid = door.ledger()
                before = len(fake.calls)
                result = openrouter.rotate_one("k1")
                left = door.ledger()
            finally:
                door.close()
        self.assertEqual((result["resumed"], result["replaced"]), (True, True))
        self.assertEqual(sum(1 for c in fake.calls[before:] if c == ("POST", "/keys")), 1)
        live = sorted(h for h, k in fake.keys.items() if not k["deleted"])
        self.assertEqual(live, [left["issued"]["k1"]["hash"]])
        self.assertEqual(fake.keys[live[0]]["name"], "k1")
        self.assertEqual([s["hash"] for s in left["retired"]].count("h-old"), 1)
        return mid, left, result, fake

    def test_a_delete_whose_answer_was_lost_is_retired_with_the_snapshot_kept_before_it(self):
        mid, left, _r, _f = self.retry(fail_delete_after={"h-old"})
        self.assertEqual(mid["issued"]["k1"]["predecessor_snapshot"]["usage"], "read")
        old = next(s for s in left["retired"] if s["hash"] == "h-old")
        self.assertEqual((old["usage"], old["reconciled"]), ("read", True))
        self.assertEqual(left["issued"]["k1"]["rotation_status"], "complete")

    def test_a_delete_that_really_failed_is_retried_before_anything_else(self):
        state = {"fails": 1}

        def wrap(fake):
            def provider(path, key, payload=None, method=None):
                if method == "DELETE" and path == "/keys/h-old" and state["fails"]:
                    state["fails"] -= 1
                    raise RuntimeError("openrouter answered HTTP 502; provider response withheld")
                return fake(path, key, payload, method)
            return provider
        _mid, left, _r, fake = self.retry(provider_wrap=wrap)
        self.assertTrue(fake.keys["h-old"]["deleted"])
        self.assertEqual(left["issued"]["k1"]["rotation_status"], "complete")


class RoundSixDoor(unittest.TestCase):
    """The door's findings of verification round 6 (cycle 5)."""

    def test_a_leaked_value_on_a_part_done_row_is_replaced(self):
        """R6-1: a leak recorded after a part-done rotation; `rotate --leaked` must change the row's hash, say
        it rotated, and hand the settle hint."""
        import contextlib
        import io
        import openrouter
        fake = Fake({"h-old": {"name": "k1", "rows": [], "daily": 0.0, "deleted": False}}, "2026-10-14",
                    fail_patch=True)
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            door = Door(tmp, {"k1": ledger_row("acct-a", "h-old", "alpha-web")}, fake)
            try:
                with self.assertRaises(RuntimeError):
                    openrouter.rotate_one("k1")
                leaked_value_hash = door.ledger()["issued"]["k1"]["hash"]
                fake.fail_patch = False
                out, err = io.StringIO(), io.StringIO()
                with patch.object(openrouter, "_leaked", lambda n, r: True), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = openrouter.cmd_rotate(None, leaked=True)
                after = door.ledger()["issued"]["k1"]
            finally:
                door.close()
        self.assertEqual(rc, 0)
        self.assertNotEqual(after["hash"], leaked_value_hash)
        self.assertTrue(fake.keys[leaked_value_hash]["deleted"])
        self.assertIn("finished first", out.getvalue())
        self.assertIn("settle", out.getvalue())

    def test_a_run_that_replaced_nothing_never_says_rotated(self):
        import contextlib
        import io
        import openrouter
        out, err = io.StringIO(), io.StringIO()
        with patch.object(openrouter, "ledger", lambda: {"issued": {"k1": ledger_row("acct-a", "h", "p")}}), \
                patch.object(openrouter, "_leaked", lambda n, r: True), \
                patch.object(openrouter, "rotate_one", lambda n: {"name": n, "resumed": True, "replaced": False}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = openrouter.cmd_rotate(None, leaked=True)
        self.assertEqual(rc, 1)
        self.assertNotIn("rotated", out.getvalue())
        self.assertNotIn("settle", out.getvalue())
        self.assertIn("not replaced", err.getvalue())

    def test_the_key_page_refuses_a_run_that_replaced_nothing(self):
        import keyserver

        class Door_:
            def rotate_one(self, name):
                return {"name": name, "resumed": True, "replaced": False}
        with patch.object(keyserver, "_door", lambda: Door_()), patch.object(keyserver, "audit", lambda *a, **k: None):
            with self.assertRaises(LookupError) as refused:
                keyserver.act_rotate_key({"name": "k1"})
        self.assertIn("not replaced", str(refused.exception))

    def test_a_row_naming_its_own_live_key_as_predecessor_is_refused_before_any_request(self):
        """R6-2."""
        import openrouter
        fake = Fake({"h-cur": {"name": "k1", "rows": [], "daily": 0.0, "deleted": False}}, "2026-10-14")
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            door = Door(tmp, {"k1": ledger_row("acct-a", "h-cur", "alpha-web", rotations=1,
                                               rotation_status="successor-delivered-predecessor-pending",
                                               predecessor_hash="h-cur")}, fake)
            try:
                with self.assertRaises(ValueError) as refused:
                    openrouter.rotate_one("k1")
            finally:
                door.close()
        self.assertIn("Repair the row", str(refused.exception))
        self.assertEqual(fake.calls, [])
        self.assertFalse(fake.keys["h-cur"]["deleted"])

    def test_the_resume_keeps_the_snapshot_it_read_before_its_own_delete(self):
        """R6-4 (C1, C23): the earlier DELETE really failed, so the resume snapshots the predecessor again (it spent
        on 10-13 since) and writes that to the row before its own DELETE; that DELETE's answer is lost, and the
        next run retires the predecessor with the newer snapshot."""
        import openrouter
        old_snapshot = {"usage": "read", "reconciled": True, "usage_by_day": {"2026-10-05": 4.0},
                        "activity_through": "2026-10-05", "read_at": "2026-10-06T10:00:00+00:00"}
        fake = Fake({"h-old": {"name": "k1", "rows": [("2026-10-05", 4.0), ("2026-10-13", 1.0)], "daily": 0.0,
                               "deleted": False},
                     "h-succ": {"name": "k1-next", "rows": [], "daily": 0.0, "deleted": False}},
                    "2026-10-14", fail_delete_after={"h-old"})
        row_ = ledger_row("acct-a", "h-succ", "alpha-web", rotations=1, rotated_on="2026-10-06",
                          rotation_status="successor-delivered-predecessor-pending", predecessor_hash="h-old",
                          predecessor_snapshot=old_snapshot)
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            door = Door(tmp, {"k1": row_}, fake)
            try:
                with self.assertRaises(RuntimeError):
                    openrouter.rotate_one("k1")
                openrouter.rotate_one("k1")
                left = door.ledger()
            finally:
                door.close()
        stone = next(s for s in left["retired"] if s["hash"] == "h-old")
        self.assertEqual(stone["usage_by_day"].get("2026-10-13"), 1.0)

    def test_a_rename_pending_row_is_renamed_even_when_the_new_mint_fails(self):
        """R6-4 (C3, C6): the predecessor is retired, the successor's rename pending, no `predecessor_hash`. The
        retry renames the successor and completes the old rotation before minting; a failed mint leaves it so."""
        import openrouter
        fake = Fake({"h-succ": {"name": "k1-next", "rows": [], "daily": 0.0, "deleted": False}}, "2026-10-14")

        def provider(path, key, payload=None, method=None):
            if path == "/keys" and payload:
                fake.calls.append(("POST", path))
                raise RuntimeError("openrouter answered HTTP 502; provider response withheld")
            return fake(path, key, payload, method)
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            door = Door(tmp, {"k1": ledger_row("acct-a", "h-succ", "alpha-web", rotations=1, tombstoned_rotations=1,
                                               rotation_status="predecessor-retired-rename-pending")}, provider)
            try:
                with self.assertRaises(RuntimeError):
                    openrouter.rotate_one("k1")
                left = door.ledger()
            finally:
                door.close()
        self.assertEqual(fake.keys["h-succ"]["name"], "k1")
        self.assertEqual(left["issued"]["k1"]["rotation_status"], "complete")

    def test_a_successor_whose_delivery_was_not_recorded_is_kept_and_retired_later(self):
        """R6-6: the write after the delivery fails. The minted successor is on the row (`successor_pending`);
        the retry keeps it until a newer successor holds the slot, then deletes and retires it."""
        import openrouter
        fake = Fake({"h-old": {"name": "k1", "rows": [], "daily": 0.0, "deleted": False}}, "2026-10-14")
        real = openrouter.save_ledger
        n = {"saves": 0}

        def save(doc):
            n["saves"] += 1
            if n["saves"] == 2:                 # the first write after the delivery
                raise OSError("disk full")
            real(doc)
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            door = Door(tmp, {"k1": ledger_row("acct-a", "h-old", "alpha-web")}, fake)
            try:
                with patch.object(openrouter, "save_ledger", save):
                    with self.assertRaises(RuntimeError):
                        openrouter.rotate_one("k1")
                mid = door.ledger()["issued"]["k1"]
                openrouter.rotate_one("k1")
                left = door.ledger()
            finally:
                door.close()
        self.assertEqual((mid["hash"], mid["successor_pending"]), ("h-old", "h-new1"))
        live = sorted(h for h, k in fake.keys.items() if not k["deleted"])
        self.assertEqual(live, [left["issued"]["k1"]["hash"]])
        self.assertEqual(sum(1 for c in fake.calls if c == ("POST", "/keys")), 2)
        self.assertEqual(sorted((s["hash"], s["reason"]) for s in left["retired"]),
                         [("h-new1", "stale-successor"), ("h-old", "rotated")])
        self.assertNotIn("stale_successors", left["issued"]["k1"])

    def test_the_scan_reads_a_successor_whose_delivery_was_not_recorded(self):
        led = {"issued": {"k1": ledger_row("acct-a", "h-old", "alpha-web", successor_pending="h-s")}}
        doc = ProjectSpend().scan(ledger=led, keys={"h-old": [row("2026-10-05", 1.0)], "h-s": [row("2026-10-06", 2.0)]},
                                  account=[row("2026-10-05", 1.0), row("2026-10-06", 2.0)])
        self.assertEqual((doc["projects"]["alpha-web"]["month_usd"], doc["unattributed"]["month_usd"]), (3.0, 0.0))

    def test_a_tombstone_not_known_to_be_deleted_is_never_pruned(self):
        """R6-3."""
        import openrouter
        doc = {"issued": {}, "retired": [{"name": "k9", "hash": "h-k9", "retired_on": "2026-01-01", "deleted": None}]}
        openrouter._retire(doc, "new", {"account": "acct-a", "project": "alpha-web"}, "h-new", "revoked")
        self.assertEqual([r["name"] for r in doc["retired"]], ["k9", "new"])

    def test_annotations_written_at_once_both_land(self):
        """R6-5: the key server answers on a thread per request; two annotations at once must not drop one."""
        import sign_credential
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp).resolve() / "credential_annotations.json"
            real_load = sign_credential.load

            def slow_load():
                doc = real_load()
                time.sleep(0.3)                 # both writers have read before either writes
                return doc
            with patch.object(sign_credential, "FILE", f), patch.object(sign_credential, "board_ids", lambda: {}), \
                    patch.object(sign_credential, "load", slow_load):
                threads = [threading.Thread(target=sign_credential.write,
                                            args=(cid, {"purpose": "a purpose", "evidence": "an evidence"}))
                           for cid in ("openrouter/one", "openrouter/two")]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(10)
            rows = json.loads(f.read_text())["annotations"]
        self.assertEqual(sorted(rows), ["openrouter/one", "openrouter/two"])


class RoundSixScan(unittest.TestCase):
    """The scan's findings of verification round 6 (cycle 5): window edges of an unreconciled snapshot."""

    def unknown(self, retired, through, month="2026-10", start="2026-09-14", end="2026-10-13"):
        import scan_spend
        stone = tomb("k2", "h", "p", retired, by_day={through: 1.0}, reconciled=False)
        return sorted(scan_spend.snapshot_sums(stone, month, start, end)[1])

    def test_a_stone_retired_on_a_windows_first_day_nulls_that_window(self):
        self.assertIn("month", self.unknown("2026-10-01", "2026-09-28"))

    def test_a_report_that_reaches_past_the_retirement_leaves_no_gap(self):
        self.assertEqual(self.unknown("2026-10-08", "2026-10-09"), [])

    def test_a_gap_starting_on_the_last_completed_day_is_unknown(self):
        """The gap starts on `end` itself, the last completed day, and runs to today's retirement."""
        self.assertEqual(self.unknown("2026-10-14", "2026-10-12"), ["last30", "month"])

    def test_only_the_third_lag_day_uncovered_does_not_reconcile(self):
        """Wednesday 2026-12-02: the week began on Monday 11-30 and the month on 12-01; 11-29 is the third
        completed day back and no counter checks it."""
        when = utc(2026, 12, 2, 6)
        self.assertFalse(snapshot_of([row("2026-11-28", 1.0)], when, "2026-12-02", daily=0.0)["reconciled"])
        self.assertTrue(snapshot_of([row("2026-11-29", 1.0)], when, "2026-12-02", daily=0.0)["reconciled"])

    def test_a_stone_not_known_to_be_deleted_and_a_live_row_sharing_its_hash_are_both_unknown(self):
        stone = tomb("k9", "h-k9", "alpha-web", "2026-10-02", reason="replaced-by-issue")
        stone["deleted"] = None
        led = {"issued": {"k9b": ledger_row("acct-a", "h-k9", "beta-api")}, "retired": [stone]}
        doc = ProjectSpend().scan(ledger=led, keys={"h-k9": [row("2026-10-05", 2.0)]}, account=[row("2026-10-05", 2.0)])
        self.assertIsNone(doc["projects"]["alpha-web"]["month_usd"])
        self.assertIsNone(doc["projects"]["beta-api"]["month_usd"])
        self.assertIsNone(doc["unattributed"]["month_usd"])

    def test_a_report_row_after_today_is_not_coverage(self):
        """A row dated after today (the provider's clock ahead) is neither in the snapshot nor its coverage."""
        s = snapshot_of([row("2026-10-13", 0.25), row("2026-10-15", 9.0)], AT, "2026-10-14", daily=0.0)
        self.assertEqual(s["activity_through"], "2026-10-13")
        self.assertNotIn("2026-10-15", s["usage_by_day"])


def run_door_r6(issued, fake, when, fn):
    import openrouter
    with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(when)):
        door = Door(tmp, issued, fake)
        try:
            try:
                out = fn(openrouter)
            except Exception as exc:  # noqa: BLE001
                out = exc
            return out, door.ledger()
        finally:
            door.close()


class RoundSixP1RotateOnPartDoneRowDoesNotReplaceValue(unittest.TestCase):
    """A leaked key's owner runs `rotate` on a row a past rotation left part done: the door only finishes the
    old rotation, the CURRENT value (the earlier successor) stays live, and cmd_rotate prints 'rotated'."""

    def test_rotate_after_patch_failure_mints_nothing_new(self):
        fake = Fake({"h-old": {"name": "k1", "rows": [("2026-10-05", 4.0)], "daily": 0.0, "deleted": False}},
                      "2026-10-14", fail_patch=True)
        issued = {"k1": ledger_row("acct-a", "h-old", "alpha-web")}
        import openrouter
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            door = Door(tmp, issued, fake)
            try:
                with self.assertRaises(RuntimeError):
                    openrouter.rotate_one("k1")            # PATCH fails: status rename-pending
                mid = door.ledger()["issued"]["k1"]
                live_after_first = mid["hash"]
                fake.fail_patch = False
                # The successor's value leaks. The operator rotates it.
                buf, err = io.StringIO(), io.StringIO()
                with patch.object(openrouter, "_leaked", lambda n, r: True), redirect_stdout(buf), redirect_stderr(err):
                    rc = openrouter.cmd_rotate(None, leaked=True)
                after = door.ledger()["issued"]["k1"]
            finally:
                door.close()
        mints = [c for c in fake.calls if c == ("POST", "/keys")]
        # The defect: the leaked live value is unchanged while the door says 'rotated'.
        self.assertNotEqual(after["hash"], live_after_first,
                            "rotate --leaked finished an old rotation but left the leaked successor live")


class RoundSixP2LegacyMainPendingRow(unittest.TestCase):
    """A row main left at 'successor-delivered-predecessor-pending' with predecessor_hash naming a hash that
    was deleted long ago: rotate only renames; no new value."""

    def test_legacy_pending_row(self):
        fake = Fake({"h-cur": {"name": "k1", "rows": [], "daily": 0.0, "deleted": False}}, "2026-10-14")
        issued = {"k1": ledger_row("acct-a", "h-cur", "alpha-web", rotations=1, rotated_on="2026-08-01",
                                     rotation_status="successor-delivered-predecessor-pending",
                                     predecessor_hash="h-gone")}
        out, led = run_door_r6(issued, fake, AT, lambda m: m.rotate_one("k1"))
        self.assertNotEqual(led["issued"]["k1"]["hash"], "h-cur", "a rotate request produced no new value")


class RoundSixP3ResumeNeverDeletesSuccessor(unittest.TestCase):
    def test_predecessor_equal_to_successor_hash(self):
        """Hand-edited / corrupted row: predecessor_hash equals the live hash. Resume must not DELETE the live key."""
        fake = Fake({"h-cur": {"name": "k1", "rows": [], "daily": 0.0, "deleted": False}}, "2026-10-14")
        issued = {"k1": ledger_row("acct-a", "h-cur", "alpha-web", rotations=1, rotated_on="2026-10-13",
                                     rotation_status="successor-delivered-predecessor-pending",
                                     predecessor_hash="h-cur")}
        out, led = run_door_r6(issued, fake, AT, lambda m: m.rotate_one("k1"))
        self.assertFalse(fake.keys["h-cur"]["deleted"], "resume deleted the row's own live key")


class RoundSixP4CrashAtEachResumeStep(unittest.TestCase):
    """Crash injected at each provider call / save of _resume_rotation; a second resume must converge to
    exactly one stone for the predecessor, the successor alive and named, no second mint."""

    def run_case(self, fail_at):
        import openrouter
        fake = Fake({"h-old": {"name": "k1", "rows": [("2026-10-05", 4.0)], "daily": 0.0, "deleted": False},
                       "h-succ": {"name": "k1-next", "rows": [], "daily": 0.0, "deleted": False}}, "2026-10-14")
        issued = {"k1": ledger_row("acct-a", "h-succ", "alpha-web", rotations=1, rotated_on="2026-10-14",
                                     rotation_status="successor-delivered-predecessor-pending",
                                     predecessor_hash="h-old")}
        n = {"saves": 0, "calls": 0}
        real_save = openrouter.save_ledger
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            def prov(path, key, payload=None, method=None):
                n["calls"] += 1
                if ("call", n["calls"]) == fail_at:
                    raise RuntimeError("openrouter answered HTTP 502; provider response withheld")
                return fake(path, key, payload, method)
            door = Door(tmp, issued, prov)

            def save(doc):
                n["saves"] += 1
                if ("save", n["saves"]) == fail_at:
                    raise OSError("disk full")
                real_save(doc)
            try:
                first_failed = False
                with patch.object(openrouter, "save_ledger", save):
                    try:
                        openrouter.rotate_one("k1")
                    except RuntimeError:
                        first_failed = True
                n["calls"] = -10_000
                n["saves"] = -10_000
                ok = True
                if first_failed:
                    try:
                        openrouter.rotate_one("k1")
                    except RuntimeError as exc:
                        ok = repr(exc)
                led = door.ledger()
            finally:
                door.close()
        stones = [s for s in led.get("retired", []) if s.get("hash") == "h-old"]
        return ok, led, stones, fake

    def test_each_step(self):
        for fail_at in [("call", i) for i in range(1, 8)] + [("save", i) for i in range(1, 5)]:
            with self.subTest(fail_at=fail_at):
                ok, led, stones, fake = self.run_case(fail_at)
                row = led["issued"]["k1"]
                # Cycle 5 (R6-1): the retry finishes the old rotation, then rotates afresh; it converges on one live
                # key, the row's, named for it, with the old predecessor retired once.
                self.assertIs(ok, True, f"{fail_at}: second resume failed: {ok}")
                self.assertEqual(len(stones), 1, f"{fail_at}: stones {stones}")
                self.assertTrue(fake.keys["h-old"]["deleted"])
                live = sorted(h for h, k in fake.keys.items() if not k["deleted"])
                self.assertEqual(live, [row["hash"]], f"{fail_at}")
                self.assertEqual(fake.keys[row["hash"]]["name"], "k1")
                self.assertEqual(row["rotation_status"], "complete")


class RoundSixP5StoneNotDeletedDoubleCount(unittest.TestCase):
    def test_renamed_live_row_and_undeleted_stone_share_hash(self):
        """A legacy deleted:None stone for h-k9, and the same key claimed again as a live row: neither counted,
        both nulled, unattributed nulls."""
        stone = tomb("k9", "h-k9", "alpha-web", "2026-10-02", reason="replaced-by-issue")
        stone["deleted"] = None
        led = {"issued": {"k9b": ledger_row("acct-a", "h-k9", "beta-api")}, "retired": [stone]}
        doc = ProjectSpend().scan(ledger=led, keys={"h-k9": [row("2026-10-05", 2.0)]},
                                    account=[row("2026-10-05", 2.0)])
        a, b = doc["projects"]["alpha-web"], doc["projects"]["beta-api"]
        self.assertIsNone(a["month_usd"])
        self.assertIsNone(b["month_usd"])
        self.assertIsNone(doc["unattributed"]["month_usd"])

    def test_undeleted_stone_pruned_after_62_days_spends_into_unattributed(self):
        """The scan follows a deleted:None stone 'while the door keeps it'; the door prunes it at 62 days
        on any write, after which a live key's spend lands in unattributed with the project whole."""
        import openrouter
        stone = tomb("k9", "h-k9", "alpha-web", "2026-08-01", reason="replaced-by-issue")
        stone["deleted"] = None
        fake = Fake({"h-k1": {"name": "k1", "rows": [], "daily": 0.0, "deleted": False}}, "2026-10-14")
        _out, led = run_door_r6({"issued": {"k1": ledger_row("acct-a", "h-k1", "alpha-web")}, "retired": [stone]},
                             fake, AT, lambda m: m.revoke_key("k1"))
        kept = [s for s in led["retired"] if s["hash"] == "h-k9"]
        self.assertTrue(kept, "a stone not known to be deleted was pruned; its key's spend goes unattributed")


class RoundSixP6StoneReadHash404Paths(unittest.TestCase):
    def test_404_with_deleted_false(self):
        stone = tomb("k9", "h-k9", "alpha-web", "2026-10-02")
        stone["deleted"] = False
        doc = ProjectSpend().scan(ledger={"issued": {}, "retired": [stone]}, keys={"h-k9": 404},
                                    account=[row("2026-10-05", 2.0)])
        a = doc["projects"]["alpha-web"]
        self.assertEqual((a["month_usd"], a["last30_usd"]), (None, None))
        self.assertIsNone(doc["unattributed"]["month_usd"])

    def test_deleted_string_true(self):
        """'deleted': 'true' (hand edit) is not True: read by hash like a live key."""
        stone = tomb("k9", "h-k9", "alpha-web", "2026-10-02", by_day={"2026-10-01": 1.0})
        stone["deleted"] = "true"
        doc = ProjectSpend().scan(ledger={"issued": {}, "retired": [stone]}, keys={"h-k9": 404},
                                    account=[row("2026-10-01", 1.0)])
        self.assertIsNone(doc["projects"]["alpha-web"]["month_usd"])

    def test_404_on_live_row_is_not_reworded(self):
        doc = ProjectSpend().scan(ledger={"issued": {"k1": ledger_row("acct-a", "h-k1", "alpha-web")}},
                                    keys={"h-k1": 404}, account=[row("2026-10-05", 2.0)])
        r = doc["projects"]["alpha-web"]["unread"][0]["reason"]
        self.assertIn("HTTP 404", r)

    def test_undeleted_stone_with_missing_retired_on(self):
        stone = tomb("k9", "h-k9", "alpha-web", "2026-10-02")
        stone["deleted"] = None
        stone.pop("retired_on")
        doc = ProjectSpend().scan(ledger={"issued": {}, "retired": [stone]}, keys={"h-k9": [row("2026-10-05", 2.0)]},
                                    account=[row("2026-10-05", 2.0)])
        self.assertEqual(doc["projects"]["alpha-web"]["month_usd"], 2.0)


class RoundSixP7ActivityThroughAtBoundaries(unittest.TestCase):
    def test_gap_windows_at_month_start(self):
        import scan_spend
        # Retired Monday 2029-01-01, report reaching 2028-12-30 (unreconciled). Scanned 2029-01-02.
        stone = tomb("k2", "h", "p", "2029-01-01", by_day={"2028-12-30": 1.0}, reconciled=False)
        month, start, end = "2029-01", "2028-12-03", "2029-01-01"
        usd, unknown = scan_spend.snapshot_sums(stone, month, start, end)
        self.assertEqual(sorted(unknown), ["last30", "month"])
        # Scanned 2029-02-01: month February, gap 12-31..01-01 not in February.
        usd, unknown = scan_spend.snapshot_sums(stone, "2029-02", "2029-01-02", "2029-01-31")
        self.assertEqual(unknown, [])
        # last30 2029-01-01..2029-01-30 holds 01-01
        usd, unknown = scan_spend.snapshot_sums(stone, "2029-01", "2029-01-01", "2029-01-30")
        self.assertEqual(sorted(unknown), ["last30", "month"])
        # last30 starting 01-02 does not hold it, month does
        usd, unknown = scan_spend.snapshot_sums(stone, "2029-01", "2029-01-02", "2029-01-31")
        self.assertEqual(unknown, ["month"])

    def test_activity_through_after_retired(self):
        """activity_through later than retired_on (clock skew / hand edit): no gap, must still be whole?"""
        import scan_spend
        stone = tomb("k2", "h", "p", "2026-10-08", by_day={"2026-10-09": 1.0}, reconciled=False)
        usd, unknown = scan_spend.snapshot_sums(stone, "2026-10", "2026-09-14", "2026-10-13")

    def test_idle_key_on_month_and_week_start_never_reconciles(self):
        """Tuesday 2026-12-01: an idle key (last spend 11-20, nothing since, counters exact) does not reconcile,
        so December's month window reads null for the whole month though the monthly counter vouched for it."""
        snap = snapshot_of([row("2026-11-20", 1.0)], utc(2026, 12, 1, 6), "2026-12-01", daily=0.0)
        import scan_spend
        stone = {**tomb("k2", "h", "p", "2026-12-01"), **{k: snap[k] for k in snap}, "retired_on": "2026-12-01"}
        usd, unknown = scan_spend.snapshot_sums(stone, "2026-12", "2026-11-14", "2026-12-13")


class RoundSixP8UsageSnapshotEdges(unittest.TestCase):
    def test_duplicate_day_rows_summed(self):
        s = snapshot_of([row("2026-10-13", 0.25), row("2026-10-13", 0.25)], AT, "2026-10-14", daily=0.0)
        self.assertEqual(s["usage_by_day"]["2026-10-13"], 0.5)
        self.assertTrue(s["reconciled"])

    def test_future_day_row(self):
        """A report row dated after today (provider clock ahead): dropped from usage_by_day but counted?"""
        s = snapshot_of([row("2026-10-13", 0.25), row("2026-10-15", 9.0)], AT, "2026-10-14", daily=0.0)

    def test_bool_counter(self):
        s = snapshot_of([], AT, "2026-10-14", daily=True)
        self.assertFalse(s["reconciled"])

    def test_nan_counter(self):
        s = snapshot_of([], AT, "2026-10-14", daily=float("nan"))
        self.assertFalse(s["reconciled"])


class RoundSixP9Lock(unittest.TestCase):
    def test_reentrant_depth_restored_after_exception(self):
        import openrouter
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "LEDGER", Path(tmp) / "l.json"):
            try:
                with openrouter._ledger_lock():
                    with openrouter._ledger_lock():
                        raise ValueError("x")
            except ValueError:
                pass
            self.assertEqual(getattr(openrouter._LOCK_STATE, "depth", 0), 0)
            import threading
            got = []

            def other():
                with openrouter._ledger_lock():
                    got.append(1)
            t = threading.Thread(target=other)
            t.start()
            t.join(5)
            self.assertEqual(got, [1])

    def test_two_threads_exclusive(self):
        import openrouter
        import threading
        import time
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "LEDGER", Path(tmp) / "l.json"), \
                patch.object(openrouter, "POLICY_LOCK_WAIT_S", 0.3):
            inside = threading.Event()
            release = threading.Event()

            def holder():
                with openrouter._ledger_lock():
                    inside.set()
                    release.wait(5)
            t = threading.Thread(target=holder)
            t.start()
            inside.wait(5)
            err = None
            try:
                with openrouter._ledger_lock():
                    pass
            except RuntimeError as exc:
                err = str(exc)
            release.set()
            t.join(5)
            self.assertIsNotNone(err)
            self.assertIn("0.3 s", err)


class RoundSixCrashEachStep(unittest.TestCase):
    """probe_r6b of round 6: a crash at each step of a fresh rotation, then a retry, converges on one live key
    (the row's, named for it) and one tombstone for the predecessor (R6-6)."""
    def run_case(self, fail_at):
        import openrouter
        fake = Fake({"h-old": {"name": "k1", "rows": [("2026-10-05", 4.0)], "daily": 0.0, "deleted": False}},
                      "2026-10-14")
        issued = {"k1": ledger_row("acct-a", "h-old", "alpha-web")}
        n = {"saves": 0, "calls": 0}
        real_save = openrouter.save_ledger
        with tempfile.TemporaryDirectory() as tmp, patch.object(openrouter, "datetime", clock(AT)):
            def prov(path, key, payload=None, method=None):
                n["calls"] += 1
                if ("call", n["calls"]) == fail_at:
                    if method == "DELETE":   # deleted, answer lost
                        fake(path, key, payload, method)
                    raise RuntimeError("openrouter request failed: TimeoutError")
                return fake(path, key, payload, method)
            door = Door(tmp, issued, prov)

            def save(doc):
                n["saves"] += 1
                if ("save", n["saves"]) == fail_at:
                    raise OSError("disk full")
                real_save(doc)
            try:
                with patch.object(openrouter, "save_ledger", save):
                    try:
                        openrouter.rotate_one("k1"); first = "ok"
                    except Exception as exc:
                        first = type(exc).__name__
                n["calls"] = n["saves"] = -10**6
                second = "skipped"
                if first != "ok":
                    try:
                        openrouter.rotate_one("k1"); second = "ok"
                    except Exception as exc:
                        second = repr(exc)[:100]
                led = door.ledger()
            finally:
                door.close()
        return first, second, led, fake

    def test_each(self):
        for fail_at in [("call", i) for i in range(1, 10)] + [("save", i) for i in range(1, 6)]:
            first, second, led, fake = self.run_case(fail_at)
            row = led["issued"]["k1"]
            live = sorted(h for h, k in fake.keys.items() if not k["deleted"])
            stones = [s["hash"] for s in led.get("retired", [])]
            with self.subTest(fail_at=fail_at):
                self.assertEqual(live, [row["hash"]], "a live key the ledger does not hold, or the row's key gone")
                self.assertEqual(fake.keys[row["hash"]]["name"], "k1")
                self.assertEqual(stones.count("h-old"), 1)


import openrouter as OR7  # noqa: E402
import scan_spend  # noqa: E402


class Crash7(BaseException):
    """A kill: nothing in the door catches it."""


class World7:
    """A fake provider + a fake slot: the slot holds a VALUE; each value maps to the hash the provider minted."""

    def __init__(self, keys, today="2026-10-14"):
        self.fake = Fake(keys, today)
        self.value_hash = {"VALUE-OLD": "h-old"}
        self.slot = "VALUE-OLD"
        self.faults = {}          # ("call", i) | ("save", i) | ("deliver", i) -> mode
        self.ncall = 0
        self.nsave = 0
        self.ndeliver = 0
        self.events = []

    def provider(self, path, key, payload=None, method=None):
        i = self.ncall
        self.ncall += 1
        mode = self.faults.get(("call", i))
        if mode == "crash-before":
            raise Crash7(f"call {i} {method} {path}")
        if mode == "5xx-before":
            raise RuntimeError("openrouter answered HTTP 502; provider response withheld")
        out = self.fake(path, key, payload, method)
        if path == "/keys" and payload:
            self.value_hash[out["key"]] = out["data"]["hash"]
        self.events.append((method or ("POST" if payload else "GET"), path))
        if mode == "crash-after":
            raise Crash7(f"after call {i} {method} {path}")
        if mode == "lost-after":
            raise RuntimeError("openrouter request failed: TimeoutError")
        return out

    def deliver(self, value, to, **o):
        i = self.ndeliver
        self.ndeliver += 1
        mode = self.faults.get(("deliver", i))
        if mode == "crash-before":
            raise Crash7("deliver before")
        if mode == "refuse":
            raise RuntimeError("Vault refused credential delivery; child output withheld")
        self.slot = value
        if mode == "crash-after":
            raise Crash7("deliver after write")
        if mode == "fail-after-write":
            raise RuntimeError("Vault refused credential delivery; child output withheld")
        return "slot"

    def slot_hash(self):
        return self.value_hash.get(self.slot)


def with_door7(world, issued, fn, when=AT):
    with tempfile.TemporaryDirectory() as tmp, patch.object(OR7, "datetime", clock(when)):
        door = Door(tmp, issued, world.provider)
        real_save = OR7.save_ledger

        def save(doc):
            i = world.nsave
            world.nsave += 1
            mode = world.faults.get(("save", i))
            if mode == "crash-before":
                raise Crash7(f"save {i}")
            if mode == "oserror":
                raise OSError(28, "No space left on device")
            real_save(doc)
            if mode == "crash-after":
                raise Crash7(f"after save {i}")
        ps = [patch.object(OR7, "deliver", world.deliver), patch.object(OR7, "save_ledger", save)]
        for p in ps:
            p.start()
        try:
            return fn(door)
        finally:
            for p in ps:
                p.stop()
            door.close()


def run_rotate7(world, ledger_doc, when=AT):
    """Run rotate_one on `ledger_doc`; return (result-or-exception, ledger after)."""
    def fn(door):
        try:
            out = OR7.rotate_one("k1")
        except BaseException as exc:  # noqa: BLE001
            out = exc
        return out, door.ledger()
    return with_door7(world, ledger_doc, fn, when)


def live_untracked7(world, doc):
    """Live keys at the fake provider that no ledger row nor undeleted tombstone tracks."""
    tracked = set()
    for rec in doc.get("issued", {}).values():
        tracked.update(scan_spend.row_hashes(rec))
    for st in doc.get("retired", []):
        if st.get("deleted", True) is not True and st.get("hash"):
            tracked.add(st["hash"])
    return sorted(h for h, k in world.fake.keys.items() if not k["deleted"] and h not in tracked)


class RoundSevenStaleSnapshotReusedWhileAlive(unittest.TestCase):
    """R6-6 retire path: a stale successor whose DELETE failed (5xx) keeps the snapshot written on day 1. It is
    still live and spends (a consumer cached it). The next rotate (day 3) reuses the day-1 snapshot without
    re-reading, DELETEs, and writes a reconciled tombstone: its day-2 spend is lost while the project reads
    whole."""

    def test(self):
        world = World7({"h-old": {"name": "k1", "rows": [("2026-10-05", 1.0)], "daily": 0.0, "deleted": False}})
        doc = {"issued": {"k1": ledger_row("acct-a", "h-old", "alpha-web")}}
        # run 1: killed right after the vault wrote the successor
        world.faults = {("deliver", 0): "crash-after"}
        out1, doc = run_rotate7(world, doc)
        s1 = world.slot_hash()
        # run 2 (same day): the stale successor's DELETE answers 502
        world.ncall = world.nsave = world.ndeliver = 0
        world.faults = {}
        orig = world.provider
        state = {"fail": True}

        def prov(path, key, payload=None, method=None):
            if method == "DELETE" and path == f"/keys/{s1}" and state["fail"]:
                state["fail"] = False
                raise RuntimeError("openrouter answered HTTP 502; provider response withheld")
            return orig(path, key, payload, method)
        world.provider = prov
        out2, doc = run_rotate7(world, doc)
        rec2 = doc["issued"]["k1"]
        # day 2: the stale successor spends (a consumer still holds its value)
        world.fake.keys[s1]["rows"].append(("2026-10-15", 2.0))
        world.fake.today = "2026-10-16"
        world.provider = orig
        world.ncall = world.nsave = world.ndeliver = 0
        out3, doc = run_rotate7(world, doc, when=utc(2026, 10, 16, 12))
        stones = {s["hash"]: s for s in doc["retired"]}
        account = [{"date": "2026-10-05", "usage": 1.0}, {"date": "2026-10-15", "usage": 2.0}]
        res = ProjectSpend().scan_at(utc(2026, 10, 17, 12), ledger=doc,
                                       keys={h: (404 if k["deleted"] else [{"date": d + " 00:00:00", "usage": u}
                                                                            for d, u in k["rows"]])
                                             for h, k in world.fake.keys.items()}, account=account)
        alpha = res["projects"]["alpha-web"]
        self.assertTrue(alpha["month_usd"] is None or abs(alpha["month_usd"] - 3.0) < 1e-6,
                        f"whole and lower: alpha month {alpha['month_usd']} (true 3.0)")


class RoundSevenIssueOverRowWithStaleSuccessor(unittest.TestCase):
    """A rotation killed after delivery leaves S1 (in the slot) as successor_pending/stale. The old key is then
    deleted at the dashboard. `issue k1` sees no key called k1, retires the row by its hash only, and the row's
    stale successor (live, in use, spending) leaves the ledger."""

    def test(self):
        world = World7({"h-old": {"name": "k1", "rows": [("2026-10-05", 1.0)], "daily": 0.0, "deleted": False}})
        doc = {"issued": {"k1": ledger_row("acct-a", "h-old", "alpha-web")}}
        world.faults = {("deliver", 0): "crash-after"}
        _out, doc = run_rotate7(world, doc)
        s1 = world.slot_hash()
        world.fake.keys[s1]["rows"].append(("2026-10-06", 2.0))
        world.fake.keys["h-old"]["deleted"] = True          # the operator deletes the old key at the dashboard
        world.faults = {}

        def fn(door):
            try:
                with patch.object(OR7, "vault_destination", lambda to: to), \
                        patch.object(OR7, "_all_keys", lambda admin, include_disabled=True: [
                            {"name": k["name"], "hash": h} for h, k in world.fake.keys.items() if not k["deleted"]]):
                    out = OR7.issue_key("k1", 10, "acct-a", "vault:x/prod/K", "alpha-web")
            except BaseException as exc:  # noqa: BLE001
                out = exc
            return out, door.ledger()
        out, doc = with_door7(world, doc, fn)
        self.assertIsInstance(out, ValueError)
        self.assertIn("run `rotate k1` first", str(out))
        self.assertIn("OBS-119", str(out))
        res = ProjectSpend().scan_at(utc(2026, 10, 16, 12), ledger=doc,
                                       keys={h: (404 if k["deleted"] else [{"date": d + " 00:00:00", "usage": u}
                                                                            for d, u in k["rows"]])
                                             for h, k in world.fake.keys.items()},
                                       account=[{"date": "2026-10-05", "usage": 1.0},
                                                {"date": "2026-10-06", "usage": 2.0}])
        alpha = res["projects"]["alpha-web"]
        world.fake.keys[s1]["rows"].append(("2026-12-10", 5.0))
        new_hash = doc["issued"]["k1"]["hash"]
        later = ProjectSpend().scan_at(utc(2026, 12, 20, 12), ledger=doc,
                                         keys={h: (404 if k["deleted"] else [{"date": d + " 00:00:00", "usage": u}
                                                                              for d, u in k["rows"]])
                                               for h, k in world.fake.keys.items()},
                                         account=[{"date": "2026-12-10", "usage": 5.0}])
        a2 = later["projects"]["alpha-web"]
        self.assertEqual(live_untracked7(world, doc), [], "a live key left the ledger untracked")
        self.assertTrue(alpha["month_usd"] is None or abs(alpha["month_usd"] - 3.0) < 1e-6,
                        f"whole and lower: {alpha['month_usd']}")

class RoundSevenGaps(unittest.TestCase):
    """The test gaps of verification round 7 (M1, M11)."""

    def test_a_refused_fresh_delivery_leaves_the_stale_successor_alive(self):
        """M1: stale successors are retired only after a newer one is delivered; a delivery that fails leaves the
        stale one, possibly the slot's key, alive."""
        world = World7({"h-old": {"name": "k1", "rows": [], "daily": 0.0, "deleted": False}})
        doc = {"issued": {"k1": ledger_row("acct-a", "h-old", "alpha-web")}}
        world.faults = {("deliver", 0): "crash-after"}
        _out, doc = run_rotate7(world, doc)
        s1 = world.slot_hash()
        world.ncall = world.nsave = world.ndeliver = 0
        world.faults = {("deliver", 0): "refuse"}
        out, doc = run_rotate7(world, doc)
        self.assertIsInstance(out, RuntimeError)
        self.assertFalse(world.fake.keys[s1]["deleted"])
        self.assertEqual(world.slot_hash(), s1)
        self.assertIn(s1, scan_spend.row_hashes(doc["issued"]["k1"]))

    def test_the_scan_reads_a_stale_successor(self):
        """M11."""
        led = {"issued": {"k1": ledger_row("acct-a", "h-cur", "alpha-web", stale_successors=[{"hash": "h-st"}])}}
        doc = ProjectSpend().scan(ledger=led, keys={"h-cur": [row("2026-10-05", 1.0)], "h-st": [row("2026-10-06", 2.0)]},
                                  account=[row("2026-10-05", 1.0), row("2026-10-06", 2.0)])
        self.assertIn("h-st", doc["_asked"])
        self.assertEqual((doc["projects"]["alpha-web"]["month_usd"], doc["unattributed"]["month_usd"]), (3.0, 0.0))


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
