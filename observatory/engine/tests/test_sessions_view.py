#!/usr/bin/env python3
"""One session count from three sources (OBS-550, CONTROL-PLANE.md CP-03, CP-04, D1).

Processes say what runs, the account manager says what it manages, the ledger says what
did work. Every source is a fixture here: the manager's JSON is handed to an injected
runner, the machine survey is a file in a temporary workspace, the ledger is a store
built by the ledger's own append. Nothing runs a real command or reads the network.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for sub in ("dashboard", "tools"):
    sys.path.append(str(ROOT / sub))

import sessions_view                                                               # noqa: E402
from i18n import Translator                                                        # noqa: E402
from store import ledger as L                                                      # noqa: E402

NOW = datetime(2026, 11, 14, 17, 31, tzinfo=timezone.utc)
STAMP = int(NOW.timestamp())


def manager_answer(sessions: list[dict]) -> str:
    return json.dumps({"ok": True, "data": {
        "managed": sum(1 for s in sessions if isinstance(s, dict) and s.get("managed")), "observed_at": STAMP - 5,
        "quiet": sum(1 for s in sessions if isinstance(s, dict) and s.get("quiet")), "sessions": sessions}})


def row(pid: int, folder: str, *, agent: str = "claude-code", managed: bool = False, quiet: bool = False,
        launch: str | None = None, **extra) -> dict:
    return {"agent": agent, "folder": folder, "last_write_at": STAMP - 60, "launch_id": launch,
            "managed": managed, "pid": pid, "quiet": quiet, "quiet_minutes": 45 if quiet else 0,
            "started_at": STAMP - 3600, **extra}


SESSIONS = [
    row(101, "/srv/example/projects/alpha-web", managed=True, launch="launch-1", pool="team-a"),
    row(102, "/srv/example/projects/alpha-web/docs"),
    row(103, "/srv/example/projects/beta-api", quiet=True),
    row(104, "/srv/example/scratch/notes", agent="codex"),
]


class Runner:
    """Stands in for the manager's CLI: records each call and answers what it was given."""

    def __init__(self, out: str = "", code: int = 0, raises: BaseException | None = None) -> None:
        self.out, self.code, self.raises, self.calls = out, code, raises, []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if self.raises:
            raise self.raises
        return self.code, self.out


def attribute(folder: str) -> str | None:
    if folder.startswith("/srv/example/projects/alpha-web"):
        return "project:alpha-web"
    if folder.startswith("/srv/example/projects/beta-api"):
        return "project:beta-api"
    return None


class Workspace:
    def __init__(self, survey: dict | None = None, ledger_sessions: int = 0,
                 ledger_projects: tuple[str | None, ...] = ()) -> None:
        self.dir = tempfile.TemporaryDirectory(prefix="observatory-sessions-")
        self.home = pathlib.Path(self.dir.name)
        raw = self.home / "store" / "raw"  # paths-check: allow: a workspace home the caller names, not this engine's
        raw.mkdir(parents=True)
        if survey is not None:
            (raw / "machine.json").write_text(json.dumps(survey), encoding="utf-8")
        db = self.home / "store" / "observatory.db"  # paths-check: allow: a workspace home the caller names, not this engine's
        conn = sqlite3.connect(db)
        conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
        for i in range(ledger_sessions):
            L.append(conn, owner="agent:claude-code", kind="session", statement="turn",
                     project_id="project:alpha-web", session_id=f"0f8fad5b-d9cb-469f-a165-7086772895{i:02d}")
        for i, project in enumerate(ledger_projects):
            L.append(conn, owner="agent:claude-code", kind="session", statement="turn",
                     project_id=project, session_id=f"7c9e6679-7425-40de-944b-e07fc1f904{i:02d}")
        conn.commit()
        conn.close()

    def close(self) -> None:
        self.dir.cleanup()


SURVEY = {"measured_at": "2026-11-14T17:25:00Z", "processes": {"count": 900, "groups": [
    {"origin": "agent:claude-code", "processes": 300, "sessions": 3},
    {"origin": "agent:codex", "processes": 4, "sessions": 1},
    {"origin": "app:Example", "processes": 9, "sessions": 2}]}}


