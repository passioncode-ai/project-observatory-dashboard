#!/usr/bin/env python3
"""Forget with a receipt: withdrawn, erased everywhere this machine reaches, and honest
about what it cannot reach (PB-137 N-013).

A temporary workspace holds a record whose text has been copied the ways the engine copies
text: into a closed workflow's handoff pack (as a related record), into the cached answer of
an acceptance (idempotency), into the lexical index, into the export `registry/ledger.jsonl`,
and into a plain backup beside the store. Then `store.forget.forget` runs, and the suite
checks every claim its receipt makes.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "tools"))

_MADE: list[pathlib.Path] = []
ALPHA = "project:alpha-web"
OWNER = "agent:forget-test"
EXECUTOR = {"provider": "anthropic", "model": "claude-opus-5-5", "accountRef": "acct-a"}
#: The text that must not survive. Distinctive, so a substring search cannot hit anything else;
#: "xylophonium" is a word no other record in these fixtures uses.
SECRET = "the vendor contract renews at forty-two thousand xylophonium per quarter"
UNIQUE = b"xylophonium"


def tearDownModule() -> None:
    for path in _MADE:
        shutil.rmtree(path, ignore_errors=True)


class Store:
    def __init__(self) -> None:
        self.home = pathlib.Path(tempfile.mkdtemp(prefix="observatory-forget-")).resolve()
        _MADE.append(self.home)
        for k, v in {"OBSERVATORY_HOME": self.home, "OBSERVATORY_DB": self.home / "store/observatory.db",
                     "OBSERVATORY_STATE": self.home / "store",
                     "OBSERVATORY_BACKUPS": self.home / "backups"}.items():
            os.environ[k] = str(v)
        (self.home / "store").mkdir(parents=True, exist_ok=True)
        for name in [k for k in list(sys.modules)
                     if k in ("paths", "configuration", "survey", "store", "embedding_policy",
                              "textkeys", "providers", "memory_redact", "export_ledger",
                              "backup_vault") or k.startswith("store.")]:
            sys.modules.pop(name, None)
        import paths
        paths.CONFIG.mkdir(parents=True, exist_ok=True)
        paths.REGISTRY.mkdir(parents=True, exist_ok=True)
        (paths.CONFIG / "models.json").write_text(
            (ROOT / "defaults/models.json").read_text(encoding="utf-8"), encoding="utf-8")
        import memory_redact
        import survey
        from store import db as sdb
        from store import forget as F
        from store import ledger as L
        from store import workflow as W
        self.paths, self.sdb, self.F, self.L, self.W, self.survey = paths, sdb, F, L, W, survey
        self.redactor = memory_redact.Redactor(known_loader=lambda: {})
        self.conn = sdb.connect()

    def export(self) -> pathlib.Path:
        import export_ledger
        out = pathlib.Path(self.paths.REGISTRY) / "ledger.jsonl"
        out.write_text(export_ledger.render(export_ledger.rows(self.conn)), encoding="utf-8")
        return out

    def copies(self) -> dict:
        """The record, a closed workflow whose pack quotes it, and an accepted handoff whose
        cached answer quotes it."""
        note = self.L.append(self.conn, owner=OWNER, statement=SECRET, why="the renewal clause",
                             project_id=ALPHA, confidence=0.5)
        self.conn.commit()
        wf = self.W.checkpoint_write(self.conn, owner=OWNER, idempotency_key="key-fg-0001",
                                     step_id="S1", status="in_progress",
                                     body={"goal": "review the vendor contract renewal quarter"},
                                     project_id=ALPHA, executor=EXECUTOR, redactor=self.redactor)
        h = self.W.handoff_create(self.conn, owner=OWNER, idempotency_key="key-fg-0002",
                                  workflow_id=wf["workflowId"], to={"provider": "anthropic"},
                                  reason="limit", lease_token=wf["leaseId"],
                                  git_reader=lambda p: {"path": p}, redactor=self.redactor)
        took = self.W.handoff_accept(self.conn, owner="agent:next", idempotency_key="key-fg-0003",
                                     handoff_id=h["handoffId"])
        return {"note": note, "wf": wf, "handoff": h, "took": took}

    def everything(self) -> bytes:
        """Every byte of the store's files, to search for the text itself."""
        db = pathlib.Path(self.paths.DB)
        out = b""
        for p in [db, db.with_name(db.name + "-wal")]:
            if p.exists():
                out += p.read_bytes()
        return out

    def close(self) -> None:
        self.conn.close()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Store()

    def tearDown(self) -> None:
        self.s.close()


