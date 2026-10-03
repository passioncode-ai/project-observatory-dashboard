#!/usr/bin/env python3
"""Agent memory for a workflow: a checkpoint per step, one executor, handoff packs.

The case the design exists for is driven end to end first: an executor runs out
of quota mid-workflow and cannot say anything, a new session takes the workflow
from what was written step by step, and the old session's late write is refused
without being lost. Every refusal after it is driven against a planted defect —
a stale token, a replayed key with a different request, an expired or superseded
offer, two sessions accepting at once — because a guard nobody has watched
refuse is not evidence.

Synthetic throughout: a temporary store, a temporary git checkout, fixture
secret values. Nothing here reads a workspace, a vault or a provider.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import memory_redact                                                               # noqa: E402
from store import ledger as L                                                      # noqa: E402
from store import migrate                                                          # noqa: E402
from store import workflow as W                                                    # noqa: E402

AGENT_A = "agent:claude-code"
AGENT_B = "agent:claude-code"          # the same identity on another account — the S1 case
CODEX = "agent:codex"
#: A fixture value with no credential SHAPE, so only the known-value filter can
#: catch it. Short words, spaces: nothing a shape heuristic would flag.
KNOWN_VALUE = "correct horse battery staple fixture"
#: Assembled at run time so this file itself carries no key-shaped literal.
SHAPED_KEY = "sk-" + "Abc123" * 6


def redactor(values: dict[str, str] | None = None) -> memory_redact.Redactor:
    return memory_redact.Redactor(known_loader=lambda: dict(values or {}))


def failing_redactor() -> memory_redact.Redactor:
    def boom() -> dict[str, str]:
        raise OSError("vault unreadable")
    return memory_redact.Redactor(known_loader=boom)


def no_git(path: str) -> dict:
    return {"path": path, "head": "0123456789ab", "branch": "main", "dirtyCount": 0,
            "dirty": []}


def no_related(conn, **_kw):
    return [], []


def body(goal: str = "ship the export", step_open: str = "S2",
         constraints: list[str] | None = None, **extra) -> dict:
    out = {"goal": goal,
           "plan": [{"step_id": "S1", "title": "read"}, {"step_id": "S2", "title": "write",
                                                         "needs": ["S1"]}],
           "done": [{"step_id": "S1", "result": "read the schema", "evidence": ["test:schema"]}],
           "open": [{"step_id": step_open, "next_action": "write the exporter"}],
           "constraints": constraints if constraints is not None else ["read-only: do not push"]}
    out.update(extra)
    return out


class Store:
    """A file-backed store in the current shape, so several connections can race."""

    def __init__(self) -> None:
        self.dir = tempfile.TemporaryDirectory(prefix="observatory-workflow-")
        self.path = pathlib.Path(self.dir.name) / "store.db"
        conn = self.connect()
        conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
        conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def close(self) -> None:
        self.dir.cleanup()


class WorkflowCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.conn = self.store.connect()

    def tearDown(self) -> None:
        self.conn.close()
        self.store.close()

    def start(self, **kw) -> dict:
        args = dict(owner=AGENT_A, idempotency_key="key-start-0001", step_id="S1",
                    status="done", body=body(), project_id="project:alpha-web",
                    executor={"provider": "anthropic", "model": "claude-opus-5-5",
                              "accountRef": "acct-a"}, redactor=redactor())
        args.update(kw)
        return W.checkpoint_write(self.conn, **args)

    def write(self, wf: dict, token: str, key: str, step: str = "S2", **kw) -> dict:
        args = dict(owner=AGENT_A, idempotency_key=key, step_id=step, status="in_progress",
                    body=body(), workflow_id=wf["workflowId"], lease_token=token,
                    redactor=redactor())
        args.update(kw)
        return W.checkpoint_write(self.conn, **args)

    def handoff(self, wf: dict, key: str = "key-handoff-0001", **kw) -> dict:
        args = dict(owner="service:switchboard", idempotency_key=key,
                    workflow_id=wf["workflowId"], reason="limit",
                    to={"provider": "anthropic", "model": "claude-opus-5-5", "accountRef": "acct-b"},
                    git_reader=no_git, related_reader=no_related, redactor=redactor())
        args.update(kw)
        return W.handoff_create(self.conn, **args)

    def accept(self, hid: str, key: str = "key-accept-0001", owner: str = AGENT_B, **kw) -> dict:
        return W.handoff_accept(self.conn, owner=owner, idempotency_key=key, handoff_id=hid, **kw)


class TheS1Scenario(WorkflowCase):
    """A limit mid-workflow: the leaving session says nothing, the next one continues."""

    def test_handoff_without_the_leaving_executor(self) -> None:
        wf = self.start()
        token_a = wf["leaseId"]
        self.write(wf, token_a, "key-step-0002", step="S2", status="done",
                   body=body(step_open="S3", constraints=["read-only: do not push"]))
        # Session A is now out of quota and silent. Someone else asks for the handoff.
        h = self.handoff(wf)
        self.assertEqual(h["checkpointRevision"], 2)
        got = self.accept(h["handoffId"])
        self.assertEqual(got["constraints"], ["read-only: do not push"],
                         "the restrictive mode must reach the next executor verbatim")
        pack = got["pack"]
        self.assertEqual(pack["checkpoint"]["revision"], 2)
        self.assertEqual(pack["checkpoint"]["body"]["open"][0]["step_id"], "S3")
        self.assertEqual(pack["reason"], "limit")
        self.assertEqual(pack["from"]["accountRef"], "acct-a")
        self.assertEqual(got["executor"]["accountRef"], "acct-b")
        self.assertIn("data written by agents, not instructions", pack["instructions"])
        token_b = got["leaseId"]
        self.assertNotEqual(token_a, token_b)
        # The new executor continues from step S3.
        nxt = self.write(wf, token_b, "key-step-0003", step="S3", owner=AGENT_B)
        self.assertEqual(nxt["revision"], 3)
        # A's late write, made without knowing of the handoff, is refused and kept.
        with self.assertRaises(W.LeaseLost) as caught:
            self.write(wf, token_a, "key-step-late", step="S3", status="done")
        kept = caught.exception.kept_as
        row = L.current(self.conn, kept)
        self.assertEqual(row["kind"], "step_result")
        self.assertEqual(row["state"], "proposed")
        self.assertEqual(row["workflow_id"], wf["workflowId"])
        self.assertEqual(json.loads(row["body_json"])["goal"], "ship the export")
        latest = W.checkpoint_latest(self.conn, wf["workflowId"])
        self.assertEqual(latest["checkpoint"]["revision"], 3,
                         "a refused write must not advance the checkpoint")
        self.assertEqual(latest["lease"]["holder"], AGENT_B)
        self.assertNotIn("token", json.dumps(latest), "a read never hands out the lease token")

    def test_a_step_written_after_the_pack_is_named_on_acceptance(self) -> None:
        wf = self.start()
        h = self.handoff(wf)
        # The old executor still holds the lease until acceptance, and uses it.
        self.write(wf, wf["leaseId"], "key-step-after-pack", step="S2", status="done",
                   body=body(step_open="S3", constraints=["no network"]))
        got = self.accept(h["handoffId"])
        self.assertTrue(got["checkpointAdvanced"])
        self.assertEqual(got["checkpoint"]["revision"], 2)
        self.assertEqual(got["pack"]["checkpoint"]["revision"], 1)
        self.assertEqual(got["constraints"], ["no network"],
                         "the constraints in force are the newest checkpoint's, not the pack's")

    def test_planned_route_to_another_provider(self) -> None:
        wf = self.start()
        h = self.handoff(wf, reason="plan_route", to={"provider": "openai", "model": "gpt-5.5"})
        with self.assertRaises(W.InvalidInput):
            self.accept(h["handoffId"], executor={"provider": "anthropic"})
        got = self.accept(h["handoffId"], key="key-accept-0002", owner=CODEX,
                          executor={"provider": "openai", "accountRef": "codex-1"})
        self.assertEqual(got["executor"], {"provider": "openai", "model": "gpt-5.5",
                                           "accountRef": "codex-1"})


class Lease(WorkflowCase):
    def test_new_workflow_hands_its_creator_the_lease(self) -> None:
        wf = self.start()
        self.assertTrue(wf["leaseId"].startswith("wl_"))
        self.assertRegex(wf["workflowId"], W.WORKFLOW_ID)
        again = self.write(wf, wf["leaseId"], "key-step-0002")
        self.assertEqual(again["revision"], 2)
        self.assertNotIn("leaseId", again, "only the first write mints a token")
        rows = L.history(self.conn, f"ckpt:{wf['workflowId']}")
        self.assertEqual([r["revision"] for r in rows], [1, 2])
        self.assertEqual(json.loads(rows[1]["supersedes_json"]),
                         [f"ckpt:{wf['workflowId']}@1"])
        self.assertEqual(rows[1]["state"], "observed",
                         "a checkpoint is working state, not a claim for the review queue")

    def test_write_without_a_token_is_refused_and_kept(self) -> None:
        wf = self.start()
        with self.assertRaises(W.LeaseLost) as caught:
            self.write(wf, None, "key-no-token")
        self.assertIsNotNone(L.current(self.conn, caught.exception.kept_as))

    def test_replayed_refusal_keeps_one_episode(self) -> None:
        wf = self.start()
        for _ in range(2):
            with self.assertRaises(W.LeaseLost) as caught:
                self.write(wf, "wl_not-the-token", "key-wrong-token")
        kept = self.conn.execute("SELECT count(*) FROM ledger WHERE kind = 'step_result'"
                                 ).fetchone()[0]
        self.assertEqual(kept, 1, "a retried refusal must not keep the same work twice")
        self.assertIsNotNone(caught.exception.kept_as)

    def test_expected_revision_is_compare_and_swap(self) -> None:
        wf = self.start()
        with self.assertRaises(L.RevisionConflict):
            self.write(wf, wf["leaseId"], "key-cas-0001", expected_revision=5)
        ok = self.write(wf, wf["leaseId"], "key-cas-0002", expected_revision=1)
        self.assertEqual(ok["revision"], 2)

    def test_close_ends_the_workflow(self) -> None:
        wf = self.start()
        self.write(wf, wf["leaseId"], "key-close-0001", status="done", close=True)
        state = W.checkpoint_latest(self.conn, wf["workflowId"])
        self.assertEqual(state["status"], "closed")
        self.assertIsNone(state["lease"])
        with self.assertRaises(W.WorkflowClosed):
            self.write(wf, wf["leaseId"], "key-close-0002")
        with self.assertRaises(W.WorkflowClosed):
            self.handoff(wf)

    def test_operator_and_blank_writers_are_refused(self) -> None:
        with self.assertRaises(L.OwnerRefused):
            self.start(owner="operator")
        with self.assertRaises(L.OwnerRequired):
            self.start(owner=" ")


class Handoff(WorkflowCase):
    def test_accepting_twice_is_refused_and_a_retry_replays(self) -> None:
        wf = self.start()
        h = self.handoff(wf)
        first = self.accept(h["handoffId"])
        again = self.accept(h["handoffId"])                   # same key: a retry
        self.assertTrue(again["replayed"])
        self.assertEqual(again["leaseId"], first["leaseId"])
        with self.assertRaises(W.HandoffAlreadyAccepted):
            self.accept(h["handoffId"], key="key-accept-other")

    def test_an_expired_offer_cannot_be_accepted_and_the_old_lease_stands(self) -> None:
        wf = self.start()
        h = self.handoff(wf)
        self.conn.execute("UPDATE workflow_leases SET expires_at = '2000-01-01T00:00:00Z'"
                          " WHERE handoff_id = ?", (h["handoffId"],))
        self.conn.commit()
        with self.assertRaises(W.HandoffExpired):
            self.accept(h["handoffId"])
        self.assertEqual(W.handoff_get(self.conn, h["handoffId"])["status"], "expired")
        still = self.write(wf, wf["leaseId"], "key-after-lapse")
        self.assertEqual(still["revision"], 2, "a lapsed offer leaves the executor in place")

    def test_a_newer_handoff_supersedes_an_unaccepted_one(self) -> None:
        wf = self.start()
        old = self.handoff(wf)
        new = self.handoff(wf, key="key-handoff-0002", reason="operator")
        with self.assertRaises(W.HandoffSuperseded):
            self.accept(old["handoffId"])
        self.assertEqual(self.accept(new["handoffId"], key="key-accept-0002")["handoffId"],
                         new["handoffId"])

    def test_a_handoff_needs_a_checkpoint_built_from_the_latest(self) -> None:
        wf = self.start()
        self.conn.execute("DELETE FROM ledger WHERE kind = 'checkpoint'")
        self.conn.commit()
        with self.assertRaises(W.NoCheckpoint):
            self.handoff(wf)

    def test_pack_is_immutable_through_the_ledger(self) -> None:
        wf = self.start()
        h = self.handoff(wf)
        with self.assertRaises(L.LedgerError):
            L.append(self.conn, owner="service:switchboard", statement="edited",
                     memory_id=h["handoffId"], expected_revision=1)
        with self.assertRaises(L.LedgerError):
            L.append(self.conn, owner=AGENT_A, statement="edited",
                     memory_id=f"ckpt:{wf['workflowId']}", expected_revision=1)
        with self.assertRaises(L.LedgerError):
            L.append(self.conn, owner=AGENT_A, statement="forged", kind="checkpoint")

    def test_concurrent_acceptance_has_one_winner(self) -> None:
        wf = self.start()
        h = self.handoff(wf)
        results: list[str] = []

        def take(i: int) -> None:
            conn = self.store.connect()
            try:
                W.handoff_accept(conn, owner=AGENT_B, idempotency_key=f"key-race-{i:04d}",
                                 handoff_id=h["handoffId"])
                results.append("won")
            except W.HandoffAlreadyAccepted:
                results.append("refused")
            finally:
                conn.close()

        # WIDEN THE WINDOW. Without a pause inside the transaction the six
        # threads run one after another under the GIL, and a deferred
        # transaction — read the offer, then write — passes this test while
        # letting two readers see the same offer. A pause inside `work` makes
        # every reader overlap, which only `BEGIN IMMEDIATE` survives.
        import time
        real_now = W._now

        def slow_now():
            time.sleep(0.05)
            return real_now()

        W._now = slow_now
        try:
            threads = [threading.Thread(target=take, args=(i,)) for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            W._now = real_now
        self.assertEqual(sorted(results), ["refused"] * 5 + ["won"])
        active = self.conn.execute("SELECT count(*) FROM workflow_leases WHERE workflow_id = ?"
                                   " AND state = 'active'", (wf["workflowId"],)).fetchone()[0]
        self.assertEqual(active, 1)

    def test_transcript_pointer_is_a_session_uuid_only(self) -> None:
        wf = self.start()
        with self.assertRaises(W.InvalidInput):
            self.handoff(wf, transcript={"provider": "claude-code", "sessionId": "../../etc"})
        h = self.handoff(wf, key="key-handoff-0003", transcript={
            "provider": "claude-code", "sessionId": "0f8fad5b-d9cb-469f-a165-70867728950e"})
        pack = W.handoff_get(self.conn, h["handoffId"])["pack"]
        self.assertEqual(pack["transcript"]["sessionId"], "0f8fad5b-d9cb-469f-a165-70867728950e")


class Idempotency(WorkflowCase):
    def test_same_key_same_request_replays(self) -> None:
        wf = self.start()
        again = self.start()
        self.assertTrue(again["replayed"])
        self.assertEqual(again["workflowId"], wf["workflowId"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM workflows").fetchone()[0], 1)

    def test_same_key_different_request_is_refused(self) -> None:
        self.start()
        with self.assertRaises(W.IdempotencyConflict):
            self.start(body=body(goal="something else"))

    def test_a_key_must_be_a_key(self) -> None:
        with self.assertRaises(W.InvalidInput):
            self.start(idempotency_key="short")


class Redaction(WorkflowCase):
    def test_shapes_and_known_values_never_reach_the_store(self) -> None:
        dirty = body(notes=f"the error said {SHAPED_KEY} and the password is {KNOWN_VALUE}")
        wf = self.start(body=dirty, redactor=redactor({KNOWN_VALUE: "alpha-web/DB_PASSWORD"}))
        self.assertEqual(wf["redacted"]["shapes"], 1)
        self.assertEqual(wf["redacted"]["knownValues"], 1)
        self.assertTrue(wf["redacted"]["knownValuesChecked"])
        dump = "\n".join(str(tuple(r)) for r in self.conn.execute("SELECT * FROM ledger"))
        dump += "\n".join(str(tuple(r)) for r in self.conn.execute("SELECT * FROM idempotency"))
        self.assertNotIn(SHAPED_KEY, dump)
        self.assertNotIn(KNOWN_VALUE, dump)
        self.assertIn("[redacted:alpha-web/DB_PASSWORD]", dump)

    def test_unreadable_known_values_degrade_honestly(self) -> None:
        wf = self.start(body=body(notes=f"token {SHAPED_KEY}"), redactor=failing_redactor())
        self.assertFalse(wf["redacted"]["knownValuesChecked"])
        self.assertEqual(wf["redacted"]["shapes"], 1, "shapes still run without the values")

    def test_account_handles_are_opaque(self) -> None:
        with self.assertRaises(W.InvalidInput):
            self.start(executor={"provider": "anthropic", "accountRef": "someone@example.com"})
        with self.assertRaises(W.InvalidInput):
            self.start(executor={"provider": "anthropic", "accountRef": SHAPED_KEY})

    def test_unknown_body_fields_are_refused(self) -> None:
        with self.assertRaises(W.InvalidInput):
            self.start(body={"goal": "x", "summary": "prose instead of state"})


class GitAndRelated(WorkflowCase):
    def test_git_snapshot_reads_state_not_contents(self) -> None:
        with tempfile.TemporaryDirectory(prefix="observatory-wf-repo-") as d:
            env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
            run = lambda *a: subprocess.run(["git", *a], cwd=d, env=env, check=True,
                                            capture_output=True)
            run("init", "-q", "-b", "main")
            run("config", "user.email", "fixture@example.invalid")
            run("config", "user.name", "Fixture")
            pathlib.Path(d, "a.txt").write_text("secret-free contents\n")
            run("add", "-A")
            run("commit", "-qm", "first")
            pathlib.Path(d, "a.txt").write_text("changed\n")
            pathlib.Path(d, "new.txt").write_text("new\n")
            snap = W.git_snapshot(d)
        self.assertNotIn("error", snap)
        self.assertEqual(snap["branch"], "main")
        self.assertEqual(len(snap["head"]), 12)
        self.assertEqual(sorted(p["path"] for p in snap["dirty"]), ["a.txt", "new.txt"])
        self.assertNotIn("changed", json.dumps(snap))
        self.assertIn("error", W.git_snapshot("/nonexistent/observatory-fixture"))
        self.assertIn("error", W.git_snapshot("relative/path"))

    def test_related_records_are_lexical_and_exclude_this_workflow(self) -> None:
        wf = self.start(body=body(goal="rotate the exporter schema"))
        other = L.append(self.conn, owner=AGENT_A, project_id="project:alpha-web",
                         statement="the exporter schema needs a version column", confidence=0.5)
        unrelated = L.append(self.conn, owner=AGENT_A, project_id="project:alpha-web",
                             statement="the landing page colour", confidence=0.5)
        for mid in (other["memoryId"], unrelated["memoryId"], f"ckpt:{wf['workflowId']}"):
            row = L.current(self.conn, mid)
            self.conn.execute("INSERT INTO search_notes (memory_id, revision, statement, why)"
                              " VALUES (?,?,?,?)", (mid, row["revision"], row["statement"], None))
        self.conn.commit()
        related, degraded = W.related_records(
            self.conn, body=body(goal="rotate the exporter schema"),
            project_id="project:alpha-web", workflow_id=wf["workflowId"])
        ids = [r["memoryId"] for r in related]
        self.assertIn(other["memoryId"], ids)
        self.assertNotIn(f"ckpt:{wf['workflowId']}", ids)
        self.assertTrue(any("not searchable yet" in d["reason"] for d in degraded),
                        "unindexed writes must be named, not read as 'nothing related'")


class Retention(WorkflowCase):
    def cutoff(self, days: int) -> str:
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_open_workflows_keep_their_checkpoints(self) -> None:
        wf = self.start()
        self.conn.execute("UPDATE ledger SET created_at = '2000-01-01T00:00:00Z'")
        self.conn.commit()
        got = W.retention_candidates(self.conn, closed_checkpoint_days=30, handoff_days=90,
                                     cutoff=self.cutoff)
        self.assertEqual(got, [], f"open workflow {wf['workflowId']} must keep its checkpoint")

    def test_closed_workflows_and_old_packs_age_out(self) -> None:
        wf = self.start()
        self.handoff(wf)
        self.write(wf, wf["leaseId"], "key-close-ret", status="done", close=True)
        self.conn.execute("UPDATE workflows SET closed_at = '2000-01-01T00:00:00Z'")
        self.conn.execute("UPDATE ledger SET created_at = '2000-01-01T00:00:00Z'"
                          " WHERE kind = 'handoff'")
        self.conn.commit()
        got = W.retention_candidates(self.conn, closed_checkpoint_days=30, handoff_days=90,
                                     cutoff=self.cutoff)
        self.assertEqual(sorted(c["kind"] for c in got), ["checkpoint", "handoff"])


class Projection(WorkflowCase):
    """A workflow's records never reach the embedding provider."""

    def test_checkpoints_and_packs_are_indexed_lexically_only(self) -> None:
        sys.path.insert(0, str(ROOT / "agent"))
        from store import indexer
        if not indexer.load_vec(self.conn):
            self.skipTest("sqlite-vec is not loadable in this interpreter")
        indexer.ensure_vec_table(self.conn, 3)
        wf = self.start()
        h = self.handoff(wf)
        note = L.append(self.conn, owner=AGENT_A, statement="an ordinary note", confidence=0.5)
        sent: list[str] = []

        def embed(texts, log=print):
            sent.extend(texts)
            return {"vectors": [[0.1, 0.2, 0.3] for _ in texts], "cost": 0.0, "tokens": 0}

        real = indexer.providers.embed
        indexer.providers.embed = embed
        try:
            rows = [indexer.indexable(self.conn, mid, 1)
                    for mid in (f"ckpt:{wf['workflowId']}", h["handoffId"], note["memoryId"])]
            written, _c, _t, ok = indexer.index_batch(self.conn, rows, True, 3)
        finally:
            indexer.providers.embed = real
        self.assertEqual(written, 3, "all three are in the lexical index")
        self.assertTrue(ok)
        self.assertEqual(sent, ["an ordinary note"], "only the note was sent for embedding")
        vec = {r[0] for r in self.conn.execute("SELECT memory_id FROM vec_notes")}
        self.assertEqual(vec, {note["memoryId"]})