class Summary(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace(SURVEY, ledger_sessions=2)
        self.addCleanup(self.ws.close)

    def run_summary(self, runner, manager="/usr/local/bin/switchboard", **kw) -> dict:
        return sessions_view.summary(self.ws.home, now=NOW, runner=runner, manager=manager,
                                     attribute=attribute, **kw)

    def test_three_facets_from_three_sources(self) -> None:
        runner = Runner(manager_answer(SESSIONS))
        doc = self.run_summary(runner)
        self.assertEqual(runner.calls[0][0][1:], ["sessions", "--machine", "--json"])
        self.assertLessEqual(runner.calls[0][1], sessions_view.MANAGER_TIMEOUT)
        self.assertEqual(doc["processes"], {"claude-code": 3, "codex": 1})
        self.assertEqual(doc["managed"], 1)
        self.assertEqual(doc["active_hour"], 2)
        self.assertEqual(doc["quiet"], 1)
        self.assertEqual(doc["sources"]["processes"]["from"], "manager")
        self.assertEqual(doc["sources"]["manager"]["state"], "read")
        self.assertEqual(doc["sources"]["manager"]["at"], "2026-11-14T17:30:55Z")
        self.assertTrue(doc["sources"]["ledger"]["at"])
        self.assertEqual(doc["degraded"], [])

    def test_active_in_the_last_hour_is_a_subset_of_running(self) -> None:
        """OBS-597: a ledger turn of a session whose process has ended does not count. A running session
        counts when its project holds a session with a turn in the last hour; a project's count is at most
        its running sessions and at most its recent ledger sessions."""
        ws = Workspace(SURVEY, ledger_sessions=3,
                       ledger_projects=("project:beta-api", "project:gamma-ended", "project:gamma-ended", None))
        self.addCleanup(ws.close)
        doc = sessions_view.summary(ws.home, now=NOW, runner=Runner(manager_answer(SESSIONS)),
                                    manager="/usr/local/bin/switchboard", attribute=attribute)
        # alpha-web: 2 running, 3 ledger -> 2; beta-api: 1 running, 1 ledger -> 1; gamma-ended: no process -> 0;
        # no project: 1 running (codex scratch), 1 ledger -> 1.
        self.assertEqual(doc["active_hour"], 4)
        self.assertEqual(doc["ledger_hour"], 7)
        self.assertLessEqual(doc["active_hour"], sum(doc["processes"].values()))
        self.assertEqual(doc["degraded"], [])
        f = sessions_view.facets(doc)
        self.assertEqual((f["running"], f["activeHour"], f["ledgerHour"]), (4, 4, 7))

    def test_an_ended_session_alone_counts_nothing(self) -> None:
        ws = Workspace(SURVEY, ledger_projects=("project:gamma-ended",))
        self.addCleanup(ws.close)
        doc = sessions_view.summary(ws.home, now=NOW, runner=Runner(manager_answer(SESSIONS)),
                                    manager="/usr/local/bin/switchboard", attribute=attribute)
        self.assertEqual((doc["active_hour"], doc["ledger_hour"]), (0, 1))
        empty = sessions_view.summary(ws.home, now=NOW, runner=Runner(manager_answer([])),
                                      manager="/usr/local/bin/switchboard", attribute=attribute)
        self.assertEqual((empty["active_hour"], empty["ledger_hour"]), (0, 1))

    def test_each_session_carries_its_project_and_never_a_path(self) -> None:
        doc = self.run_summary(Runner(manager_answer(SESSIONS)))
        by_pid = {s["pid"]: s for s in doc["sessions"]}
        first = by_pid[101]
        self.assertEqual((first["agent"], first["project"], first["folder_name"], first["managed"],
                          first["launch_id"], first["account_group"], first["state"]),
                         ("claude-code", "project:alpha-web", "alpha-web", True, "launch-1", "team-a", "working"))
        self.assertEqual(first["last_turn_at"], "2026-11-14T17:30:00Z")
        self.assertEqual(by_pid[102]["project"], "project:alpha-web")
        self.assertEqual(by_pid[103]["state"], "quiet")
        self.assertIsNone(by_pid[104]["project"])
        self.assertNotIn("/srv/example", json.dumps(doc), "a full folder path reached the summary")

    def test_each_running_session_says_whether_it_is_active(self) -> None:
        """OBS-599: one rule for "working", carried on each row. A running session is active when its project
        holds a ledger turn of the last hour; with fewer turns than running sessions, the sessions written to
        most recently take them. The count is the rows marked, so the tile and the list agree."""
        ws = Workspace(SURVEY, ledger_projects=("project:alpha-web",))
        self.addCleanup(ws.close)
        rows = [row(101, "/srv/example/projects/alpha-web", last_write_at=STAMP - 600),
                row(102, "/srv/example/projects/alpha-web/docs", last_write_at=STAMP - 60, quiet=True),
                row(103, "/srv/example/projects/beta-api"),
                row(104, "/srv/example/scratch/notes", agent="codex")]
        doc = sessions_view.summary(ws.home, now=NOW, runner=Runner(manager_answer(rows)),
                                    manager="/usr/local/bin/switchboard", attribute=attribute)
        active = {s["pid"]: s["active"] for s in doc["sessions"]}
        self.assertEqual(active, {101: False, 102: True, 103: False, 104: False})
        self.assertEqual(doc["active_hour"], 1)
        self.assertEqual(doc["active_hour"], sum(1 for s in doc["sessions"] if s["active"]))
        bare = tempfile.TemporaryDirectory()
        self.addCleanup(bare.cleanup)
        unread = sessions_view.summary(pathlib.Path(bare.name), now=NOW, runner=Runner(manager_answer(rows)),
                                       manager="/usr/local/bin/switchboard", attribute=attribute)
        self.assertEqual({s["active"] for s in unread["sessions"]}, {None}, "an unread ledger is not 'no turn'")

    def test_mark_active_is_the_one_join(self) -> None:
        running = [{"pid": 1, "project": "project:a", "last_turn_at": "2026-11-14T17:00:00Z"},
                   {"pid": 2, "project": "project:a", "last_turn_at": "2026-11-14T17:30:00Z"},
                   {"pid": 3, "project": None, "last_turn_at": None},
                   {"pid": 4, "project": "project:b", "last_turn_at": None}]
        recent = [{"projectId": "project:a"}, {"projectId": None}, {"projectId": "project:gone"}]
        marked = sessions_view.mark_active(running, recent)
        self.assertEqual([(r["pid"], r["active"]) for r in marked], [(1, False), (2, True), (3, True), (4, False)])
        self.assertNotIn("active", running[0], "the rows passed in are not changed")
        self.assertEqual([r["active"] for r in sessions_view.mark_active(running, None)], [None] * 4)

    def test_manager_not_installed_is_none_never_zero(self) -> None:
        runner = Runner(manager_answer(SESSIONS))
        doc = self.run_summary(runner, manager=None)
        self.assertEqual(runner.calls, [], "nothing is run when no manager is installed")
        self.assertIsNone(doc["managed"])
        self.assertEqual(doc["sources"]["manager"]["state"], "not-installed")
        # The process survey stands in for the count of what runs.
        self.assertEqual(doc["processes"], {"claude-code": 3, "codex": 1})
        self.assertEqual(doc["sources"]["processes"]["from"], "machine-survey")
        self.assertEqual(doc["sources"]["processes"]["at"], "2026-11-14T17:25:00Z")
        self.assertEqual(doc["sessions"], [])
        # OBS-597: without the manager's rows no running session can be joined to a ledger turn, so the
        # facet is not read; the ledger's own count stays beside it under its own name.
        self.assertIsNone(doc["active_hour"])
        self.assertEqual(doc["ledger_hour"], 2)
        self.assertEqual([d["code"] for d in doc["degraded"]], ["not-installed", "needs-session-list"])

    def test_malformed_answer_is_named_and_falls_back(self) -> None:
        for out in ("not json", json.dumps({"ok": True}), json.dumps({"data": {"sessions": "x"}}), "[]"):
            with self.subTest(out=out[:20]):
                doc = self.run_summary(Runner(out))
                self.assertIsNone(doc["managed"])
                self.assertEqual(doc["sources"]["manager"]["state"], "malformed")
                self.assertIn("malformed", [d["code"] for d in doc["degraded"]])
                self.assertEqual(doc["sources"]["processes"]["from"], "machine-survey")

    def test_a_failed_or_slow_manager_is_named(self) -> None:
        import subprocess
        cases = ((Runner("", code=2), "failed"),
                 (Runner(raises=subprocess.TimeoutExpired(["switchboard"], 10)), "timeout"),
                 (Runner(raises=FileNotFoundError("gone")), "not-installed"),
                 (Runner(raises=PermissionError("denied")), "failed"))
        for runner, state in cases:
            with self.subTest(state=state):
                doc = self.run_summary(runner)
                self.assertIsNone(doc["managed"])
                self.assertEqual(doc["sources"]["manager"]["state"], state)
                self.assertIn(state, [d["code"] for d in doc["degraded"]])

    def test_a_bad_row_is_skipped_and_counted(self) -> None:
        rows = [*SESSIONS[:2], {"pid": "x"}, "nonsense"]
        doc = self.run_summary(Runner(manager_answer(rows)))
        self.assertEqual(len(doc["sessions"]), 2)
        self.assertIn("rows-skipped", [d["code"] for d in doc["degraded"]])

    def test_fewer_sessions_than_processes_is_reported_not_hidden(self) -> None:
        doc = self.run_summary(Runner(manager_answer(SESSIONS[:1])))
        codes = {d["code"]: d for d in doc["degraded"]}
        self.assertIn("fewer-than-processes", codes)
        self.assertIn("claude-code", codes["fewer-than-processes"]["reason"])
        self.assertEqual(doc["processes"], {"claude-code": 1}, "the manager's own count is kept, not replaced")

    def test_no_survey_no_store(self) -> None:
        bare = tempfile.TemporaryDirectory()
        self.addCleanup(bare.cleanup)
        doc = sessions_view.summary(pathlib.Path(bare.name), now=NOW, runner=Runner(), manager=None,
                                    attribute=attribute)
        self.assertIsNone(doc["managed"])
        self.assertIsNone(doc["active_hour"])
        self.assertIsNone(doc["ledger_hour"])
        self.assertEqual(doc["processes"], {})
        self.assertIsNone(doc["sources"]["processes"]["from"])
        codes = [d["code"] for d in doc["degraded"]]
        self.assertIn("not-surveyed", codes)
        self.assertIn("no-store", codes)

    def test_attribution_that_fails_degrades_and_keeps_the_session(self) -> None:
        def broken(folder):
            raise OSError("registry unreadable")
        doc = sessions_view.summary(self.ws.home, now=NOW, runner=Runner(manager_answer(SESSIONS)),
                                    manager="/usr/local/bin/switchboard", attribute=broken)
        self.assertEqual(len(doc["sessions"]), 4)
        self.assertTrue(all(s["project"] is None for s in doc["sessions"]))
        self.assertIn("attribution", [d["source"] for d in doc["degraded"]])


class File(unittest.TestCase):
    def test_write_then_load_and_a_missing_file_says_so(self) -> None:
        ws = Workspace(SURVEY)
        self.addCleanup(ws.close)
        missing = sessions_view.load(ws.home)
        self.assertIsNone(missing["managed"])
        self.assertEqual([d["code"] for d in missing["degraded"]], ["not-collected"])
        doc = sessions_view.summary(ws.home, now=NOW, runner=Runner(manager_answer(SESSIONS)),
                                    manager="/usr/local/bin/switchboard", attribute=attribute)
        sessions_view.write(doc, ws.home / "store" / "raw" / sessions_view.FILE)  # paths-check: allow: a workspace home the caller names, not this engine's
        self.assertEqual(sessions_view.load(ws.home), doc)
        (ws.home / "store" / "raw" / sessions_view.FILE).write_text("{broken", encoding="utf-8")  # paths-check: allow: a workspace home the caller names, not this engine's
        self.assertEqual([d["code"] for d in sessions_view.load(ws.home)["degraded"]], ["unreadable"])

    def test_find_manager_on_path_then_in_the_user_bin(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d)
            self.assertEqual(sessions_view.find_manager(which=lambda name: "/opt/bin/switchboard", home=home),
                             "/opt/bin/switchboard")
            self.assertIsNone(sessions_view.find_manager(which=lambda name: None, home=home))
            local = home / ".local" / "bin" / "switchboard"
            local.parent.mkdir(parents=True)
            local.write_text("#!/bin/sh\n", encoding="utf-8")
            local.chmod(0o755)
            self.assertEqual(sessions_view.find_manager(which=lambda name: None, home=home), str(local))


class OneCount(unittest.TestCase):
    """Home and Agents read one function, so the two pages can never disagree."""

    def setUp(self) -> None:
        self.ws = Workspace(SURVEY, ledger_sessions=2)
        self.addCleanup(self.ws.close)
        self.doc = sessions_view.summary(self.ws.home, now=NOW, runner=Runner(manager_answer(SESSIONS)),
                                         manager="/usr/local/bin/switchboard", attribute=attribute)

    def test_facets(self) -> None:
        f = sessions_view.facets(self.doc)
        self.assertEqual((f["running"], f["managed"], f["activeHour"]), (4, 1, 2))
        self.assertEqual(f["processesFrom"], "manager")
        empty = sessions_view.facets(None)
        self.assertEqual((empty["running"], empty["managed"], empty["activeHour"], empty["ledgerHour"]),
                         (None, None, None, None))

    def test_home_and_agents_show_the_same_numbers(self) -> None:
        import agents_page
        import home_page
        payload = {"agents": {"counters": {"sessionsActive": 9, "stalled": 0}}, "sessions": self.doc}
        t = Translator("en")
        tile = next(x for x in home_page.tiles(payload, t) if x["key"] == "agents")
        home = tile["value"] + " " + tile["sub"]
        page = agents_page.agents_html(payload, t)
        for text in ("4 sessions running", "1 managed", "2 active in the last hour"):
            with self.subTest(text=text):
                self.assertIn(text, _plain(home))
        for label, value in (("Sessions running", "4"), ("Managed", "1"), ("Active in the last hour", "2")):
            with self.subTest(label=label):
                self.assertRegex(_plain(page), rf"{label}\s*{value}\b")
        self.assertNotIn("9", _plain(tile["value"]), "the ledger's day list is not the running count")

    def test_a_manager_that_is_not_installed_reads_not_read_never_zero(self) -> None:
        import agents_page
        import home_page
        doc = sessions_view.summary(self.ws.home, now=NOW, runner=Runner(), manager=None, attribute=attribute)
        payload = {"agents": {"counters": {"sessionsActive": 0, "stalled": 0}}, "sessions": doc}
        t = Translator("en")
        tile = next(x for x in home_page.tiles(payload, t) if x["key"] == "agents")
        self.assertNotIn("0 managed", _plain(tile["sub"]))
        self.assertIn("managed: not read", _plain(tile["sub"]))
        page = _plain(agents_page.agents_html(payload, t))
        self.assertNotRegex(page, r"Managed\s*0\b")
        self.assertIn("the account manager is not installed", page)
        # OBS-597: the ledger's count is named as what it is, never shown as the running sessions' activity.
        self.assertNotIn("2 active in the last hour", _plain(tile["sub"]))
        self.assertIn("activity not read", _plain(tile["sub"]))
        self.assertNotRegex(page, r"Active in the last hour\s*2\b")
        self.assertIn("2 sessions recorded a turn; which of them still run needs the account manager", page)


class Finding(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace(SURVEY)
        self.addCleanup(self.ws.close)
        import coding_session_findings
        self.rule = coding_session_findings

    def test_unmanaged_sessions_are_one_info_finding(self) -> None:
        doc = sessions_view.summary(self.ws.home, now=NOW, runner=Runner(manager_answer(SESSIONS)),
                                    manager="/usr/local/bin/switchboard", attribute=attribute)
        rows = self.rule.findings(doc)
        self.assertEqual(len(rows), 1)
        f = rows[0]
        self.assertEqual((f["type"], f["severity"]), ("session.unmanaged", "info"))
        self.assertEqual(f["title"], "3 coding sessions run outside a lifecycle owner")
        self.assertIn("account manager", f["action"])
        self.assertIn("switchboard launch", f["action"])
        self.assertIn("embedded agent", f["action"])
        self.assertIn("claude-code 2", f["detail"])
        self.assertNotIn("/srv/example", json.dumps(f))

    def test_all_managed_is_silent(self) -> None:
        managed = [dict(s, managed=True) for s in SESSIONS]
        doc = sessions_view.summary(self.ws.home, now=NOW, runner=Runner(manager_answer(managed)),
                                    manager="/usr/local/bin/switchboard", attribute=attribute)
        self.assertEqual(self.rule.findings(doc), [])

    def test_no_manager_says_what_it_adds_and_how_to_set_it_up(self) -> None:
        doc = sessions_view.summary(self.ws.home, now=NOW, runner=Runner(), manager=None, attribute=attribute)
        rows = self.rule.findings(doc)
        self.assertEqual(len(rows), 1)
        f = rows[0]
        self.assertEqual((f["type"], f["severity"]), ("session.unmanaged", "info"))
        self.assertEqual(f["title"], "4 coding sessions run without an account manager")
        for word in ("limits", "hand-over", "passioncode"):
            self.assertIn(word, f["action"] + f["detail"])

    def test_a_manager_that_failed_raises_no_finding(self) -> None:
        doc = sessions_view.summary(self.ws.home, now=NOW, runner=Runner("", code=1),
                                    manager="/usr/local/bin/switchboard", attribute=attribute)
        self.assertEqual(self.rule.findings(doc), [], "a failed read is degraded, not a fact about sessions")
        self.assertEqual(self.rule.findings(None), [])

    def test_the_type_has_a_label_and_the_title_both_languages(self) -> None:
        import finding_types
        import i18n
        self.assertIn("session.unmanaged", finding_types.LABELS)
        for msgid in ("{n} coding sessions run outside a lifecycle owner",
                      "{n} coding sessions run without an account manager"):
            self.assertIn(msgid, i18n.catalog("ru"))
            self.assertIn(msgid, i18n.catalog("en"))


def _plain(markup: str) -> str:
    import html
    import re
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())



class ByProjectTest(unittest.TestCase):
    """Sessions per project (docs/design/control-plane/sessions-per-project.md): the live rows, the ledger's
    sessions with the step their workflow last wrote, and the open workflows, joined by id."""

    DOC = {"sessions": [
        {"agent": "claude-code", "project": "project:alpha", "folder_name": "alpha", "pid": 1, "managed": True,
         "launch_id": "L1", "account_group": "g1", "last_turn_at": "2026-11-14T17:20:00Z", "quiet": True,
         "state": "quiet"},
        {"agent": "codex", "project": "project:alpha", "folder_name": "alpha", "pid": 2, "managed": False,
         "launch_id": None, "account_group": None, "last_turn_at": "2026-11-14T17:10:00Z", "quiet": False,
         "state": "working"},
        {"agent": "claude-code", "project": None, "folder_name": "scratch", "pid": 3, "managed": False,
         "launch_id": None, "account_group": None, "last_turn_at": None, "quiet": False, "state": "working"}]}
    FLOWS = [
        {"workflowId": "wf_open", "projectId": "project:alpha", "status": "open", "goal": "ship it",
         "step": {"stepId": "build", "status": "in_progress", "revision": 3, "at": "2026-11-14T17:25:00Z"},
         "stalled": False, "lease": {"holder": "agent:claude-code", "leaseRef": "lease_x"}},
        {"workflowId": "wf_done", "projectId": "project:alpha", "status": "closed", "goal": "old",
         "step": {"stepId": "final", "status": "done", "revision": 9, "at": "2026-11-14T10:00:00Z"}}]
    LEDGER = [
        {"sessionId": "s-new", "projectId": "project:alpha", "lastTurnAt": "2026-11-14T17:25:00Z", "turns": 4,
         "workflowId": "wf_open", "active": True},
        {"sessionId": "s-old", "projectId": "project:alpha", "lastTurnAt": "2026-11-14T10:00:00Z", "turns": 2,
         "workflowId": "wf_done", "active": False},
        {"sessionId": "s-free", "projectId": "project:beta", "lastTurnAt": "2026-11-14T12:00:00Z", "turns": 1,
         "workflowId": None, "active": False},
        {"sessionId": "s-none", "projectId": None, "lastTurnAt": "2026-11-14T12:00:00Z", "turns": 1,
         "workflowId": None, "active": False}]

    def test_joins_live_recent_and_workflows_per_project(self):
        out = sessions_view.by_project(self.DOC, self.LEDGER, self.FLOWS)
        self.assertEqual(sorted(out), ["project:alpha", "project:beta"])   # no project is invented
        a = out["project:alpha"]
        # OBS-599: working is the one rule (`mark_active`), never the manager's `state`: alpha's one turn of the
        # last hour goes to its session written to most recently, the one the manager calls quiet.
        self.assertEqual([r["agent"] for r in a["live"]], ["claude-code", "codex"])   # working first
        self.assertEqual([r["active"] for r in a["live"]], [True, False])
        self.assertEqual(a["counts"], {"live": 2, "working": 1, "recent": 2, "workflows": 1})
        self.assertEqual([r["sessionId"] for r in a["recent"]], ["s-new", "s-old"])
        self.assertEqual(a["recent"][0]["workflow"]["stepId"], "build")      # what it last did
        self.assertEqual(a["recent"][1]["workflow"]["stepStatus"], "done")   # a closed workflow still names its step
        self.assertEqual([w["workflowId"] for w in a["workflows"]], ["wf_open"])   # only open ones listed
        self.assertEqual(a["workflows"][0]["executor"], "agent:claude-code")
        self.assertIsNone(out["project:beta"]["recent"][0]["workflow"])
        self.assertNotIn("pid", a["live"][0])
        self.assertNotIn("leaseRef", json.dumps(out))                         # no lease handle leaves

    def test_working_is_the_one_rule_and_never_the_managers_state(self):
        """OBS-599: a project's `working` count and its live rows read `active`, the rule the Agents page and the
        session count use; a row the manager calls working without a turn in the last hour is not working, and a
        ledger that was not read leaves the state unknown, never quiet."""
        doc = {"sessions": [dict(r, active=False) for r in self.DOC["sessions"]]}
        doc["sessions"][0]["active"] = True                                  # quiet by the manager, a turn by the rule
        a = sessions_view.by_project(doc, self.LEDGER, self.FLOWS)["project:alpha"]
        self.assertEqual([(r["agent"], r["active"]) for r in a["live"]], [("claude-code", True), ("codex", False)])
        self.assertEqual(a["counts"]["working"], 1)
        unread = sessions_view.by_project(self.DOC, None, None)["project:alpha"]
        self.assertEqual([r["active"] for r in unread["live"]], [None, None])
        self.assertEqual(unread["counts"]["working"], 0)

    def test_the_projects_page_reads_the_one_rule(self):
        """OBS-599: the Projects page's "Running now" list colours a row by `active`, never by `state`."""
        src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
        live = src[src.index("const live = list(ps.live"):src.index("const recent = list(ps.recent")]
        self.assertIn("s.active === true", live)
        self.assertNotIn("s.state", live)

    def test_the_tool_names_the_rule(self):
        src = (ROOT / "mcp/server.py").read_text(encoding="utf-8")
        doc = src[src.index("def observatory_sessions("):]
        doc = doc[doc.index('"""'):doc.index('"""', doc.index('"""') + 3)]
        self.assertIn("`active`", doc)

    def test_unknown_workflow_keeps_its_id_only(self):
        out = sessions_view.by_project({}, [{"sessionId": "s", "projectId": "project:a", "lastTurnAt": "x",
                                             "turns": 1, "workflowId": "wf_gone", "active": False}], [])
        self.assertEqual(out["project:a"]["recent"][0]["workflow"], {"workflowId": "wf_gone"})

    def test_empty_and_malformed_sources(self):
        self.assertEqual(sessions_view.by_project(None, None, None), {})
        self.assertEqual(sessions_view.by_project({"sessions": ["x", None]}, ["y"], ["z", {"x": 1}]), {})

    def test_bounded(self):
        doc = {"sessions": [dict(self.DOC["sessions"][1], pid=i) for i in range(sessions_view.MAX_PROJECT_ROWS + 5)]}
        a = sessions_view.by_project(doc, [], [])["project:alpha"]
        self.assertEqual(len(a["live"]), sessions_view.MAX_PROJECT_ROWS)
        self.assertEqual(a["counts"]["live"], sessions_view.MAX_PROJECT_ROWS + 5)

    def test_projects_without_store_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = sessions_view.projects(pathlib.Path(tmp), doc=self.DOC, now=NOW)
        self.assertEqual(out["schema"], "project-sessions/1")
        self.assertIn("no-store", [d["code"] for d in out["degraded"]])
        self.assertEqual(out["projects"]["project:alpha"]["counts"]["live"], 2)

    def test_projects_not_readable_reads_no_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = sessions_view.projects(pathlib.Path(tmp), doc=self.DOC, now=NOW, read_store=False)
        self.assertIn("not-readable", [d["code"] for d in out["degraded"]])
        self.assertEqual(out["projects"]["project:alpha"]["recent"], [])


if __name__ == "__main__":
    unittest.main()