class EverywhereThisMachineReaches(Base):
    def test_the_text_is_gone_from_every_copy_and_the_receipt_says_so(self) -> None:
        c = self.s.copies()
        pack = json.loads(self.s.conn.execute(
            "SELECT body_json FROM ledger WHERE memory_id = ?", (c["handoff"]["handoffId"],)
        ).fetchone()[0])
        self.assertIn(SECRET, json.dumps(pack), "the fixture copies the text into the pack")
        self.assertTrue(self.s.conn.execute("SELECT count(*) FROM idempotency WHERE instr(response_json, ?) > 0",
                                            (SECRET,)).fetchone()[0], "and into a cached answer")
        export = self.s.export()
        receipt = self.s.F.forget(self.s.conn, c["note"]["memoryId"], reason="contract withdrawn")
        status = {b["backend"]: b["status"] for b in receipt["backends"]}
        self.assertEqual(status["ledger"], "erased")
        self.assertEqual(status["handoff-packs"], "erased")
        self.assertEqual(status["idempotency"], "erased")
        self.assertEqual(status["file"], "erased")
        self.assertEqual(status["export"], "erased")
        self.assertEqual(status["git-history"], "retained")
        self.assertTrue(receipt["erasedInStore"], receipt)
        self.assertFalse(receipt["complete"], "git history keeps the export: never complete")
        self.assertNotIn(SECRET.encode(), self.s.everything(), "no page of the file holds it")
        self.assertNotIn(UNIQUE, self.s.everything(),
                         "nor any of its words: the full-text segments were merged away")
        self.assertNotIn(SECRET, export.read_text(encoding="utf-8"))
        # The audit trail stays: the rows exist, say who and when, and say nothing else.
        row = self.s.conn.execute("SELECT * FROM ledger WHERE memory_id = ?",
                                  (c["note"]["memoryId"],)).fetchone()
        self.assertEqual((row["statement"], row["why"], row["owner"]), ("[forgotten]", None, OWNER))
        # Reads see nothing.
        out = self.s.survey.search("vendor contract renews quarter", project_id=ALPHA)
        self.assertNotIn(c["note"]["memoryId"], [r["memoryId"] for r in out["results"]])

    def test_a_backup_taken_before_is_retained_and_never_called_complete(self) -> None:
        c = self.s.copies()
        db = pathlib.Path(self.s.paths.DB)
        shutil.copy2(db, db.with_name("observatory.db.backup-20261005T000000Z"))
        receipt = self.s.F.forget(self.s.conn, c["note"]["memoryId"], reason="contract withdrawn")
        backup = [b for b in receipt["backends"] if b["backend"] == "backups"][0]
        self.assertEqual(backup["status"], "retained")
        self.assertIn("1 plain backup", backup["detail"])
        self.assertFalse(receipt["complete"])

    def test_forgetting_again_rechecks_and_changes_nothing_new(self) -> None:
        c = self.s.copies()
        first = self.s.F.forget(self.s.conn, c["note"]["memoryId"], reason="contract withdrawn")
        again = self.s.F.forget(self.s.conn, c["note"]["memoryId"], reason="contract withdrawn")
        self.assertIsNone(again["erasedInStore"], "a rerun has no text left to search for")
        self.assertIn("0 revision(s) rewritten",
                      [b for b in again["backends"] if b["backend"] == "ledger"][0]["detail"])
        names = lambda r: [b["backend"] for b in r["backends"]]                  # noqa: E731
        self.assertEqual(names(again), names(first) + ["residue"],
                         "every backend checked again, plus the one it cannot check")


