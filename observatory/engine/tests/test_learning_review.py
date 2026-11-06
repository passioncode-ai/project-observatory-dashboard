#!/usr/bin/env python3
"""The daily learning review (OBS-331 part a): the UTC day and when it is due, lessons and their classes, rising
failures from products' daily digests, late arrivals, the report's files and what it never carries.

Every value is synthetic: two throwaway Git clones give two pseudonymous project keys, lessons are written through
`store.lessons.write` with the ledger's clock fixed, digest lines through `ops_digest_store.record` with its clock
fixed, and the review runs at a fixed `now`. Nothing reaches a network: the privacy case makes sockets and URL
openers raise for the whole run.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import socket
import stat
import subprocess
import sys
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402
from git_env import git_env  # noqa: E402

D = date(2026, 11, 5)
ALPHA, BETA = "project:alpha-web", "project:beta-api"
CLASS = "absence-needs-its-own-check"
#: Modules that bind a workspace's paths when they are imported: each test loads them on its own workspace.
CACHED = ("paths", "configuration", "store", "tools", "learning_review", "ops_digest_store", "memory_redact",
          "scan_leaks", "learning_review_cli", "atomic")


def at(day: date, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def git(cwd: pathlib.Path, *args: str) -> str:
    out = subprocess.run(["git", "-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture", *args],
                         cwd=cwd, env=git_env(), capture_output=True, text=True, timeout=60, check=True)
    return out.stdout.strip()


def clone(name: str) -> tuple[pathlib.Path, str, str]:
    """A repository with a root of its own (its key) and its head commit."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix=f"observatory-review-{name}-")).resolve()
    git(d, "init", "-q", "-b", "main")
    (d / "readme.txt").write_text(f"{name}\n", encoding="utf-8")
    git(d, "add", "-A")
    git(d, "commit", "-qm", f"root of {name}")
    root = sorted(git(d, "rev-list", "--max-parents=0", "HEAD").split())[0]
    return d, "p-" + root[:12], git(d, "rev-parse", "HEAD")


CLONES: dict[str, tuple[pathlib.Path, str, str]] = {}


def setUpModule() -> None:
    CLONES["alpha-web"] = clone("alpha-web")
    CLONES["beta-api"] = clone("beta-api")


class Clock:
    """The ledger's and the digest store's `now`, moved by the test."""

    def __init__(self, when: datetime) -> None:
        self.when = when

    def stamp(self) -> str:
        return self.when.strftime("%Y-%m-%dT%H:%M:%SZ")