class RetentionSeam(WorkflowCase):
    """`store/retention.py` applies the workflow horizons, not the generic ones."""

    def test_an_old_open_checkpoint_survives_and_a_closed_one_ages_out(self) -> None:
        from store import retention
        defaults = json.loads((ROOT / "defaults/retention.json").read_text(encoding="utf-8"))
        real = retention.config
        retention.config = lambda: defaults
        try:
            wf = self.start()
            self.conn.execute("UPDATE ledger SET created_at = '2000-01-01T00:00:00Z'")
            self.conn.commit()
            self.assertEqual(retention.ledger_candidates(self.conn), [],
                             "an observed checkpoint 26 years old is still the open work")
            self.write(wf, wf["leaseId"], "key-ret-close", status="done", close=True)
            self.conn.execute("UPDATE workflows SET closed_at = '2000-01-01T00:00:00Z'")
            self.conn.commit()
            got = retention.ledger_candidates(self.conn)
            self.assertEqual([c["kind"] for c in got], ["checkpoint"])
            self.assertIn("closed more than 30 days", got[0]["reason"])
        finally:
            retention.config = real


class Migration(unittest.TestCase):
    def test_a_pre_workflow_store_gains_the_shape_and_keeps_its_rows(self) -> None:
        with tempfile.TemporaryDirectory(prefix="observatory-wf-mig-") as d:
            conn = sqlite3.connect(pathlib.Path(d) / "old.db")
            conn.row_factory = sqlite3.Row
            schema = (ROOT / "store/schema.sql").read_text(encoding="utf-8")
            # The ledger as 0.13.0 created it: no workflow columns, no workflow tables.
            old = schema.split("-- AGENT MEMORY (migration 0008)")[0] + \
                "  PRIMARY KEY (memory_id, revision)\n) STRICT;\n"
            old = old[old.index("CREATE TABLE IF NOT EXISTS ledger"):]
            conn.executescript(old + "CREATE TABLE outbox (seq INTEGER PRIMARY KEY"
                               " AUTOINCREMENT, memory_id TEXT NOT NULL, revision INTEGER NOT NULL,"
                               " projection_version INTEGER NOT NULL DEFAULT 1, consumed_at TEXT)"
                               " STRICT; CREATE TABLE tombstones (memory_id TEXT, revision INTEGER,"
                               " reason TEXT, approved_by TEXT, created_at TEXT);")
            conn.execute("INSERT INTO ledger (memory_id, revision, kind, function, scope,"
                         " statement, state, owner, created_at) VALUES ('mem:old', 1, 'note',"
                         " 'episodic', 'project', 'kept', 'proposed', 'agent:x',"
                         " '2026-01-01T00:00:00Z')")
            note = migrate._agent_memory_workflows(conn)
            self.assertIn("workflow_id", note)
            self.assertIn("already carries", migrate._agent_memory_workflows(conn),
                          "a second run must be a no-op, as on a store built from the schema")
            row = L.current(conn, "mem:old")
            self.assertEqual(row["statement"], "kept")
            self.assertIsNone(row["workflow_id"])
            wf = W.checkpoint_write(conn, owner=AGENT_A, idempotency_key="key-mig-0001",
                                    step_id="S1", status="done", body=body(),
                                    redactor=redactor())
            self.assertEqual(wf["revision"], 1)
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
