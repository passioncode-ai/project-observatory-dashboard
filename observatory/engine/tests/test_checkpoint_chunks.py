#!/usr/bin/env python3
"""Checkpoint prose is found with its field, chunk and revision; replays resurrect nothing
(PB-137 N-011, lexical half).

A checkpoint's decisions, constraints and results are what a later session asks about, and
they were already indexed with the revision, in the same transaction (migration 0009). What
this suite adds:

* a search hit on a checkpoint says WHERE in the body it matched (`chunks`: the field path
  and chunk index, never the text twice), so a successor reads the decision, not the log;
* identifiers do not dominate prose: a workflow id or handoff id is not a search key;
* an outbox row replayed for an OLDER revision never puts the superseded text back into the
  lexical index, and a tombstoned record never comes back either;
* a lexical write that fails takes the revision with it (one transaction), and a committed
  checkpoint is found from a fresh connection at once (a restart loses nothing);
* the lag a search reports is the VECTOR index's: the lexical index is written with each
  revision, and the answer says which arm is behind.

The semantic half of N-011 (chunks within a model's token limit, the vector worker) waits
for an admitted local model (N-006/N-010, deferred under OBS-35, finding F-016).
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402

_MADE: list[pathlib.Path] = []
ALPHA = "project:alpha-web"
OWNER = "agent:chunk-test"
EXECUTOR = {"provider": "anthropic", "model": "claude-opus-5-5", "accountRef": "acct-a"}


def tearDownModule() -> None:
    for path in _MADE:
        shutil.rmtree(path, ignore_errors=True)


def long_body() -> dict:
    decisions = [{"id": f"D{i}", "choice": f"choice number {i} about caching",
                  "why": f"ordinary reasoning paragraph {i} about the rollout cadence"}
                 for i in range(1, 6)]
    decisions[3]["why"] = ("the partner's accountant needs invoices grouped by fiscal quarter, "
                           "so the exporter writes one file per quarter")
    return {"goal": "ship the invoice exporter",
            "decisions": decisions,
            "constraints": ["read-only: never push to the partner bucket",
                            "never email the partner before the operator approves"],
            "open": [{"step_id": "S3", "next_action": "write the quarter grouping"}],
            "notes": " ".join(f"filler sentence {i} about unrelated tooling." for i in range(80))}


class Store:
    def __init__(self) -> None:
        self.home = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-chunks-")).resolve()
        _MADE.append(self.home)
        os.environ["OBSERVATORY_HOME"] = str(self.home)
        os.environ["OBSERVATORY_DB"] = str(self.home / "store" / "observatory.db")  # paths-check: allow — an isolated workspace's store, handed over as OBSERVATORY_DB; this IS the redirection
        os.environ["OBSERVATORY_STATE"] = str(self.home / "store")
        (self.home / "store").mkdir(parents=True, exist_ok=True)
        for name in [k for k in list(sys.modules)
                     if k in ("paths", "configuration", "survey", "store", "embedding_policy",
                              "textkeys", "providers", "memory_redact") or k.startswith("store.")]:
            sys.modules.pop(name, None)
        import paths
        paths.CONFIG.mkdir(parents=True, exist_ok=True)
        (paths.CONFIG / "models.json").write_text(
            (ROOT / "defaults/models.json").read_text(encoding="utf-8"), encoding="utf-8")
        import memory_redact
        import survey
        from store import db as sdb
        from store import indexer
        from store import ledger as L
        from store import workflow as W
        self.sdb, self.L, self.W, self.ix, self.survey = sdb, L, W, indexer, survey
        self.redactor = memory_redact.Redactor(known_loader=lambda: {})
        self.conn = sdb.connect()

    def checkpoint(self, body: dict, key: str = "key-chunk-0001", **kw) -> dict:
        return self.W.checkpoint_write(self.conn, owner=OWNER, idempotency_key=key, step_id="S2",
                                       status="in_progress", body=body, project_id=ALPHA,
                                       executor=EXECUTOR, redactor=self.redactor, **kw)

    def lexical_rows(self, memory_id: str) -> list[tuple]:
        return [tuple(r) for r in self.conn.execute(
            "SELECT revision, statement FROM search_notes WHERE memory_id = ?", (memory_id,))]

    def index(self) -> None:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.ix.cmd_index(self.conn, 1000)

    def close(self) -> None:
        self.conn.close()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Store()

    def tearDown(self) -> None:
        self.s.close()


class Provenance(Base):
    def test_a_long_checkpoint_is_found_with_its_field_and_chunk(self) -> None:
        wf = self.s.checkpoint(long_body())
        out = self.s.survey.search("invoices grouped by fiscal quarter", project_id=ALPHA)
        hits = [r for r in out["results"] if r["memoryId"] == f"ckpt:{wf['workflowId']}"]
        self.assertEqual(len(hits), 1, out)
        hit = hits[0]
        self.assertEqual(hit["revision"], 1)
        fields = [c["field"] for c in hit["chunks"]]
        self.assertEqual(fields[0], "decisions[3].why", hit["chunks"])
        self.assertNotIn("text", hit["chunks"][0], "provenance points, it does not repeat")
        self.assertGreater(hit["chunks"][0]["covered"], 0.5)

    def test_a_constraint_is_found_and_named(self) -> None:
        self.s.checkpoint(long_body())
        out = self.s.survey.search("email the partner before approval", project_id=ALPHA)
        self.assertEqual(out["results"][0]["chunks"][0]["field"], "constraints[1]", out)

    def test_long_notes_are_cut_into_numbered_chunks(self) -> None:
        import textkeys
        chunks = textkeys.body_chunks(long_body())
        notes = [f for f, _ in chunks if f.startswith("notes#")]
        self.assertGreater(len(notes), 1, "a long note is more than one chunk")
        self.assertTrue(all(len(t.split()) <= textkeys.CHUNK_WORDS for _, t in chunks))

    def test_identifiers_are_not_search_keys(self) -> None:
        wf = self.s.checkpoint(long_body())
        stems = self.s.conn.execute("SELECT stems FROM search_notes WHERE memory_id = ?",
                                    (f"ckpt:{wf['workflowId']}",)).fetchone()[0].split()
        hexpart = wf["workflowId"].split("_", 1)[1]
        self.assertNotIn(hexpart, stems, "a workflow id is found by the workflow tools")
        self.assertIn("invoic", " ".join(stems))


class NothingComesBack(Base):
    def test_replaying_an_older_revision_does_not_resurrect_it(self) -> None:
        first = self.s.L.append(self.s.conn, owner=OWNER, statement="the old exporter wording",
                                project_id=ALPHA, confidence=0.5)
        self.s.L.append(self.s.conn, owner=OWNER, statement="the new exporter wording",
                        memory_id=first["memoryId"], expected_revision=1, project_id=ALPHA,
                        confidence=0.5)
        self.s.conn.commit()
        # The newer revision's row was consumed; the older one is replayed late.
        self.s.conn.execute("UPDATE outbox SET consumed_at = '2026-01-01T00:00:00Z'"
                            " WHERE memory_id = ? AND revision = 2", (first["memoryId"],))
        self.s.conn.commit()
        self.s.index()
        self.assertEqual(self.s.lexical_rows(first["memoryId"]),
                         [(2, "the new exporter wording")])
        pending = self.s.conn.execute("SELECT count(*) FROM outbox WHERE consumed_at IS NULL"
                                      ).fetchone()[0]
        self.assertEqual(pending, 0, "the superseded row is consumed, not held")

    def test_replaying_a_forgotten_record_does_not_resurrect_it(self) -> None:
        gone = self.s.L.append(self.s.conn, owner=OWNER, statement="the withdrawn exporter",
                               project_id=ALPHA, confidence=0.5)
        self.s.conn.commit()
        self.s.L.tombstone(self.s.conn, gone["memoryId"], reason="test", approved_by="operator")
        self.s.conn.execute("DELETE FROM search_notes WHERE memory_id = ?", (gone["memoryId"],))
        self.s.conn.commit()
        self.s.index()
        self.assertEqual(self.s.lexical_rows(gone["memoryId"]), [])


class WritesAndRestarts(Base):
    def test_a_failed_lexical_write_takes_the_revision_with_it(self) -> None:
        import textkeys
        before = self.s.conn.execute("SELECT count(*) FROM ledger").fetchone()[0]
        real = textkeys.stems_of

        def full(*_a, **_k):
            raise sqlite3.OperationalError("database or disk is full")
        textkeys.stems_of = full
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.s.checkpoint(long_body())
        finally:
            textkeys.stems_of = real
        self.assertEqual(self.s.conn.execute("SELECT count(*) FROM ledger").fetchone()[0], before)
        self.assertEqual(self.s.conn.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)

    def test_a_committed_checkpoint_is_found_after_a_restart(self) -> None:
        wf = self.s.checkpoint(long_body())
        self.s.conn.close()
        self.s.conn = self.s.sdb.connect()
        out = self.s.survey.search("fiscal quarter invoices", project_id=ALPHA)
        self.assertEqual(out["results"][0]["memoryId"], f"ckpt:{wf['workflowId']}")


class TheLagIsTheVectorIndexs(Base):
    def test_the_lag_names_the_arm_that_is_behind(self) -> None:
        self.s.checkpoint(long_body())
        out = self.s.survey.search("fiscal quarter invoices", project_id=ALPHA)
        lag = [d for d in out["degraded"] if d["source"] == "projection"]
        self.assertEqual(len(lag), 1, out["degraded"])
        self.assertEqual(lag[0].get("arm"), "vector")
        self.assertIn("lexical index is written with each revision", lag[0]["reason"])
        self.assertTrue(out["results"], "the lexical arm already has the checkpoint")


if __name__ == "__main__":
    unittest.main()