class Workspace(unittest.TestCase):
    """A fresh workspace per test: registry naming the two clones, an empty store, a fixed clock."""

    def setUp(self) -> None:
        self.home = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-review-home-")).resolve()
        os.environ["OBSERVATORY_HOME"] = str(self.home)
        os.environ["OBSERVATORY_STATE"] = str(self.home / "store")
        os.environ["OBSERVATORY_DB"] = str(self.home / "store" / "observatory.db")  # paths-check: allow (an isolated workspace's store)
        os.environ["OBSERVATORY_SCRATCH"] = str(self.home / "scratch")
        os.environ.pop("OBSERVATORY_REGISTRY", None)
        (self.home / "scratch").mkdir(parents=True)
        for name in [k for k in list(sys.modules) if k in CACHED or k.split(".")[0] in ("store", "tools")]:
            sys.modules.pop(name, None)
        import paths
        self.paths = paths
        reg = pathlib.Path(paths.REGISTRY)
        reg.mkdir(parents=True, exist_ok=True)
        (reg / "projects.json").write_text(json.dumps({"projects": [{"id": ALPHA}, {"id": BETA}]}))
        (reg / "repositories.json").write_text(json.dumps({"schema_version": 2, "repositories": [
            {"id": f"repository:example-org/{n}", "name_with_owner": f"example-org/{n}", "host": "github",
             "local": {"path": str(CLONES[n][0])}} for n in ("alpha-web", "beta-api")]}))
        (reg / "relations.json").write_text(json.dumps({"relations": [
            {"type": "implemented_by", "from": ALPHA, "to": "repository:example-org/alpha-web"},
            {"type": "implemented_by", "from": BETA, "to": "repository:example-org/beta-api"}]}))
        from store import db as sdb
        from store import ledger as L
        from store import lessons as LS
        import learning_review as LR
        import ops_digest as OD
        import ops_digest_store as S
        import memory_redact
        self.sdb, self.L, self.LS, self.LR, self.OD, self.S = sdb, L, LS, LR, OD, S
        self.clock = Clock(at(D, 10))
        self.patches = [patch.object(L, "_now", self.clock.stamp),
                        patch.object(LR, "_redactor", lambda: memory_redact.Redactor(known_loader=dict)),
                        patch.object(LR, "local_offset", lambda at=None: "+01:00")]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    # ── writing the world ──

    def lesson(self, name: str | None, *, key: str | None = None, commit: str | None = None,
               title: str = "An absent input must fail its own check", class_key: str = CLASS) -> dict:
        if name is not None:
            _path, key, commit = CLONES[name]
        body = self.LS.body_of({"classKey": class_key, "title": title, "trigger": "a gate reads an optional file",
                                "check": "docs gate rule absent-file", "nouns": ["gate", "absence", "file"],
                                "origin": {"projectId": key, "commit": commit}})
        conn = self.sdb.connect()
        try:
            out = self.LS.write(conn, self.L, owner="agent:task-pipeline", body=body,
                                provenance=[{"source": "test"}])
            conn.commit()
        finally:
            conn.close()
        return out

    def digest(self, project: str, day: date, failed: dict[str, list[int]], *, received: datetime | None = None,
               ok: dict[str, int] | None = None, unknown: dict[str, int] | None = None, source: str = "app",
               revision_of: dict | None = None) -> dict:
        """A daily line folded by the engine's port of the contract's folds: `failed[op][h]` failures in hour h."""
        hours = max(len(v) for v in failed.values())
        hourly = []
        for h in range(hours):
            counts = [{"op": op, "ok": (ok or {}).get(op, 1) if h == 0 else 0,
                       "failed": v[h] if h < len(v) else 0,
                       "unknown": (unknown or {}).get(op, 0) if h == 0 else 0} for op, v in failed.items()]
            hourly.append(self.OD.fold_hour(counts, f"{day.isoformat()}T{h:02d}:00:00Z",
                                            hourly[-1] if hourly else None))
        line = revision_of or self.OD.fold_day(day.isoformat(), hourly)
        self.record(project, line, received or at(day + timedelta(days=1), 1), source)
        return line

    def record(self, project: str, line: dict, received: datetime, source: str = "app") -> None:
        fixed = received

        class Fixed(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        with patch.object(self.S, "datetime", Fixed):
            self.S.record(project, None, source, line, "binding:bnd_test0001", today=received.date())

    def review(self, now: datetime, **kw) -> dict:
        self.clock.when = now
        return self.LR.run(now, **kw)

    def report(self, day: date) -> dict:
        return json.loads(self.LR.report_path(day).read_text(encoding="utf-8"))

    def html(self, day: date) -> str:
        return self.LR.report_path(day, ".html").read_text(encoding="utf-8")

    def state(self, mid: str) -> str:
        conn = self.sdb.connect()
        try:
            return conn.execute("SELECT state FROM ledger WHERE memory_id = ? ORDER BY revision DESC LIMIT 1",
                                (mid,)).fetchone()[0]
        finally:
            conn.close()

    def receipt(self, checked_on: str) -> None:
        (pathlib.Path(self.paths.SCRATCH) / "corroboration.json").write_text(json.dumps({"checked_on": checked_on}))


class DaysAndTiming(Workspace):
    def test_the_reviewed_day_is_a_utc_day(self) -> None:
        self.review(at(D + timedelta(days=1), 7))
        doc = self.report(D)
        self.assertEqual((doc["day"], doc["boundary"]), ("2026-11-05", "utc"))
        self.assertEqual(doc["window"], {"from": "2026-11-05T00:00:00Z", "to": "2026-11-06T00:00:00Z",
                                         "localOffset": "+01:00"})
        self.assertIn("01:00 to 01:00 the next day", self.html(D))

    def test_not_due_before_six_utc_and_due_after(self) -> None:
        LR = self.LR
        self.assertEqual(LR.newest_due_day(at(D + timedelta(days=1), 5, 59), 6), D - timedelta(days=1))
        self.assertEqual(LR.newest_due_day(at(D + timedelta(days=1), 6), 6), D)
        self.review(at(D + timedelta(days=1), 7))
        self.assertFalse(LR.is_due(at(D + timedelta(days=2), 5, 59)), "D's report is written; D+1 is not due yet")
        self.assertTrue(LR.is_due(at(D + timedelta(days=2), 6)))
        with patch.object(LR, "utc_now", lambda: at(D + timedelta(days=2), 5, 59)):
            self.assertEqual(LR.main(["--due"]), 3)
        with patch.object(LR, "utc_now", lambda: at(D + timedelta(days=2), 6)):
            self.assertEqual(LR.main(["--due"]), 0)

    def test_missing_days_are_caught_up_oldest_first_up_to_seven(self) -> None:
        self.review(at(D - timedelta(days=9), 7))          # the last report written: D-10, and the six days before it
        answer = self.review(at(D + timedelta(days=1), 7))   # ten days later
        days = [line.split()[0] for line in answer["written"]]
        self.assertEqual(days, [(D - timedelta(days=n)).isoformat() for n in range(6, -1, -1)])
        self.assertEqual(self.report(D)["daysNotReviewed"],
                         [(D - timedelta(days=n)).isoformat() for n in (9, 8, 7)])
        self.assertFalse(self.LR.report_path(D - timedelta(days=8)).exists(), "an older missing day is never written")
        self.assertEqual(self.report(D - timedelta(days=6))["daysNotReviewed"], [], "named in the newest report only")

    def test_a_complete_report_is_never_rewritten_without_again(self) -> None:
        self.review(at(D + timedelta(days=1), 7))
        before = self.LR.report_path(D).read_bytes()
        self.assertEqual(self.review(at(D + timedelta(days=1), 9))["outcome"], "not-due")
        self.assertEqual(self.review(at(D + timedelta(days=1), 9), day=D)["outcome"], "already-complete")
        self.assertEqual(self.LR.report_path(D).read_bytes(), before)

    def test_again_keeps_the_previous_revision(self) -> None:
        self.review(at(D + timedelta(days=1), 7))
        first = self.report(D)
        out = self.review(at(D + timedelta(days=1), 9), day=D, again=True)
        self.assertEqual(out["outcome"], "written")
        self.assertEqual(self.report(D)["revision"], 2)
        kept = json.loads((self.LR.report_path(D).parent / f"{D.isoformat()}.r1.json").read_text())
        self.assertEqual(kept, first)

    def test_a_partial_report_is_rewritten_until_two_days_after(self) -> None:
        folder = pathlib.Path(self.paths.STATE) / "ops-digests" / "alpha-web"
        folder.mkdir(parents=True)
        (folder / "2026-11.json").write_text("not this store's document")
        self.review(at(D + timedelta(days=1), 7))
        self.assertEqual(self.report(D)["state"], "partial")
        self.assertNotIn(D, self.LR.plan(at(D + timedelta(days=1), 9), 6)["days"],
                         "nothing it reads changed: not rebuilt at every tick")
        self.assertIn(D, self.LR.plan(at(D + timedelta(days=1), 13), 6)["days"], "retried after RETRY_HOURS")
        self.assertNotIn(D, self.LR.plan(at(D + timedelta(days=2), 6), 6)["days"], "kept partial after D+2 06:00")
        (folder / "2026-11.json").unlink()
        self.assertIn(D, self.LR.plan(at(D + timedelta(days=1), 9), 6)["days"], "its input changed: due again")
        self.review(at(D + timedelta(days=1), 9))
        self.assertEqual(self.report(D)["state"], "complete")
        self.assertEqual(self.report(D)["revision"], 1, "a rewrite of a partial report is not a new revision")

    def test_a_damaged_report_is_kept_aside_and_written_again(self) -> None:
        self.review(at(D + timedelta(days=1), 7))
        self.LR.report_path(D).write_bytes(b"\x00damaged report")
        with self.assertRaises(self.LR.Refused) as caught:
            self.LR.read_report(D)
        self.assertEqual(caught.exception.code, "report-unreadable")
        self.assertIn("damaged-", self.LR.HINTS["report-unreadable"])
        self.assertEqual(self.review(at(D + timedelta(days=1), 9), day=D, again=True)["outcome"], "written")
        kept = self.LR.report_path(D).parent / f"{D.isoformat()}.damaged-1.json"
        self.assertEqual(kept.read_bytes(), b"\x00damaged report")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(kept.stat().st_mode), 0o600)
        doc = self.report(D)
        self.assertEqual(doc["state"], "complete", "a kept damaged file is a note, not a partial source")
        self.assertIn("report-damaged-kept", [d["code"] for d in doc["degraded"]])
        self.assertIn(D, self.LR.report_days(), "the damaged copy is not read as a report of its own")

    def test_a_day_not_closed_or_out_of_retention_is_refused(self) -> None:
        now = at(D + timedelta(days=1), 7)
        for day, code in ((D + timedelta(days=1), "day-not-closed"),
                          (D - timedelta(days=self.LR.KEEP_DAYS + 1), "day-outside-retention")):
            with self.assertRaises(self.LR.Refused) as caught:
                self.review(now, day=day)
            self.assertEqual(caught.exception.code, code)


