#!/usr/bin/env python3
"""Vector namespaces: one index per pinned model identity (PB-137 N-005).

Proves store/namespaces.py and migration 0010: the migration is additive and quarantines
the legacy index; identities are validated and never mix; table names come from integer
ids only; a backfill is resumable, honours its eligibility, and never activates; activation
refuses without a receipt. Synthetic stores in temporary workspaces, removed afterwards.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402

_MADE: list[pathlib.Path] = []
_CACHED = ("paths", "store.db", "store.ledger", "store.migrate", "store.namespaces", "store")


def tearDownModule() -> None:
    for path in _MADE:
        shutil.rmtree(path, ignore_errors=True)


def fresh():
    home = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-ns-")).resolve()
    _MADE.append(home)
    os.environ["OBSERVATORY_HOME"] = str(home)
    os.environ["OBSERVATORY_DB"] = str(home / "store" / "observatory.db")  # paths-check: allow — an isolated workspace's store, handed over as OBSERVATORY_DB; this IS the redirection
    os.environ["OBSERVATORY_STATE"] = str(home / "store")
    (home / "store").mkdir(parents=True, exist_ok=True)
    for m in _CACHED:
        sys.modules.pop(m, None)
    from store import db as sdb
    from store import ledger as L
    from store import namespaces as NS
    return sdb.connect(), L, NS


def identity(**over):
    base = {"provider": "local", "model": "multilingual-e5-small", "revision": "614241f",
            "weights_sha256": "a" * 64, "tokenizer_sha256": "b" * 64, "dims": 4,
            "metric": "cosine", "normalization": "l2", "query_prefix": "query: ",
            "passage_prefix": "passage: ", "chunker": "statement+why/1"}
    base.update(over)
    return base


def vec_loaded(conn) -> bool:
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:                                                                 # noqa: BLE001
        return False


class Migration(unittest.TestCase):
    def test_the_migration_registers_the_legacy_index_as_quarantined(self):
        conn, _L, NS = fresh()
        rows = NS.listing(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].state, rows[0].table), ("legacy", "vec_notes"))
        self.assertEqual(rows[0].identity, NS.LEGACY)
        self.assertEqual(rows[0].model_key, NS.model_key(NS.LEGACY),
                         "the migration's inline legacy identity equals the module's")
        conn.close()

    def test_the_migration_is_additive_and_idempotent(self):
        conn, _L, NS = fresh()
        from store import migrate
        before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        fn = dict(migrate.MIGRATIONS)["0010-vector-namespaces"]
        fn(conn)
        conn.commit()
        after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        self.assertEqual(before, after, "a second run creates and drops nothing")
        self.assertEqual(len(NS.listing(conn)), 1, "the legacy namespace is registered once")
        conn.close()


class Identity(unittest.TestCase):
    def setUp(self):
        self.conn, self.L, self.NS = fresh()

    def tearDown(self):
        self.conn.close()

    def test_an_incomplete_or_unpinned_identity_is_refused(self):
        for bad in ({}, identity(weights_sha256=None), identity(dims=0), identity(metric="dot"),
                    identity(extra="x"), identity(model="")):
            with self.subTest(bad):
                with self.assertRaises(self.NS.NamespaceError):
                    self.NS.register(self.conn, bad)

    def test_one_identity_one_namespace_and_any_difference_is_another(self):
        a = self.NS.register(self.conn, identity())
        again = self.NS.register(self.conn, identity())
        other_rev = self.NS.register(self.conn, identity(revision="d128750"))
        other_prefix = self.NS.register(self.conn, identity(query_prefix=""))
        self.assertEqual(a.namespace_id, again.namespace_id)
        self.assertEqual(len({a.namespace_id, other_rev.namespace_id, other_prefix.namespace_id}), 3)
        self.assertEqual(a.state, "inactive")

    def test_table_names_come_from_integer_ids_only(self):
        ns = self.NS.register(self.conn, identity(model="x; DROP TABLE ledger; --"))
        self.assertEqual(ns.table, f"vec_ns_{ns.namespace_id}")
        with self.assertRaises(self.NS.NamespaceError):
            self.NS.table_name("1; DROP TABLE ledger")


class Backfill(unittest.TestCase):
    def setUp(self):
        self.conn, self.L, self.NS = fresh()
        if not vec_loaded(self.conn):
            self.skipTest("sqlite-vec is not loadable in this interpreter")
        self.ns = self.NS.register(self.conn, identity())
        self.NS.ensure_table(self.conn, self.ns)
        for i in range(5):
            self.L.append(self.conn, owner="agent:fixture", statement=f"note {i}", state="proposed",
                          confidence=0.5, project_id="project:alpha")
        self.calls = 0

    def tearDown(self):
        self.conn.close()

    def embed(self, texts):
        self.calls += 1
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    def count(self):
        return self.conn.execute(f"SELECT count(*) FROM {self.ns.table}").fetchone()[0]

    def test_a_backfill_runs_to_ready_and_never_to_active(self):
        steps = []
        while True:
            r = self.NS.backfill_step(self.conn, self.ns, self.embed, allowed=lambda _r: True, batch=2)
            steps.append(r)
            if r["complete"]:
                break
        self.assertEqual(self.count(), 5)
        self.assertEqual(self.NS.get(self.conn, self.ns.namespace_id).state, "ready")
        self.assertNotIn("active", [s["state"] for s in steps])

    def test_an_interrupted_batch_leaves_the_previous_state_and_resumes(self):
        self.NS.backfill_step(self.conn, self.ns, self.embed, allowed=lambda _r: True, batch=2)
        before = self.NS.get(self.conn, self.ns.namespace_id)

        def broken(texts):
            raise RuntimeError("the model process died")
        with self.assertRaises(RuntimeError):
            self.NS.backfill_step(self.conn, self.ns, broken, allowed=lambda _r: True, batch=2)
        after = self.NS.get(self.conn, self.ns.namespace_id)
        self.assertEqual((after.backfill_cursor, after.backfill_done),
                         (before.backfill_cursor, before.backfill_done))
        self.assertEqual(self.count(), 2)
        while not self.NS.backfill_step(self.conn, self.ns, self.embed, allowed=lambda _r: True,
                                        batch=2)["complete"]:
            pass
        self.assertEqual(self.count(), 5, "resumed, and nothing written twice")

    def test_a_vector_of_another_width_never_enters(self):
        with self.assertRaises(self.NS.NamespaceError):
            self.NS.backfill_step(self.conn, self.ns, lambda t: [[0.1, 0.2] for _ in t],
                                  allowed=lambda _r: True)
        self.assertEqual(self.count(), 0)

    def test_eligibility_decides_each_record_and_refused_ones_are_never_embedded(self):
        seen = []

        def embed(texts):
            seen.extend(texts)
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]
        while not self.NS.backfill_step(self.conn, self.ns, embed,
                                        allowed=lambda r: r["statement"] != "note 3")["complete"]:
            pass
        self.assertNotIn("note 3", seen)
        self.assertEqual(self.count(), 4)

    def test_activation_needs_a_receipt_and_the_legacy_index_never_activates(self):
        while not self.NS.backfill_step(self.conn, self.ns, self.embed, allowed=lambda _r: True,
                                        batch=10)["complete"]:
            pass
        with self.assertRaises(self.NS.NamespaceError):
            self.NS.activate(self.conn, self.ns, receipt=None)
        with self.assertRaises(self.NS.NamespaceError):
            self.NS.activate(self.conn, self.ns, receipt={"model_admission": "x"})
        legacy = self.NS.listing(self.conn)[0]
        with self.assertRaisesRegex(self.NS.NamespaceError, "quarantined"):
            self.NS.activate(self.conn, legacy, receipt={"model_admission": "a", "scope_filter": "b",
                                                         "abstention_calibration": "c"})
        done = self.NS.activate(self.conn, self.ns, receipt={"model_admission": "a",
                                                             "scope_filter": "b",
                                                             "abstention_calibration": "c"})
        self.assertEqual(done.state, "active")

    def test_a_legacy_or_retired_namespace_gets_no_table_and_no_backfill(self):
        legacy = self.NS.listing(self.conn)[0]
        with self.assertRaises(self.NS.NamespaceError):
            self.NS.ensure_table(self.conn, legacy)
        with self.assertRaises(self.NS.NamespaceError):
            self.NS.backfill_step(self.conn, legacy, self.embed, allowed=lambda _r: True)

    def test_workflow_records_are_not_backfilled_until_they_are_chunked(self):
        from store import workflow as W
        import memory_redact
        W.checkpoint_write(self.conn, owner="agent:fixture", idempotency_key="namespaces-0001",
                           step_id="S1", status="done", body={"goal": "a goal"},
                           project_id="project:alpha",
                           redactor=memory_redact.Redactor(known_loader=lambda: {}))
        while not self.NS.backfill_step(self.conn, self.ns, self.embed, allowed=lambda _r: True,
                                        batch=10)["complete"]:
            pass
        ids = {r[0] for r in self.conn.execute(f"SELECT memory_id FROM {self.ns.table}")}
        self.assertFalse(any(i.startswith("ckpt:") for i in ids))


class Doctor(unittest.TestCase):
    def test_doctor_names_each_namespace_and_its_state(self):
        conn, _L, NS = fresh()
        NS.register(conn, identity())
        conn.close()
        sys.modules.pop("workspace", None)
        import workspace
        got = workspace._vector_namespaces(pathlib.Path(os.environ["OBSERVATORY_HOME"]))
        self.assertEqual([(n["provider"], n["state"]) for n in got],
                         [("openai", "legacy"), ("local", "inactive")])
        self.assertNotIn("embedding", json.dumps(got).lower().replace("text-embedding", ""),
                         "names and counts only, never vectors")


if __name__ == "__main__":
    unittest.main()
