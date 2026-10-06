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

    def test_a_key_that_could_not_be_checked_waits_for_a_person_too(self) -> None:
        # Audit A10: `unknown` blocks the workflow as `missing` does, so it is named.
        def blind(project: str) -> dict:
            return {"project": project.split(":", 1)[-1], "vault": [], "env": [],
                    "degraded": [{"source": "vault", "reason": "synthetic: not listable"}]}
        view = agents_view.summary(self.estate.db, credential_reader=blind)
        need = next(n for n in view["needsYou"] if n["kind"].startswith("credential-"))
        self.assertEqual((need["kind"], need["name"]), ("credential-unknown", "STRIPE_KEY"))
        en = agents_page.agents_html({"agents": view}, Translator("en"))
        self.assertIn("could not be checked", en)
        ru = agents_page.agents_html({"agents": view}, Translator("ru"))
        self.assertIn("не удалось проверить", ru)

    def test_projects_read_as_names_and_link_to_their_panel(self) -> None:
        # Audit A27: the page showed raw `project:…` ids.
        view = dict(self.view, projectNames={"project:alpha-web": "Alpha Web"})
        page = agents_page.agents_html({"agents": view}, Translator("en"))
        self.assertIn('<a href="projects.html#project:alpha-web">Alpha Web</a>', page)
        self.assertIn("project:beta-api", page, "an id the registry does not name stays an id")

    def test_the_names_come_from_the_registry_and_never_fail_the_page(self) -> None:
        import paths
        reg = pathlib.Path(self.estate.dir.name) / "registry"
        reg.mkdir()
        (reg / "projects.json").write_text(json.dumps({"projects": [
            {"id": "project:alpha-web", "name": "Alpha Web"}]}), encoding="utf-8")
        old = paths.REGISTRY
        try:
            paths.REGISTRY = reg
            self.assertEqual(agents_view.project_names({"project:alpha-web", "project:x"}),
                             {"project:alpha-web": "Alpha Web", "project:x": "project:x"})
            (reg / "projects.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(agents_view.project_names({"project:x"}), {"project:x": "project:x"})
        finally:
            paths.REGISTRY = old

    def test_the_signature_ignores_clocks_and_moves_with_what_the_page_says(self) -> None:
        # Audit A27: every 15-second refresh was announced to a screen reader.
        later = agents_view.summary(self.estate.db, credential_reader=no_keys,
                                    now=datetime.now(timezone.utc) + timedelta(seconds=40))
        self.assertEqual(agents_page.signature(self.view), agents_page.signature(later))
        fewer = dict(self.view, needsYou=self.view["needsYou"][:1])
        self.assertNotEqual(agents_page.signature(self.view), agents_page.signature(fewer))
        page = agents_page.agents_html({"agents": self.view}, Translator("en"))
        self.assertIn(f'data-signature="{agents_page.signature(self.view)}"', page)

    def test_workflow_reads_as_the_glossary_word(self) -> None:
        # L10N-06: a workflow is «задача» in every PassionCode product.
        ru = agents_page.agents_html({"agents": self.view}, Translator("ru"))
        self.assertIn(">Задачи<", ru)
        self.assertNotIn(">Workflow", ru)
        self.assertNotIn("Процесс", ru)

    def test_a_page_rendered_in_one_language_reads_in_the_other(self) -> None:
        # L10N-01: the reader's system language decides, so a page built in
        # English is read in Russian. Every word, date and number is marked, so
        # the page script's re-translation says what a Russian render says.
        sys.path.insert(0, str(ROOT / "tests"))
        import relocalize
        for built, reader in (("en", "ru"), ("ru", "en")):
            page = agents_page.agents_html({"agents": self.view}, Translator(built))
            want = agents_page.agents_html({"agents": self.view}, Translator(reader))
            got = relocalize.relocalize(page, reader)
            self.assertTrue(got == want, f"{built}→{reader}: " + relocalize.first_difference(got, want))

    def test_dates_follow_the_readers_language(self) -> None:
        # L10N-05: a Russian page writes 02.01.2026, not 2026-01-02.
        import re
        ru = agents_page.agents_html({"agents": self.view}, Translator("ru"))
        self.assertRegex(ru, r'<time data-date="\d{4}-\d{2}-\d{2} [^"]*">\d{2}\.\d{2}\.\d{4} ')
        shown = re.sub(r'data-[a-z-]+="[^"]*"', "", ru)
        self.assertNotRegex(shown, r">\d{4}-\d{2}-\d{2}")

    def test_an_uncoded_reason_reads_in_russian_when_the_catalog_has_it(self) -> None:
        # L10N-04: the engine's English is the identity; the page translates it.
        view = dict(self.view, degraded=[{"source": "agents", "reason": "the store does not exist yet"},
                                         {"source": "agents", "reason": "synthetic: nobody translated this"}])
        ru = agents_page.agents_html({"agents": view}, Translator("ru"))
        self.assertIn(">хранилище ещё не создано<", ru)
        self.assertIn(">synthetic: nobody translated this<", ru)

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