class ReviewFindings(Base):
    """Defects found by the pre-release review of 2026-10-05 (H1-H3)."""

    def closed_workflow(self, goal: str) -> dict:
        wf = self.s.W.checkpoint_write(self.s.conn, owner=OWNER, idempotency_key="key-fg-0201",
                                       step_id="S1", status="in_progress", body={"goal": goal},
                                       project_id=ALPHA, executor=EXECUTOR, redactor=self.s.redactor)
        h = self.s.W.handoff_create(self.s.conn, owner=OWNER, idempotency_key="key-fg-0202",
                                    workflow_id=wf["workflowId"], to={"provider": "anthropic"},
                                    reason="limit", lease_token=wf["leaseId"],
                                    git_reader=lambda p: {"path": p}, redactor=self.s.redactor)
        self.s.W.checkpoint_write(self.s.conn, owner=OWNER, idempotency_key="key-fg-0203",
                                  step_id="S2", status="done", body={"goal": goal},
                                  workflow_id=wf["workflowId"], lease_token=wf["leaseId"],
                                  close=True, redactor=self.s.redactor)
        return {"wf": wf, "handoff": h}

    def test_a_checkpoints_goal_leaves_the_handoff_statement_and_search(self) -> None:
        c = self.closed_workflow("price the quokkaprice tier for partners")
        statement = self.s.conn.execute("SELECT statement FROM ledger WHERE memory_id = ?",
                                        (c["handoff"]["handoffId"],)).fetchone()[0]
        self.assertIn("quokkaprice", statement, "the fixture's pack quotes the goal")
        receipt = self.s.F.forget(self.s.conn, f"ckpt:{c['wf']['workflowId']}", reason="test")
        self.assertTrue(receipt["erasedInStore"], receipt)
        out = self.s.survey.search("quokkaprice tier", project_id=ALPHA)
        self.assertEqual(out["results"], [], "the handoff no longer answers for the goal")
        self.assertNotIn(b"quokkaprice", self.s.everything())

    def test_provenance_and_conflicts_are_erased_too(self) -> None:
        note = self.s.L.append(self.s.conn, owner=OWNER, statement="a plain claim",
                               project_id=ALPHA, confidence=0.5,
                               provenance=[{"quote": "the zanzibarite ledger said so"}])
        self.s.conn.commit()
        receipt = self.s.F.forget(self.s.conn, note["memoryId"], reason="test")
        row = self.s.conn.execute("SELECT provenance_json FROM ledger WHERE memory_id = ?",
                                  (note["memoryId"],)).fetchone()[0]
        self.assertEqual(row, "[]")
        self.assertNotIn(b"zanzibarite", self.s.everything())
        self.assertTrue(receipt["erasedInStore"])

    def test_a_rerun_says_it_cannot_search_for_what_is_gone(self) -> None:
        note = self.s.L.append(self.s.conn, owner=OWNER, statement=SECRET, project_id=ALPHA,
                               confidence=0.5)
        self.s.conn.commit()
        self.s.F.forget(self.s.conn, note["memoryId"], reason="test")
        again = self.s.F.forget(self.s.conn, note["memoryId"], reason="test")
        self.assertIsNone(again["erasedInStore"], "unknown, not a fresh claim of erasure")
        self.assertFalse(again["complete"])
        self.assertIn("residue", [b["backend"] for b in again["backends"]])

    def test_an_interrupted_erasure_leaves_an_honest_partial_receipt(self) -> None:
        note = self.s.L.append(self.s.conn, owner=OWNER, statement=SECRET, project_id=ALPHA,
                               confidence=0.5)
        self.s.conn.commit()
        real = self.s.F._purge

        def broken(conn, backends):
            raise RuntimeError("database is locked")
        self.s.F._purge = broken
        try:
            with self.assertRaises(self.s.F.ForgetError) as ctx:
                self.s.F.forget(self.s.conn, note["memoryId"], reason="test")
        finally:
            self.s.F._purge = real
        self.assertIn("run forget again", str(ctx.exception))
        journal = (pathlib.Path(self.s.paths.STATE) / "logs" / "forget.jsonl").read_text()
        last = json.loads(journal.splitlines()[-1])
        self.assertEqual((last["interrupted"], last["erasedInStore"], last["complete"]),
                         ("projections", False, False))


