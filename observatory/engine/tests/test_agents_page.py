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
        # OSS-26: a command copied from a non-default workspace runs against that workspace.
        import paths
        self.assertTrue(stalled["command"].startswith(f"OBSERVATORY_HOME='{paths.HOME}' project-observatory full "),
                        stalled["command"])
        page = agents_page.agents_html({"agents": self.view}, Translator("en"))
        self.assertNotIn(">project-observatory full workflow show", page)

    def test_an_empty_value_shows_the_shared_mark_and_no_em_dash(self) -> None:
        """OBS-596: every value the page has none for (no project, no handoff reason, no step, no age,
        no executor) shows `i18n.NO_VALUE`, and no sentence joins its parts with an em dash."""
        import copy
        from i18n import NO_VALUE
        view = copy.deepcopy(self.view)
        wf = view["workflows"][0]
        wf["projectId"] = None
        wf["step"] = {}
        for s in wf["segments"]:
            s["arrivedBy"] = {"reason": None}
            s["executor"] = {}
        wf["offers"] = [{"state": "offered", "reason": "limit", "to": {}, "at": None, "expiresAt": None},
                        {"state": "lapsed", "reason": None, "to": {"provider": "openai"}, "at": None}]
        view["needsYou"] = view["needsYou"] + [
            {"kind": "credential-env", "workflowId": wf["workflowId"], "name": "ALPHA_KEY", "env": "prod"},
            {"kind": "stalled", "workflowId": wf["workflowId"], "seconds": None}]
        view["degraded"] = [{"source": "ledger", "reason": "a reason written by a collector"}]
        for locale in ("en", "ru"):
            page = agents_page.agents_html({"agents": view}, Translator(locale))
            self.assertNotIn("\u2014", page, locale)
            self.assertIn(NO_VALUE, page, locale)
        self.assertIn("is only in a .env: move it to the vault", agents_page.agents_html({"agents": view}, Translator("en")))

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
        self.assertIn("Требует решения", ru)
        self.assertIn("лимит", ru, "a handoff reason in the reader's language")
        self.assertNotIn("каталог удалён", ru, "missing here is a key, not a deleted worktree")

    def test_a_card_that_needs_attention_opens_by_itself(self) -> None:
        import re
        page = agents_page.agents_html({"agents": self.view}, Translator("en"))
        card = re.search(r'<details class="card panel agents-wf" id="wf-[^"]+"( open)?><summary><span class="mono">'
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


def sessions_doc(**over) -> dict:
    """A coding-sessions/1 summary (sessions_view): managed, unmanaged and quiet sessions of two agents."""
    doc = {"schema": "coding-sessions/1", "measuredAt": "2026-10-26T17:24:00Z",
           "processes": {"claude-code": 2, "codex": 1}, "managed": 1, "quiet": 1, "active_hour": 1,
           "sessions": [
               {"agent": "codex", "project": "project:beta-api", "folder_name": "beta-api", "pid": 201,
                "managed": False, "launch_id": None, "account_group": None,
                "last_turn_at": "2026-10-26T17:10:00Z", "quiet": False, "state": "working", "active": False},
               {"agent": "claude-code", "project": None, "folder_name": "scratch", "pid": 102, "managed": False,
                "launch_id": None, "account_group": None, "last_turn_at": None, "quiet": True, "state": "quiet",
                "active": False},
               {"agent": "claude-code", "project": "project:alpha-web", "folder_name": "alpha-web", "pid": 101,
                "managed": True, "launch_id": "launch-1", "account_group": "team-a",
                "last_turn_at": "2026-10-26T17:20:00Z", "quiet": False, "state": "working", "active": True}],
           "sources": {"processes": {"from": "manager", "at": "2026-10-26T17:24:00Z"},
                       "manager": {"state": "read", "at": "2026-10-26T17:24:00Z"},
                       "ledger": {"at": "2026-10-26T17:24:00Z"}},
           "degraded": []}
    doc.update(over)
    return doc


def plain(markup: str) -> str:
    import html
    import re
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


class ControlPlaneAgents(unittest.TestCase):
    """CP-02 (OBS-551, OSS-119): Agents opens with the sessions running here; problems come next, one row per
    workflow with its worst state and a button, never a command."""

    def setUp(self) -> None:
        AgentsPage.setUp(self)
        self.rows = [{"id": "project:alpha-web", "name": "Alpha Web"}]

    def tearDown(self) -> None:
        AgentsPage.tearDown(self)

    def page(self, view=None, sessions=None, t=Translator("en")) -> str:
        return agents_page.agents_html({"agents": view or self.view, "rows": self.rows,
                                        "sessions": sessions or sessions_doc()}, t)

    def test_sessions_open_the_page_grouped_by_agent_and_project(self) -> None:
        import re
        page = self.page()
        first = page.index('id="agents-sessions"')
        self.assertLess(first, page.index('id="agents-needs"'))
        self.assertLess(page.index('id="agents-needs"'), page.index('id="agents-workflows"'))
        section = page[first:page.index('id="agents-needs"')]
        self.assertIn("Sessions running", section, "the three facets head the sessions")
        self.assertIn("by the account manager, read 2026-10-26 17:24:00", plain(section))
        agents = re.findall(r'<h3 class="agents-project"[^>]*data-agent="([^"]+)"', section)
        self.assertEqual(agents, ["claude-code", "codex"])
        rows = re.findall(r'<tr data-session="(\d+)">(.*?)</tr>', section, re.S)
        self.assertEqual([pid for pid, _ in rows], ["101", "102", "201"], "by agent, then by project")
        managed = plain(rows[0][1])
        for word in ("Alpha Web", "alpha-web", "yes", "team-a", "working"):
            self.assertIn(word, managed)
        quiet = plain(rows[1][1])
        for word in ("scratch", "no", "quiet"):
            self.assertIn(word, quiet)
        self.assertIn('href="projects.html#project:beta-api"', rows[2][1])

    def test_one_session_list_with_one_rule_for_working(self) -> None:
        """OBS-599 (CP-02, OSS-24, OSS-119): one list, the running sessions; a row reads "working" only when it
        runs and recorded a turn in the last hour (sessions_view's `active`), never by the manager's own quiet
        flag. The working rows are the tile's count, and the ledger's turns are not a second list."""
        import re
        doc = sessions_doc()
        doc["sessions"][0].update(quiet=True, state="quiet", active=True)     # quiet by the manager, a turn by the ledger
        doc["sessions"][2].update(quiet=False, state="working", active=False)
        doc["active_hour"] = 1
        page = self.page(sessions=doc)
        self.assertEqual(page.count("<table"), page[page.index('id="agents-sessions"'):].count("<table"),
                         "every session table is in the one sessions section")
        self.assertNotIn("Recorded turns", page)
        section = page[page.index('id="agents-sessions"'):page.index('id="agents-needs"')]
        rows = dict(re.findall(r'<tr data-session="(\d+)">(.*?)</tr>', section, re.S))
        self.assertIn("working", plain(rows["201"]))
        self.assertIn("quiet", plain(rows["101"]))
        working = sum(1 for r in rows.values() if 'data-t="working"' in r)
        self.assertEqual(working, doc["active_hour"], "the list and the tile count the same sessions")
        self.assertIn("recorded a turn in the last hour", plain(section), "the rule is said on the page")
        unread = sessions_doc()
        for r in unread["sessions"]:
            r["active"] = None
        cells = dict(re.findall(r'<tr data-session="(\d+)">(.*?)</tr>', self.page(sessions=unread), re.S))
        self.assertNotIn('data-t="working"', "".join(cells.values()), "an unread ledger shows no state")

    def test_a_list_written_before_the_rule_is_joined_with_the_ledger_on_the_page(self) -> None:
        import re
        doc = sessions_doc()
        for r in doc["sessions"]:
            del r["active"]
        view = dict(self.view, sessions=[{"projectId": "project:beta-api", "active": True},
                                         {"projectId": "project:alpha-web", "active": False}])
        page = self.page(view=view, sessions=doc)
        rows = dict(re.findall(r'<tr data-session="(\d+)">(.*?)</tr>', page, re.S))
        self.assertIn('data-t="working"', rows["201"])
        self.assertNotIn('data-t="working"', rows["101"])

    def test_the_sessions_tile_puts_its_source_and_its_agents_on_separate_lines(self) -> None:
        """OBS-572: the source line is sentence case and the per-agent counts are a list, not an uppercase run-on."""
        import re
        section = self.page()[self.page().index('id="agents-sessions"'):]
        tile = re.search(r'<div class="tile"><span data-t="Sessions running">.*?</div>', section, re.S).group(0)
        self.assertRegex(tile, r'<span class="tile-sub" data-t="processes the account manager lists, read \{at\}"')
        items = re.findall(r"<li>(.*?)</li>", tile)
        self.assertEqual([" ".join(re.sub(r"<[^>]+>", " ", i).split()) for i in items], ["claude-code 2", "codex 1"])
        self.assertNotIn(" · ", re.sub(r"<[^>]+>", "", tile), "no run-on line of agents")
        self.assertIn(".tile-sub { display: block; text-transform: none", (ROOT / "dashboard/workspace.css").read_text())

    def test_the_live_refresh_keeps_what_a_person_picked_and_never_rebuilds_a_control_in_use(self) -> None:
        """OBS-572: the offer's provider and reason, and its status line, survive the swap; a focused control defers it."""
        src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
        live = src[src.index("(function agentsLive()"):]
        self.assertIn("if (!box || inUse(box)) return;", live)
        self.assertLess(live.index("const form = formState(box);"), live.index("box.replaceWith(fresh);"))
        self.assertLess(live.index("box.replaceWith(fresh);"), live.index("formRestore(fresh, form);"))
        self.assertIn('const FORM = "[data-wf-provider], [data-wf-reason], [data-wf-said]";', live)

    def test_a_session_list_that_was_not_read_says_why_never_none(self) -> None:
        unread = sessions_doc(sessions=[], managed=None, quiet=None,
                              sources={"processes": {"from": "machine-survey", "at": "2026-10-26T17:00:00Z"},
                                       "manager": {"state": "timeout", "at": None}, "ledger": {"at": None}},
                              degraded=[{"source": "manager", "code": "timeout",
                                         "reason": "the account manager did not answer in 10 s"}])
        section = plain(self.page(sessions=unread))
        self.assertIn("the account manager did not answer in 10 s", section)
        self.assertNotIn("No session is running", section)
        empty = plain(self.page(sessions=sessions_doc(sessions=[], processes={}, managed=0)))
        self.assertIn("No session is running", empty)

    def test_one_row_per_workflow_with_its_worst_state_and_its_action(self) -> None:
        import re
        view = dict(self.view, needsYou=[*self.view["needsYou"],
                                         {"kind": "handoff-lapsed", "workflowId": self.b, "projectId": "project:beta-api",
                                          "command": "x"}])
        page = self.page(view)
        needs = page[page.index('id="agents-needs"'):page.index('id="agents-workflows"')]
        rows = re.findall(r'<li class="agents-need" data-wf="([^"]+)" data-worst="([^"]+)">(.*?)</li>', needs, re.S)
        self.assertEqual(sorted((w, k) for w, k, _ in rows),
                         sorted([(self.a, "credential-missing"), (self.b, "stalled")]))
        stalled = next(r for w, _k, r in rows if w == self.b)
        self.assertIn("a handoff lapsed", plain(stalled), "the lesser state is named on the same row")
        button = re.search(r'<button[^>]*data-wf-act="handoff"[^>]*>', stalled).group(0)
        self.assertIn(f'data-wf="{self.b}"', button)
        self.assertIn(" disabled", button, "opened as a file nothing can act")
        self.assertIn("observatory_handoff_create", button)
        self.assertRegex(stalled, r'<select[^>]*data-wf-provider[^>]*>.*<option value="openai"', )
        self.assertIn(f'href="#wf-{self.b}"', stalled)
        keys = next(r for w, _k, r in rows if w == self.a)
        self.assertNotIn("data-wf-act", keys, "a key's value is typed by a person, not pressed for")
        self.assertIn('href="creds.html"', keys)
        self.assertIn('href="health.html#queue-s"', keys, "kept steps are reviewed on the Health page")
        self.assertIn(f'id="wf-{self.b}"', page, "the row's link lands on its workflow")
        self.assertNotIn("<code", page)
        self.assertNotIn("project-observatory", plain(page))

    def test_russian(self) -> None:
        ru = plain(self.page(t=Translator("ru")))
        for word in ("Сессии на этой машине", "Передать другому агенту", "да", "нет"):
            self.assertIn(word, ru)


class HandoffRoute(unittest.TestCase):
    """POST /api/workflow-handoff on the key server: the same store function as observatory_handoff_create."""

    TOKEN = "t" * 32

    def setUp(self) -> None:
        import importlib
        import os
        import threading
        import time
        from unittest.mock import patch
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name).resolve()
        self.home = base / "home"
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.home), "HOME": str(base / "user"),
                                           "USERPROFILE": str(base / "user")}, clear=False)
        self.env.start()
        import workspace
        workspace.initialize(self.home)
        import paths
        importlib.reload(paths)
        from store import db as store_db
        conn = store_db.connect(paths.DB)
        conn.row_factory = sqlite3.Row
        w = W.checkpoint_write(conn, owner="agent:codex", idempotency_key="route-start", step_id="S1",
                               status="in_progress", project_id="project:beta-api",
                               executor={"provider": "openai"}, body=body("migrate the schema"), redactor=QUIET)
        conn.execute("UPDATE ledger SET created_at = ? WHERE memory_id = ?", (past(900), f"ckpt:{w['workflowId']}"))
        conn.commit()
        conn.close()
        self.wid = w["workflowId"]
        sys.path.insert(0, str(ROOT / "tools"))
        import keyserver
        self.ks = keyserver
        self.audit = keyserver.AUDIT
        keyserver.AUDIT = base / "keyserver.jsonl"
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.srv = keyserver.Server(("127.0.0.1", self.port), keyserver.Handler, self.TOKEN)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        time.sleep(0.1)

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.ks.AUDIT = self.audit
        self.env.stop()
        self.tmp.cleanup()

    def post(self, body: dict) -> tuple[int, dict]:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/workflow-handoff",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Observatory-Token": self.TOKEN,
                                              "X-Observatory-Caller": "page:agents",
                                              "Origin": f"http://127.0.0.1:{self.port}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read().decode())

    def test_offer_a_silent_workflow_to_another_agent(self) -> None:
        code, said = self.post({"workflowId": self.wid, "provider": "anthropic", "reason": "crash"})
        self.assertEqual(code, 200, said)
        self.assertTrue(said["handoffId"].startswith("handoff:"), said)
        self.assertIn("expiresAt", said)
        self.assertNotIn("leaseId", json.dumps(said))
        rows = [json.loads(x) for x in self.ks.AUDIT.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(r["action"], r["subject"]) for r in rows], [("workflow-handoff", self.wid)])
        self.assertEqual((rows[0]["provider"], rows[0]["reason"]), ("anthropic", "crash"))

    def test_refusals_are_typed_and_write_nothing(self) -> None:
        for body_, want in (({"workflowId": self.wid, "provider": "anthropic", "reason": "operator"}, 400),
                            ({"workflowId": self.wid, "provider": "", "reason": "crash"}, 400),
                            ({"workflowId": self.wid, "provider": "a b/c", "reason": "crash"}, 400),
                            ({"workflowId": "../etc", "provider": "anthropic", "reason": "crash"}, 400),
                            ({"workflowId": "wf_0000000000000000", "provider": "anthropic", "reason": "crash"}, 404)):
            with self.subTest(body=body_):
                code, said = self.post(body_)
                self.assertEqual(code, want, said)
                self.assertIn("error", said)


if __name__ == "__main__":
    unittest.main(verbosity=2)