class Lessons(Workspace):
    def test_a_new_lesson_is_listed_on_the_day_it_was_minted(self) -> None:
        out = self.lesson("alpha-web")
        self.review(at(D + timedelta(days=1), 7))
        items = self.report(D)["lessons"]
        self.assertEqual([(i["code"], i["memoryId"], i["revision"], i["project"], i["state"]) for i in items],
                         [("lesson-new", out["memoryId"], 1, ALPHA, "proposed")])
        self.assertEqual(items[0]["source"], f"ledger:{out['memoryId']}@1")
        self.assertEqual(items[0]["origin"]["projectId"], CLONES["alpha-web"][1])
        self.assertEqual(self.report(D - timedelta(days=1))["lessons"], [], "placed on its own day only")

    def test_revised_replaced_promoted_and_invalidated_are_told_apart(self) -> None:
        def row(mid, rev, state, commit="commit:aaaaaaaaaaaa", prov=(), day="2026-11-05"):
            return {"memory_id": mid, "revision": rev, "state": state, "created_at": f"{day}T10:00:00Z",
                    "body": {"classKey": CLASS, "origin": {"projectId": "p-0123456789ab", "commit": commit}},
                    "provenance": list(prov)}
        rows = [row("mem:a", 1, "proposed"), row("mem:a", 2, "proposed"),
                row("mem:b", 1, "proposed"),
                row("mem:b", 2, "observed", prov=[{"source": "corroboration", "by": "agent:lesson-corroborator"}]),
                row("mem:b", 3, "archived"),
                row("mem:c", 1, "proposed", commit="commit:bbbbbbbbbbbb", prov=[{"replaces": "mem:b@3"}]),
                row("mem:d", 1, "proposed", prov=[{"source": "lesson-evidence-revalidation"},
                                                  {"replaces": "mem:b@3"}])]
        codes = {(r["memory_id"], r["revision"]): r["code"] for r in self.LR.classify(rows)}
        self.assertEqual(codes, {("mem:a", 1): "lesson-new", ("mem:a", 2): "lesson-revised",
                                 ("mem:b", 1): "lesson-new", ("mem:b", 2): "lesson-promoted",
                                 ("mem:c", 1): "lesson-replaced", ("mem:d", 1): "observation-invalidated"})
        # And through the store: the same project meeting the class again revises its one record.
        first = self.lesson("alpha-web")
        again = self.lesson("alpha-web", title="An absent input must fail a check of its own")
        self.assertEqual((again["memoryId"], again["outcome"]), (first["memoryId"], "revised"))
        self.review(at(D + timedelta(days=1), 7))
        self.assertEqual([i["code"] for i in self.report(D)["lessons"]], ["lesson-new", "lesson-revised"])

    def test_one_class_from_two_keys_is_one_repeated_class(self) -> None:
        a, b = self.lesson("alpha-web"), self.lesson("beta-api")
        self.receipt((D + timedelta(days=1)).isoformat())      # a refreshing tick ran the corroborator today: dry
        self.review(at(D + timedelta(days=1), 7))
        classes = self.report(D)["classes"]
        self.assertEqual(len(classes), 1)
        c = classes[0]
        self.assertEqual((c["code"], c["classKey"], c["keys"], c["resolved"]), ("class-repeated", CLASS, 2, 2))
        self.assertEqual(sorted(c["members"]), sorted([f"ledger:{a['memoryId']}@1", f"ledger:{b['memoryId']}@1"]))

    def test_the_same_key_twice_is_not_a_repeat(self) -> None:
        self.lesson("alpha-web")
        self.lesson("alpha-web", title="An absent input must fail a check of its own")
        self.review(at(D + timedelta(days=1), 7))
        self.assertEqual(self.report(D)["classes"], [])

    def test_promotion_is_the_corroborators_and_runs_only_when_its_receipt_is_a_day_old(self) -> None:
        a, b = self.lesson("alpha-web"), self.lesson("beta-api")
        self.receipt((D + timedelta(days=1)).isoformat())
        self.review(at(D + timedelta(days=1), 7))
        self.assertEqual(self.report(D)["sources"]["corroborator"]["state"], "corroborator-dry")
        self.assertEqual((self.state(a["memoryId"]), self.state(b["memoryId"])), ("proposed", "proposed"))
        self.receipt(D.isoformat())                          # yesterday's: no refreshing tick ran it today
        self.review(at(D + timedelta(days=2), 7))
        doc = self.report(D + timedelta(days=1))
        self.assertEqual(doc["sources"]["corroborator"]["state"], "corroborator-ran")
        self.assertEqual((self.state(a["memoryId"]), self.state(b["memoryId"])), ("observed", "observed"))
        self.review(at(D + timedelta(days=3), 7))
        promoted = [i for i in self.report(D + timedelta(days=2))["lessons"] if i["code"] == "lesson-promoted"]
        self.assertEqual(sorted(i["memoryId"] for i in promoted), sorted([a["memoryId"], b["memoryId"]]))
        conn = self.sdb.connect()
        try:
            provenance = conn.execute("SELECT provenance_json FROM ledger WHERE memory_id = ? ORDER BY revision DESC"
                                      " LIMIT 1", (a["memoryId"],)).fetchone()[0]
        finally:
            conn.close()
        self.assertIn("agent:lesson-corroborator", provenance, "the corroborator promoted it, by its own rule")

    def test_an_unresolvable_key_is_named_and_witnesses_nothing(self) -> None:
        self.lesson("alpha-web")
        stray = self.lesson(None, key="p-0123456789ab", commit="0123456789abcdef")
        self.review(at(D + timedelta(days=1), 7))
        doc = self.report(D)
        item = next(i for i in doc["lessons"] if i["memoryId"] == stray["memoryId"])
        self.assertIsNone(item["project"])
        c = doc["classes"][0]
        self.assertEqual((c["keys"], c["resolved"], c["outcome"]), (2, 1, "not-resolvable-here"))
        self.assertIn("not resolvable here", c["why"])
        self.assertEqual(self.state(stray["memoryId"]), "proposed")


