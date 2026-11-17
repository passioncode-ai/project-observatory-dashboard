#!/usr/bin/env python3
"""The daily doctor (OBS-558, CP-15): a fixed catalogue of checks once a day; a safe fix runs and journals its undo,
an unsafe one only reports.

Every source is a fixture under a temporary workspace; the model summary is a fake that records what it was asked,
so nothing reaches a network. A made-up value planted in a vault slot is checked never to reach a stored file.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import sqlite3
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "dashboard"))
import tmp as tmpdir                                                # noqa: E402
import daily_doctor as dd                                           # noqa: E402
import doctor_findings as dfn                                       # noqa: E402
import maintenance                                                  # noqa: E402

AT = datetime.datetime(2026, 11, 16, 12, 0, tzinfo=datetime.timezone.utc)
FAKE_VALUE = "fakevalue-doctor-9999-not-a-real-key"
OLD = (AT - datetime.timedelta(hours=5)).timestamp()


def iso(moment: datetime.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Workspace:
    def __init__(self):
        self.base = pathlib.Path(tmpdir.mkdtemp())
        self.store = self.base / "store"
        for sub in ("raw", "state", "logs"):
            (self.store / sub).mkdir(parents=True, exist_ok=True)
        self.vault = self.base / "vault" / "projects"
        self.vault.mkdir(parents=True)

    def write(self, rel: str, text: str, *, mtime: float | None = OLD) -> pathlib.Path:
        path = self.store / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def maintenance(self, doc: dict) -> None:
        (self.store / "maintenance.json").write_text(json.dumps(doc), encoding="utf-8")

    def scans(self, started_at: str | None) -> None:
        con = sqlite3.connect(self.store / "observatory.db")
        con.execute("CREATE TABLE scans (started_at TEXT)")
        if started_at:
            con.execute("INSERT INTO scans VALUES (?)", (started_at,))
        con.commit()
        con.close()

    def healthy(self) -> None:
        """Every source present and clean."""
        self.maintenance({"pass": {"at": iso(AT)}, "snapshot": {"at": iso(AT - datetime.timedelta(hours=3)),
                                                                "result": "taken"}})
        self.scans(iso(AT - datetime.timedelta(minutes=20)))
        self.write("raw/lifecycle.json", json.dumps({"observations": []}))
        self.write("raw/harness.json", json.dumps({"inventory": {"findings": []}}))

    def run(self, **kw):
        kw.setdefault("vault_root", self.vault)
        kw.setdefault("alive", lambda pid: False)
        kw.setdefault("summarize", Recorder())
        kw.setdefault("clock", lambda: AT.timestamp())
        return dd.run(self.base, at=kw.pop("at", AT), **kw)

    def journal(self) -> list[dict]:
        path = self.store / "logs" / "doctor.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []

    def all_text(self) -> str:
        return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in self.store.rglob("*")
                         if p.is_file() and p.suffix in (".json", ".jsonl"))


class Recorder:
    def __init__(self, text="Two orphaned temporary files were moved aside."):
        self.asked, self.text = [], text

    def __call__(self, facts):
        self.asked.append(facts)
        return {"text": self.text, "model": "fake/model"}


def by_id(doc: dict) -> dict:
    return {c["id"]: c for c in doc["checks"]}


class Catalogue(unittest.TestCase):
    def test_the_catalogue_is_fixed_and_small(self):
        ids = [c.id for c in dd.CATALOGUE]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(5 <= len(ids) <= 8, ids)
        for check in dd.CATALOGUE:
            self.assertTrue(bool(check.fix) + bool(check.finding) + bool(check.covered_by) == 1, check.id)

    def test_a_clean_workspace_reads_ok_everywhere(self):
        w = Workspace()
        w.healthy()
        doc = w.run()
        self.assertEqual({c["id"]: c["state"] for c in doc["checks"]}, {c.id: "ok" for c in dd.CATALOGUE})
        self.assertEqual(doc["degraded"], [])
        self.assertEqual(doc["fixes"], [])


class SafeFix(unittest.TestCase):
    """CP-15's test: a safe fix runs and journals its undo."""

    def test_orphaned_temp_files_are_moved_aside_and_the_undo_puts_them_back(self):
        w = Workspace()
        w.healthy()
        dead = w.write("raw/worktree-registration.json.4242.tmp", "{}")
        mine = w.write("raw/worktree-registration.json.777.tmp", "{}")       # its writer still runs
        fresh = w.write(".maintenance-abcd", "{}", mtime=AT.timestamp() - 60)  # still being written
        stale = w.write(".maintenance-efgh", "{}")
        doc = w.run(alive=lambda pid: pid == 777)
        check = by_id(doc)["temp_orphans"]
        self.assertEqual(check["state"], "fixed")
        self.assertEqual(sorted(check["items"]), [".maintenance-efgh", "raw/worktree-registration.json.4242.tmp"])
        self.assertFalse(dead.exists())
        self.assertFalse(stale.exists())
        self.assertTrue(mine.exists() and fresh.exists())
        jid = check["fix"]["journal_id"]
        self.assertRegex(jid, dd.JOURNAL_ID.pattern)
        quarantined = w.store / "state" / "doctor-quarantine" / jid / "raw" / "worktree-registration.json.4242.tmp"
        self.assertTrue(quarantined.is_file())
        lines = w.journal()
        self.assertEqual([(line["event"], line["id"]) for line in lines], [("applied", jid)])
        out = dd.undo(w.base, journal_id=jid)
        self.assertEqual([u["id"] for u in out["undone"]], [jid])
        self.assertTrue(dead.is_file() and stale.is_file())
        self.assertEqual([line["event"] for line in w.journal()], ["applied", "undone"])
        again = dd.undo(w.base, journal_id=jid)
        self.assertEqual(again["error"]["code"], "already-undone")

    def test_an_unreadable_receipt_is_moved_aside_but_one_being_written_is_left(self):
        w = Workspace()
        w.healthy()
        torn = w.write("raw/remotes.json", '{"remotes": [')
        writing = w.write("raw/google.json", '{"half', mtime=AT.timestamp() - 30)
        good = w.write("raw/spend.json", "{}")
        doc = w.run()
        check = by_id(doc)["receipt_unreadable"]
        self.assertEqual((check["state"], check["items"]), ("fixed", ["raw/remotes.json"]))
        self.assertFalse(torn.exists())
        self.assertTrue(writing.exists() and good.exists())

    def test_undo_refuses_a_file_a_collector_wrote_again_and_keeps_the_copy(self):
        w = Workspace()
        w.healthy()
        torn = w.write("raw/remotes.json", '{"remotes": [')
        jid = by_id(w.run())["receipt_unreadable"]["fix"]["journal_id"]
        torn.write_text('{"remotes": []}', encoding="utf-8")                  # the next tick rewrote it
        out = dd.undo(w.base, journal_id=jid)
        self.assertEqual(out["undone"], [])
        self.assertEqual(out["refused"][0]["files"], [{"file": "raw/remotes.json", "reason": "replaced"}])
        self.assertEqual(json.loads(torn.read_text(encoding="utf-8")), {"remotes": []})
        self.assertTrue((w.store / "state" / "doctor-quarantine" / jid / "raw" / "remotes.json").is_file())

    def test_undo_last_reverses_the_newest_fixes_first(self):
        w = Workspace()
        w.healthy()
        w.write("raw/a.json", "{")
        first = by_id(w.run())["receipt_unreadable"]["fix"]["journal_id"]
        w.write("raw/b.json", "{")
        second = by_id(w.run(at=AT + datetime.timedelta(days=1),
                             clock=lambda: (AT + datetime.timedelta(days=1)).timestamp()))["receipt_unreadable"]["fix"]["journal_id"]
        out = dd.undo(w.base, last=1)
        self.assertEqual([u["id"] for u in out["undone"]], [second])
        self.assertEqual([f["journal_id"] for f in dd.agent_view(w.base, at=AT)["undoable"]], [first])

    def test_an_unknown_id_is_a_typed_refusal(self):
        w = Workspace()
        self.assertEqual(dd.undo(w.base, journal_id="doc_0000000000000000")["error"]["code"], "unknown-journal-id")
        self.assertEqual(dd.undo(w.base, journal_id="../x")["error"]["code"], "invalid-journal-id")

    def test_a_dry_run_moves_nothing_and_journals_nothing(self):
        w = Workspace()
        w.healthy()
        torn = w.write("raw/remotes.json", "{")
        doc = w.run(apply=False)
        self.assertEqual(by_id(doc)["receipt_unreadable"]["state"], "would-fix")
        self.assertTrue(torn.exists())
        self.assertEqual(w.journal(), [])

    def test_quarantine_older_than_its_keep_is_pruned_and_journalled(self):
        w = Workspace()
        w.healthy()
        w.write("raw/a.json", "{")
        jid = by_id(w.run())["receipt_unreadable"]["fix"]["journal_id"]
        later = AT + dd.QUARANTINE_KEEP + datetime.timedelta(days=1)
        w.run(at=later, clock=lambda: later.timestamp())
        self.assertFalse((w.store / "state" / "doctor-quarantine" / jid).exists())
        self.assertEqual([line["event"] for line in w.journal() if line["id"] == jid], ["applied", "expired"])
        self.assertEqual(dd.undo(w.base, journal_id=jid)["error"]["code"], "expired")