class WhatIsRefusedOrKept(Base):
    def test_an_open_workflows_checkpoint_is_refused_and_nothing_changes(self) -> None:
        wf = self.s.W.checkpoint_write(self.s.conn, owner=OWNER, idempotency_key="key-fg-0101",
                                       step_id="S1", status="in_progress", body={"goal": SECRET},
                                       project_id=ALPHA, executor=EXECUTOR, redactor=self.s.redactor)
        mid = f"ckpt:{wf['workflowId']}"
        with self.assertRaises(self.s.F.ForgetError):
            self.s.F.forget(self.s.conn, mid, reason="test")
        body = self.s.conn.execute("SELECT body_json FROM ledger WHERE memory_id = ?", (mid,)).fetchone()[0]
        self.assertIn(SECRET, body)
        self.assertEqual(self.s.conn.execute("SELECT count(*) FROM tombstones").fetchone()[0], 0)

    def test_a_reason_is_required_and_a_missing_record_is_refused(self) -> None:
        note = self.s.L.append(self.s.conn, owner=OWNER, statement=SECRET, project_id=ALPHA,
                               confidence=0.5)
        self.s.conn.commit()
        with self.assertRaises(self.s.F.ForgetError):
            self.s.F.forget(self.s.conn, note["memoryId"], reason=" ")
        with self.assertRaises(self.s.F.ForgetError):
            self.s.F.forget(self.s.conn, "mem:nothing", reason="x")

    def test_a_pending_outbox_row_cannot_bring_it_back(self) -> None:
        note = self.s.L.append(self.s.conn, owner=OWNER, statement=SECRET, project_id=ALPHA,
                               confidence=0.5)
        self.s.conn.commit()
        self.s.F.forget(self.s.conn, note["memoryId"], reason="test")
        pending = self.s.conn.execute("SELECT count(*) FROM outbox WHERE memory_id = ? AND consumed_at IS NULL",
                                      (note["memoryId"],)).fetchone()[0]
        self.assertGreaterEqual(pending, 1, "the queue still points at it")
        from store import indexer
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            indexer.cmd_index(self.s.conn, 100)
        self.assertEqual(self.s.conn.execute("SELECT count(*) FROM search_notes WHERE memory_id = ?",
                                             (note["memoryId"],)).fetchone()[0], 0)

    def test_the_journal_names_the_erasure_and_holds_no_text(self) -> None:
        note = self.s.L.append(self.s.conn, owner=OWNER, statement=SECRET, project_id=ALPHA,
                               confidence=0.5)
        self.s.conn.commit()
        self.s.F.forget(self.s.conn, note["memoryId"], reason="contract withdrawn")
        journal = pathlib.Path(self.s.paths.STATE) / "logs" / "forget.jsonl"
        self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
        text = journal.read_text(encoding="utf-8")
        self.assertIn(note["memoryId"], text)
        self.assertNotIn(SECRET, text)

    def test_plan_reads_only(self) -> None:
        c = self.s.copies()
        before = self.s.everything()
        p = self.s.F.plan(self.s.conn, c["note"]["memoryId"])
        self.assertEqual(p["handoffPacks"], [c["handoff"]["handoffId"]])
        self.assertGreaterEqual(p["idempotencyAnswers"], 1)
        self.assertIn(SECRET.encode(), self.s.everything())
        self.assertEqual(self.s.conn.execute("SELECT count(*) FROM tombstones").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