class Bounded(Workspace):
    def test_the_corroborator_out_of_time_makes_the_report_partial(self) -> None:
        a, b = self.lesson("alpha-web"), self.lesson("beta-api")
        with patch.object(self.LR, "CORROBORATOR_SECONDS", 0.0):
            self.review(at(D + timedelta(days=1), 7))
        doc = self.report(D)
        self.assertEqual((doc["state"], doc["sources"]["corroborator"]["state"]), ("partial", "corroborator-failed"))
        self.assertIn("out of time", next(d["reason"] for d in doc["degraded"] if d["code"] == "corroborator-failed"))
        self.assertEqual(doc["summary"]["lessonsNew"], 2, "the lessons are listed all the same")
        self.assertEqual((self.state(a["memoryId"]), self.state(b["memoryId"])), ("proposed", "proposed"))

    def test_the_clones_are_swept_once_and_the_sweep_is_bounded(self) -> None:
        self.lesson("alpha-web")
        from tools import corroborate as C
        calls = []
        real = C.project_keys

        def counted(**kw):
            calls.append(kw.get("deadline"))
            return real(**kw)
        with patch.object(C, "project_keys", counted):
            self.review(at(D + timedelta(days=1), 7))
        self.assertEqual(len(calls), 1, "one sweep, shared with the corroborator")
        self.assertIsNotNone(calls[0], "and it has a deadline")
        with patch.object(self.LR, "KEYS_SECONDS", 0.0):
            self.review(at(D + timedelta(days=1), 9), day=D, again=True)
        self.assertEqual(self.report(D)["sources"]["corroborator"]["state"], "corroborator-failed")

    def test_the_reviews_own_corroborator_run_promotes_but_never_archives(self) -> None:
        a, b = self.lesson("alpha-web"), self.lesson("beta-api")
        self.review(at(D + timedelta(days=1), 7))
        self.assertEqual(self.state(a["memoryId"]), "observed")
        reg = pathlib.Path(self.paths.REGISTRY) / "repositories.json"
        doc = json.loads(reg.read_text())
        doc["repositories"] = [r for r in doc["repositories"] if not r["id"].endswith("beta-api")]
        reg.write_text(json.dumps(doc))                    # beta's support is gone from this machine
        self.review(at(D + timedelta(days=2), 7))
        self.assertEqual(self.state(a["memoryId"]), "observed",
                         "revalidation stays the tick's run after a registry refresh")

    def test_lines_and_late_arrivals_are_bounded(self) -> None:
        self.digest(ALPHA, D, {"ipc.call": [1]})
        self.digest(BETA, D, {"ipc.call": [1]})
        with patch.object(self.LR, "MAX_LINES", 1):
            self.review(at(D + timedelta(days=1), 7))
        doc = self.report(D)
        self.assertEqual((len(doc["lines"]), doc["truncated"]["lines"]), (1, 1))
        self.assertIn("arrivedLate", doc["truncated"])
        self.assertIn("items-truncated", [d["code"] for d in doc["degraded"]])

    def test_an_unreadable_registry_is_named_not_every_project_flagged(self) -> None:
        self.digest(ALPHA, D, {"ipc.call": [1]})
        (pathlib.Path(self.paths.REGISTRY) / "projects.json").write_text("{not json")
        self.review(at(D + timedelta(days=1), 7))
        doc = self.report(D)
        self.assertEqual([f for f in doc["failures"] if f["code"] == "project-not-in-registry"], [])
        self.assertIn("registry-unreadable", [d["code"] for d in doc["degraded"]])
        self.assertEqual(doc["state"], "partial")