class UnsafeOnlyReports(unittest.TestCase):
    """CP-15's test: an unsafe one only reports."""

    def test_a_stale_backup_is_a_finding_and_nothing_is_changed(self):
        w = Workspace()
        w.healthy()
        before = {"pass": {"at": iso(AT)}, "snapshot": {"at": iso(AT - datetime.timedelta(hours=60)), "result": "taken"},
                  "snapshot_failure": {"at": iso(AT - datetime.timedelta(minutes=30)), "detail": "OSError: disk full"}}
        w.maintenance(before)
        doc = w.run()
        check = by_id(doc)["backup_stale"]
        self.assertEqual((check["state"], check["finding"], check["hours"]), ("finding", "doctor.backup_stale", 60))
        self.assertNotIn("fix", check)
        self.assertEqual(json.loads((w.store / "maintenance.json").read_text(encoding="utf-8")), before)
        self.assertEqual(w.journal(), [])
        rows = dfn.findings(doc)
        self.assertEqual([r["type"] for r in rows], ["doctor.backup_stale"])
        self.assertIn("disk full", rows[0]["detail"])

    def test_a_step_failing_pass_after_pass_is_a_finding_per_step(self):
        w = Workspace()
        w.healthy()
        state = json.loads((w.store / "maintenance.json").read_text(encoding="utf-8"))
        report = {"org": {"result": "error", "detail": "TimeoutExpired"}, "guard": {"result": "ran"}}
        for i in range(dd.STREAK_PASSES):
            maintenance.record_streaks(state, report, AT - datetime.timedelta(minutes=15 * (dd.STREAK_PASSES - i)))
        maintenance.record_streaks(state, {"shares": {"result": "error"}}, AT)
        w.maintenance(state)
        check = by_id(w.run())["step_failing"]
        self.assertEqual((check["state"], check["items"]), ("finding", ["org"]))
        rows = [r for r in dfn.findings(dd.read_doc(w.base)) if r["type"] == "doctor.step_failing"]
        self.assertEqual([r["subject"] for r in rows], ["doctor:step/org"])
        maintenance.record_streaks(state, {"org": {"result": "ran"}}, AT)
        self.assertNotIn("org", state["step_streaks"])

    def test_an_unreadable_vault_metadata_file_is_named_and_no_value_is_read(self):
        w = Workspace()
        w.healthy()
        slot = w.vault / "alpha-web" / "prod"
        slot.mkdir(parents=True)
        (slot / "API_KEY").write_text(FAKE_VALUE, encoding="utf-8")
        (slot / "API_KEY.meta.json").write_text("{not json", encoding="utf-8")
        (slot / "OTHER.meta.json").write_text("{}", encoding="utf-8")
        doc = w.run()
        check = by_id(doc)["vault_meta"]
        self.assertEqual((check["state"], check["items"]), ("finding", ["alpha-web/prod/API_KEY"]))
        dfn.findings(doc)
        self.assertNotIn(FAKE_VALUE, w.all_text())
        self.assertNotIn(FAKE_VALUE, json.dumps(doc))

    def test_checks_an_existing_rule_covers_point_at_it_instead_of_a_second_row(self):
        w = Workspace()
        w.healthy()
        w.write("raw/lifecycle.json", json.dumps({"observations": [
            {"kind": "stale-code", "slug": "alpha", "pid": 11}, {"kind": "orphan", "slug": "alpha"}]}))
        w.write("raw/harness.json", json.dumps({"inventory": {"findings": [{"code": "plugin-behind"}]}}))
        (w.store / "observatory.db").unlink()
        w.scans(iso(AT - datetime.timedelta(hours=9)))
        doc = w.run()
        checks = by_id(doc)
        for cid, covered in (("mcp_doors", "lifecycle.stale_servers"), ("plugin_behind", "harness.plugin_behind"),
                             ("scan_fresh", "scan.stale")):
            self.assertEqual((checks[cid]["state"], checks[cid]["covered_by"]), ("finding", covered), cid)
        self.assertEqual(checks["mcp_doors"]["count"], 1)
        self.assertEqual(dfn.findings(doc), [])


