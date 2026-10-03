#!/usr/bin/env python3
"""The Agents page: which agents are working, on what, and where their work moved.

Driven over a real store built by the workflow operations themselves — a workflow
handed between two accounts after a limit, a stalled one, a kept step, a session
tied to its workflow — and rendered in both languages. The states a person can
meet before any of that exists (no store, a store from before agent memory, no
workflow yet) are driven too, because "nothing to show" and "nothing could be
read" must not look alike. Synthetic throughout.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))

import agents_view                                                                 # noqa: E402
import agents_page                                                                 # noqa: E402
import memory_redact                                                               # noqa: E402
from i18n import Translator                                                        # noqa: E402
from store import ledger as L                                                      # noqa: E402
from store import workflow as W                                                    # noqa: E402

QUIET = memory_redact.Redactor(known_loader=lambda: {})


def body(goal: str, **extra) -> dict:
    out = {"goal": goal, "open": [{"step_id": "S2", "next_action": "write the exporter"}],
           "constraints": ["read-only: do not push"]}
    out.update(extra)
    return out


def no_keys(project: str) -> dict:
    return {"project": project.split(":", 1)[-1], "vault": [], "env": [], "degraded": []}


class Estate:
    def __init__(self) -> None:
        self.dir = tempfile.TemporaryDirectory(prefix="observatory-agents-")
        self.db = pathlib.Path(self.dir.name) / "store.db"
        conn = sqlite3.connect(self.db)
        conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
        conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def close(self) -> None:
        self.dir.cleanup()


def past(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class AgentsPage(unittest.TestCase):
    def setUp(self) -> None:
        self.estate = Estate()
        conn = self.estate.connect()
        # Workflow A: started on account a, ran out, handed to account b.
        a = W.checkpoint_write(conn, owner="agent:claude-code", idempotency_key="ag-a-start",
                               step_id="S1", status="done", project_id="project:alpha-web",
                               executor={"provider": "anthropic", "accountRef": "acct-a"},
                               body=body("ship the <script>exporter</script>", credentials=[
                                   {"project": "alpha-web", "env": "prod", "name": "STRIPE_KEY"}]),
                               redactor=QUIET)
        conn.execute("UPDATE ledger SET created_at = ? WHERE memory_id = ?",
                     (past(600), f"ckpt:{a['workflowId']}"))
        conn.commit()
        h = W.handoff_create(conn, owner="service:switchboard", idempotency_key="ag-a-handoff",
                             workflow_id=a["workflowId"], reason="limit",
                             to={"provider": "anthropic", "accountRef": "acct-b"},
                             git_reader=lambda p: {"path": p}, related_reader=lambda c, **k: ([], []),
                             credential_reader=no_keys, redactor=QUIET)
        got = W.handoff_accept(conn, owner="agent:claude-code", idempotency_key="ag-a-accept",
                               handoff_id=h["handoffId"], credential_reader=no_keys)
        W.checkpoint_write(conn, owner="agent:claude-code", idempotency_key="ag-a-step2",
                           step_id="S2", status="in_progress", workflow_id=a["workflowId"],
                           lease_token=got["leaseId"], body=body("ship the <script>exporter</script>", credentials=[
                               {"project": "alpha-web", "env": "prod", "name": "STRIPE_KEY"}]),
                           redactor=QUIET)
        with self.assertRaises(W.LeaseLost):
            W.checkpoint_write(conn, owner="agent:claude-code", idempotency_key="ag-a-late",
                               step_id="S2", status="done", workflow_id=a["workflowId"],
                               lease_token=a["leaseId"], body=body("late"), redactor=QUIET)
        # Workflow B: silent for an hour.
        b = W.checkpoint_write(conn, owner="agent:codex", idempotency_key="ag-b-start",
                               step_id="S1", status="in_progress", project_id="project:beta-api",
                               executor={"provider": "openai", "model": "gpt-5.5"},
                               body=body("migrate the schema"), redactor=QUIET)
        conn.execute("UPDATE ledger SET created_at = ? WHERE memory_id = ?",
                     (past(3600), f"ckpt:{b['workflowId']}"))
        # A session working on A right now.
        L.append(conn, owner="agent:observatory-log", kind="session", statement="turn",
                 project_id="project:alpha-web", workflow_id=a["workflowId"],
                 session_id="0f8fad5b-d9cb-469f-a165-70867728950e")
        conn.commit()
        conn.close()
        self.a, self.b = a["workflowId"], b["workflowId"]
        self.view = agents_view.summary(self.estate.db, credential_reader=no_keys)

    def tearDown(self) -> None:
        self.estate.close()

    def test_counters_and_what_needs_a_person(self) -> None:
        c = self.view["counters"]
        self.assertEqual((c["workflowsOpen"], c["stalled"], c["keptSteps"], c["sessionsActive"]),
                         (2, 1, 1, 1), c)
        kinds = sorted(n["kind"] for n in self.view["needsYou"])
        self.assertEqual(kinds, ["credential-missing", "kept-steps", "stalled"], kinds)
        stalled = next(n for n in self.view["needsYou"] if n["kind"] == "stalled")
        self.assertEqual(stalled["workflowId"], self.b)
        self.assertIn(f"workflow show {self.b}", stalled["command"])

    def test_the_lanes_follow_the_workflow_across_accounts(self) -> None:
        wf = next(w for w in self.view["workflows"] if w["workflowId"] == self.a)
        segs = wf["segments"]
        self.assertEqual([s["executor"].get("accountRef") for s in segs], ["acct-a", "acct-b"])
        self.assertEqual(segs[1]["arrivedBy"]["reason"], "limit")
        self.assertEqual(segs[0]["endedReason"], "handed-off")
        self.assertEqual([c["stepId"] for c in segs[0]["checkpoints"]], ["S1"])
        self.assertEqual([c["stepId"] for c in segs[1]["checkpoints"]], ["S2"])

    def test_no_token_and_no_value_reaches_the_page(self) -> None:
        dump = json.dumps(self.view)
        self.assertNotIn("wl_", dump)
        page = agents_page.agents_html({"agents": self.view}, Translator("en"))
        self.assertNotIn("wl_", page)

    def test_the_page_renders_both_languages_and_escapes_what_agents_wrote(self) -> None:
        en = agents_page.agents_html({"agents": self.view}, Translator("en"))
        self.assertIn("Needs you", en)
        self.assertIn(self.a, en)
        self.assertNotIn("<script>exporter", en)
        self.assertIn("&lt;script&gt;exporter", en)
        self.assertIn("STRIPE_KEY", en)
        self.assertIn('class="agents-events', en, "the lanes have an ordered list beside them")
        self.assertIn("details", en)
        ru = agents_page.agents_html({"agents": self.view}, Translator("ru"))
        self.assertIn("Нужно вам", ru)
        self.assertIn("лимит", ru, "a handoff reason in the reader's language")
        self.assertNotIn("каталог удалён", ru, "missing here is a key, not a deleted worktree")

    def test_a_card_that_needs_attention_opens_by_itself(self) -> None:
        import re
        page = agents_page.agents_html({"agents": self.view}, Translator("en"))
        card = re.search(r'<details class="card panel agents-wf"( open)?><summary><span class="mono">'
                         + re.escape(self.b), page)
        self.assertIsNotNone(card)
        self.assertEqual(card.group(1), " open", "the stalled workflow's card opens by itself")


class States(unittest.TestCase):
    """Nothing to show and nothing could be read must not look alike."""

    def test_no_store(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            view = agents_view.summary(pathlib.Path(d) / "absent.db")
        self.assertEqual(view["degraded"][0]["code"], "no-store")
        page = agents_page.agents_html({"agents": view}, Translator("en"))
        self.assertIn("the store does not exist yet", page)
        self.assertNotIn("No workflow yet", page, "an unread store is not an empty one")

    def test_a_store_from_before_agent_memory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = pathlib.Path(d) / "old.db"
            sqlite3.connect(db).execute("CREATE TABLE ledger (memory_id TEXT)").connection.close()
            view = agents_view.summary(db)
        self.assertEqual(view["degraded"][0]["code"], "before-agent-memory")

    def test_an_empty_store_says_how_a_workflow_starts(self) -> None:
        estate = Estate()
        try:
            view = agents_view.summary(estate.db)
        finally:
            estate.close()
        self.assertEqual(view["degraded"], [])
        page = agents_page.agents_html({"agents": view}, Translator("en"))
        self.assertIn("No workflow yet", page)
        self.assertIn("Nothing is waiting for you.", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