class Failures(Workspace):
    def failures(self, day: date = D, code: str | None = None) -> list[dict]:
        return [f for f in self.report(day)["failures"] if code is None or f["code"] == code]

    def test_a_contract_rise_is_read_from_the_first_revision(self) -> None:
        self.digest(ALPHA, D, {"ipc.call": [2, 12]})                        # rose in its second hour: 12 after 2
        later = self.OD.fold_day(D.isoformat(), [self.OD.fold_hour([{"op": "ipc.call", "ok": 9, "failed": 0,
                                                                     "unknown": 0}], f"{D}T00:00:00Z", None)])
        self.record(ALPHA, later, at(D + timedelta(days=1), 2))
        self.review(at(D + timedelta(days=1), 7))
        rises = self.failures(code="risen-in-hour")
        self.assertEqual([(r["op"], r["failed"], r["hours"], r["project"]) for r in rises],
                         [("ipc.call", 12, 1, ALPHA)])
        ignored = self.failures(code="later-revisions-ignored")
        self.assertEqual([r.get("revisions") for r in ignored], [2])
        self.assertEqual(rises[0]["line"], "ops-digests:alpha-web/2026-11#2026-11-05/app/r1")

    def test_ten_after_five_rose_since_yesterday_and_ten_after_six_did_not(self) -> None:
        self.digest(ALPHA, D - timedelta(days=1), {"a.five": [5], "a.six": [6]})
        self.digest(ALPHA, D, {"a.five": [5, 5], "a.six": [5, 5], "a.zero": [5, 5]})
        self.review(at(D + timedelta(days=1), 7))
        rose = {(r["op"], r["previous"]) for r in self.failures(code="rose-since-yesterday")}
        self.assertEqual(rose, {("a.five", 5), ("a.zero", None)})
        self.assertEqual(self.failures(code="risen-in-hour"), [], "no hour reached ten")

    def test_nine_failures_never_rise(self) -> None:
        self.digest(ALPHA, D, {"a.nine": [9]})
        self.review(at(D + timedelta(days=1), 7))
        self.assertEqual([f for f in self.failures() if f["code"] in self.LR.RISE_CODES], [])

    def test_three_rises_in_seven_days_are_rising_repeatedly(self) -> None:
        for back in (6, 3, 0):
            self.digest(ALPHA, D - timedelta(days=back), {"sync.pull": [5, 6]})
        self.digest(ALPHA, D - timedelta(days=1), {"sync.pull": [1]})
        self.review(at(D + timedelta(days=1), 7))
        rep = self.failures(code="rising-repeatedly")
        self.assertEqual(len(rep), 1)
        self.assertEqual(rep[0]["days"], [(D - timedelta(days=n)).isoformat() for n in (6, 3, 0)])
        self.assertEqual(self.report(D)["summary"]["risingRepeatedly"], 1)
        self.assertEqual(self.failures(D - timedelta(days=3), code="rising-repeatedly"), [], "two days are not three")

    def test_a_line_at_4096_operations_is_names_at_cap(self) -> None:
        counts = [{"op": f"op.n{i}", "ok": 1, "failed": 0, "unknown": 0} for i in range(self.OD.OPS_MAX)]
        line = self.OD.fold_day(D.isoformat(), [self.OD.fold_hour(counts, f"{D}T00:00:00Z", None)])
        self.record(ALPHA, line, at(D + timedelta(days=1), 1))
        self.review(at(D + timedelta(days=1), 7))
        self.assertEqual(len(self.failures(code="names-at-cap")), 1)
        self.assertTrue(self.report(D)["lines"][0]["namesAtCap"])

    def test_a_late_day_is_reported_once_in_the_next_report(self) -> None:
        self.review(at(D + timedelta(days=1), 7))
        self.digest(ALPHA, D - timedelta(days=1), {"ipc.call": [2, 12]}, received=at(D + timedelta(days=1), 10))
        self.review(at(D + timedelta(days=2), 7))
        late = self.report(D + timedelta(days=1))["arrivedLate"]
        self.assertEqual([(x["day"], x["project"]) for x in late], [((D - timedelta(days=1)).isoformat(), ALPHA)])
        self.assertEqual([f["code"] for f in late[0]["failures"]], ["risen-in-hour", "rose-since-yesterday"])
        self.review(at(D + timedelta(days=3), 7))
        self.assertEqual(self.report(D + timedelta(days=2))["arrivedLate"], [], "once, never again")

    def test_a_rebuilt_report_keeps_its_late_lines(self) -> None:
        self.review(at(D, 7))                                                   # D-7 to D-1, complete
        self.digest(ALPHA, D - timedelta(days=2), {"ipc.call": [2, 12]}, received=at(D, 10))
        folder = pathlib.Path(self.paths.STATE) / "ops-digests" / "beta-api"
        folder.mkdir(parents=True)
        (folder / "2026-11.json").write_text("not this store's document")      # D's own read: partial
        self.review(at(D + timedelta(days=1), 7))
        late = lambda: [x["line"] for x in self.report(D)["arrivedLate"]]       # noqa: E731
        first = late()
        self.assertEqual(len(first), 1)
        self.assertEqual(self.report(D)["state"], "partial")
        self.review(at(D + timedelta(days=1), 14))                             # retried: rewritten in place
        self.assertEqual(self.report(D)["writtenAt"], "2026-11-06T14:00:00Z")
        self.assertEqual(late(), first, "a rewritten partial report keeps the late line it listed")
        self.review(at(D + timedelta(days=1), 15), day=D, again=True)
        self.assertEqual((self.report(D)["revision"], late()), (2, first), "--again keeps it too")
        self.review(at(D + timedelta(days=2), 7))
        self.assertEqual(self.report(D + timedelta(days=1))["arrivedLate"], [], "and no other report lists it")

    def test_damage_only_the_late_scan_reads_is_a_note_not_partial(self) -> None:
        day = date(2026, 11, 20)
        self.review(at(date(2026, 10, 26), 7))                                 # reports of 19 to 25 October
        folder = pathlib.Path(self.paths.STATE) / "ops-digests" / "alpha-web"
        folder.mkdir(parents=True)
        (folder / "2026-10.json").write_text("not this store's document")
        self.review(at(day + timedelta(days=1), 7))
        doc = self.report(day)
        self.assertEqual(doc["state"], "complete")
        notes = [d for d in doc["degraded"] if d["code"] == "digest-month-damaged"]
        self.assertEqual([d.get("partial") for d in notes], [False])
        self.assertEqual(doc["daysNotReviewed"][0], "2026-10-26")

    def test_a_project_not_in_the_registry_is_shown_by_its_slug(self) -> None:
        self.digest("project:gamma-ops", D, {"ipc.call": [1]})
        self.review(at(D + timedelta(days=1), 7))
        self.assertEqual([f["project"] for f in self.failures(code="project-not-in-registry")], ["project:gamma-ops"])