class Degraded(unittest.TestCase):
    def test_absent_sources_are_unknown_and_named_never_ok(self):
        w = Workspace()
        doc = w.run(vault_root=None, resolve_vault=lambda: (_ for _ in ()).throw(OSError("no store")))
        checks = by_id(doc)
        for cid in ("backup_stale", "step_failing", "scan_fresh", "mcp_doors", "plugin_behind", "vault_meta"):
            self.assertEqual(checks[cid]["state"], "unknown", cid)
        self.assertEqual({d["source"] for d in doc["degraded"]} >= {"maintenance", "store", "lifecycle", "harness",
                                                                    "vault"}, True, doc["degraded"])

    def test_the_view_before_any_run_says_so(self):
        w = Workspace()
        view = dd.agent_view(w.base, at=AT)
        self.assertEqual((view["checks"], view["undoable"]), ([], []))
        self.assertEqual([d["source"] for d in view["degraded"]], ["doctor"])

    def test_a_run_two_days_old_is_named_stale(self):
        w = Workspace()
        w.healthy()
        w.run()
        view = dd.agent_view(w.base, at=AT + datetime.timedelta(hours=49))
        self.assertIn("doctor", {d["source"] for d in view["degraded"]})


class Summary(unittest.TestCase):
    def test_the_model_writes_only_when_the_result_changed(self):
        w = Workspace()
        w.healthy()
        w.write("raw/a.json", "{")
        rec = Recorder()
        first = w.run(summarize=rec)
        self.assertEqual((first["summary"]["state"], first["summary"]["text"]), ("written", rec.text))
        facts = json.dumps(rec.asked[0])
        self.assertIn("receipt_unreadable", facts)
        self.assertNotIn("raw/a.json", facts)                                # names stay on this machine
        later = AT + datetime.timedelta(days=1)
        second = w.run(summarize=rec, at=later, clock=lambda: later.timestamp())
        self.assertEqual(second["summary"]["state"], "written")             # fixed -> ok is a change
        third_at = later + datetime.timedelta(hours=1)
        third = w.run(summarize=rec, at=third_at, clock=lambda: third_at.timestamp())
        self.assertEqual((third["summary"]["state"], len(rec.asked)), ("unchanged", 2))

    def test_no_key_or_budget_turns_the_summary_off_and_degraded_says_so(self):
        w = Workspace()
        w.healthy()

        def off(_facts):
            raise dd.SummaryOff("no model key is configured")
        doc = w.run(summarize=off)
        self.assertEqual(doc["summary"], {"state": "off", "text": None, "reason": "no model key is configured"})
        self.assertIn({"source": "model", "reason": "no model key is configured"}, doc["degraded"])

    def test_the_default_summarizer_is_off_without_the_agent_feature(self):
        with self.assertRaises(dd.SummaryOff):
            dd.model_summary([{"id": "x"}], enabled=lambda: False)


class Maintenance(unittest.TestCase):
    def test_the_pass_runs_the_doctor_once_a_day(self):
        w = Workspace()
        calls = []

        def runner(argv, **kw):
            calls.append(argv)

            class P:
                returncode, stdout, stderr = 0, json.dumps({"result": "ran", "fixed": 1, "findings": 2}), ""
            return P()
        state: dict = {}
        out = maintenance.step_doctor(w.base, state, AT, runner=runner)
        self.assertEqual(out, {"result": "ran", "fixed": 1, "findings": 2})
        self.assertEqual(maintenance.step_doctor(w.base, state, AT + datetime.timedelta(hours=2), runner=runner),
                         {"result": "not-due", "last": iso(AT)})
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(str(a).endswith("daily_doctor.py") for a in calls[0]))

    def test_a_failed_doctor_is_recorded_and_tried_again_next_pass(self):
        w = Workspace()

        def runner(argv, **kw):
            class P:
                returncode, stdout, stderr = 1, "", "boom"
            return P()
        state: dict = {}
        self.assertEqual(maintenance.step_doctor(w.base, state, AT, runner=runner)["result"], "failed")
        self.assertNotIn("doctor", state)


if __name__ == "__main__":
    unittest.main()