class RetrosAndFailureModes(Workspace):
    def test_retro_after_work_ok_is_answered_asks_never_retros_written(self) -> None:
        self.lesson("alpha-web")
        self.digest(ALPHA, D, {"retro-after-work": [0], "ipc.call": [1]}, ok={"retro-after-work": 3},
                    unknown={"retro-after-work": 2})
        self.review(at(D + timedelta(days=1), 7))
        retros = self.report(D)["retros"]
        self.assertEqual(retros, [{"project": ALPHA, "lessonsRecorded": 1, "insightLinesAdded": None,
                                   "retroAfterWork": {"asked": 5, "answeredSeen": 3, "refused": 0, "notSeen": 2}}])
        text = self.LR.report_path(D).read_text(encoding="utf-8")
        self.assertNotIn("retrosWritten", text)
        page = self.html(D)
        self.assertNotIn("<th>retros written", page.lower())
        self.assertIn("answered by a retro the host saw written", page)

    def test_a_damaged_month_makes_the_report_partial_and_keeps_the_file(self) -> None:
        self.digest(BETA, D, {"ipc.call": [1]})
        folder = pathlib.Path(self.paths.STATE) / "ops-digests" / "alpha-web"
        folder.mkdir(parents=True)
        (folder / "2026-11.json").write_bytes(b"\x00damaged")
        self.review(at(D + timedelta(days=1), 7))
        doc = self.report(D)
        self.assertEqual(doc["state"], "partial")
        self.assertEqual(doc["sources"]["digests"]["damaged"], [{"project": ALPHA, "month": "2026-11"}])
        self.assertIn("digest-month-damaged", [d["code"] for d in doc["degraded"]])
        self.assertEqual([r["project"] for r in doc["lines"]], [BETA], "the other project is read")
        self.assertEqual((folder / "2026-11.json").read_bytes(), b"\x00damaged")

    def test_no_digests_is_a_complete_report_of_lessons(self) -> None:
        self.lesson("alpha-web")
        self.review(at(D + timedelta(days=1), 7))
        doc = self.report(D)
        self.assertEqual((doc["state"], doc["sources"]["digests"]["state"]), ("complete", "no-digests"))
        self.assertEqual(doc["summary"]["lessonsNew"], 1)
        self.assertIn("No product sent a digest for this day.", self.html(D))

    def test_a_second_writer_is_review_locked(self) -> None:
        with self.LR._Lock():
            with self.assertRaises(self.LR.Refused) as caught:
                self.LR.run(at(D + timedelta(days=1), 7), wait=0.3)
        self.assertEqual(caught.exception.code, "review-locked")
        self.assertFalse(self.LR.report_path(D).exists(), "nothing written")

    def test_an_unwritable_review_folder_answers_review_unwritable(self) -> None:
        import atomic
        with patch.object(atomic, "write_text", side_effect=OSError(28, "no space left")):
            out = self.review(at(D + timedelta(days=1), 7))
        self.assertEqual(out["outcome"], "review-unwritable")
        runs = json.loads(self.LR.state_path().read_text())["runs"]
        self.assertEqual(runs[-1]["outcome"], "review-unwritable")


class Privacy(Workspace):
    def test_report_files_are_owner_only_and_self_contained(self) -> None:
        self.lesson("alpha-web")
        self.digest(ALPHA, D, {"ipc.call": [2, 12]})
        self.review(at(D + timedelta(days=1), 7))
        if os.name != "nt":
            for path in (self.LR.report_path(D), self.LR.report_path(D, ".html"), self.LR.state_path()):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
            for folder in (self.LR.root(), self.LR.report_path(D).parent):
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700, folder.name)
        page = self.html(D)
        for needle in ("<script", "http", "src=", "<link", "@import"):
            self.assertNotIn(needle, page.lower())
        self.assertIn('<html lang="en">', page)

    def test_the_report_carries_no_clone_path_and_rescrubs_lesson_text(self) -> None:
        import memory_redact
        self.lesson("alpha-web", title="Rotate the zebra-quartz-9911 value before shipping")
        with patch.object(self.LR, "_redactor",
                          lambda: memory_redact.Redactor(known_loader=lambda: {"zebra-quartz-9911": "FIXTURE_KEY"})):
            self.review(at(D + timedelta(days=1), 7))
        for text in (self.LR.report_path(D).read_text(encoding="utf-8"), self.html(D)):
            self.assertNotIn("zebra-quartz-9911", text)
            self.assertIn("[redacted:FIXTURE_KEY]", text)
            for name in CLONES:
                self.assertNotIn(str(CLONES[name][0]), text)
            self.assertNotIn(str(self.home), text)

    def test_nothing_is_sent_outward(self) -> None:
        self.lesson("alpha-web")
        self.lesson("beta-api")
        self.digest(ALPHA, D, {"ipc.call": [2, 12]})

        def refuse(*_a, **_k):
            raise AssertionError("the review opened a network connection")
        with patch.object(socket, "socket", refuse), patch.object(socket, "create_connection", refuse), \
                patch.object(urllib.request, "urlopen", refuse):
            out = self.review(at(D + timedelta(days=1), 7))
        self.assertEqual(out["outcome"], "written")
        self.assertEqual(self.report(D)["state"], "complete")


class TheCommand(Workspace):
    def cli(self, *argv: str, now: datetime) -> tuple[int, str]:
        sys.path.insert(0, str(ROOT / "tools"))
        import learning_review_cli
        out = io.StringIO()
        with patch.object(self.LR, "utc_now", lambda: now), redirect_stdout(out):
            self.clock.when = now
            code = learning_review_cli.main(list(argv))
        return code, out.getvalue()

    def test_run_show_and_list(self) -> None:
        self.lesson("alpha-web")
        now = at(D + timedelta(days=1), 7)
        code, out = self.cli("run", now=now)
        self.assertEqual(code, 0)
        self.assertIn("2026-11-05 complete r1 (1 new, 0 revised, 0 promoted, 0 repeated classes, 0 rises, 0 late)", out)
        code, out = self.cli("show", now=now)
        self.assertEqual(code, 0)
        self.assertIn("learning review of 2026-11-05", out)
        self.assertIn(f"html: {self.LR.report_path(D, '.html')}", out)
        code, out = self.cli("show", "--json", now=now)
        self.assertEqual(json.loads(out)["day"], "2026-11-05")
        code, out = self.cli("list", "--json", now=now)
        self.assertEqual([r["day"] for r in json.loads(out)][0], "2026-11-05")
        self.assertEqual(self.cli("run", "--day", "2026-11-05", now=now)[0], 0)

    def test_refusals_carry_their_codes(self) -> None:
        now = at(D + timedelta(days=1), 7)
        code, out = self.cli("show", "--json", now=now)
        self.assertEqual((code, json.loads(out)["code"]), (2, "no-report"))
        code, out = self.cli("run", "--day", "2026-11-06", "--json", now=now)
        self.assertEqual((code, json.loads(out)["code"]), (2, "day-not-closed"))
        code, out = self.cli("run", "--again", "--json", now=now)
        self.assertEqual((code, json.loads(out)["code"]), (2, "day-invalid"))

    def test_the_dispatcher_hands_the_verb_to_its_parser(self) -> None:
        import observatory as cli
        self.assertIn("learning-review", cli.OWN_PARSER_COMMANDS)
        for argv in (["learning-review", "show"], ["learning-review", "list", "--days", "7"],
                     ["learning-review", "run", "--day", "2026-11-05", "--again"]):
            self.assertEqual(cli.refusal(argv), "", argv)
        self.assertNotEqual(cli.refusal(["learning-review", "run", "--day", "5 Nov"]), "")
        self.assertEqual(cli.command_name(["observatory.py", "learning-review", "run"]), "learning-review")


class TheSetting(Workspace):
    def test_the_settle_hour_is_a_setting(self) -> None:
        settings = self.home / "config" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"schema_version": 1, "sources": {}, "integrations": {}, "features": {},
                                        "learning_review": {"settle_hour_utc": 9}}))
        self.assertEqual(self.LR.settle_hour(), 9)
        self.assertEqual(self.LR.plan(at(D + timedelta(days=1), 8))["newest"], D - timedelta(days=1))
        self.assertEqual(self.LR.plan(at(D + timedelta(days=1), 9))["newest"], D)
        import configuration
        settings.write_text(json.dumps({"schema_version": 1, "learning_review": {"settle_hour_utc": 24}}))
        with self.assertRaises(configuration.ConfigurationError):
            configuration.load()
        self.assertEqual(self.LR.settle_hour(), self.LR.SETTLE_HOURS, "an unreadable setting falls back")


if __name__ == "__main__":
    unittest.main()
