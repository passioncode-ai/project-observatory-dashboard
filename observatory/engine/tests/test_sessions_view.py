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
    def __init__(self, survey: dict | None = None, ledger_sessions: int = 0) -> None:
        self.dir = tempfile.TemporaryDirectory(prefix="observatory-sessions-")
        self.home = pathlib.Path(self.dir.name)
        raw = self.home / "store" / "raw"
        raw.mkdir(parents=True)
        if survey is not None:
            (raw / "machine.json").write_text(json.dumps(survey), encoding="utf-8")
        db = self.home / "store" / "observatory.db"
        conn = sqlite3.connect(db)
        conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
        for i in range(ledger_sessions):
            L.append(conn, owner="agent:claude-code", kind="session", statement="turn",
                     project_id="project:alpha-web", session_id=f"0f8fad5b-d9cb-469f-a165-7086772895{i:02d}")
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

    def test_manager_not_installed_is_none_never_zero(self) -> None:
        runner = Runner(manager_answer(SESSIONS))
        doc = self.run_summary(runner, manager=None)
        self.assertEqual(runner.calls, [], "nothing is run when no manager is installed")
        self.assertIsNone(doc["managed"])
        self.assertEqual(doc["sources"]["manager"]["state"], "not-installed")
        self.assertEqual([d["code"] for d in doc["degraded"]], ["not-installed"])
        # The process survey stands in for the count of what runs.
        self.assertEqual(doc["processes"], {"claude-code": 3, "codex": 1})
        self.assertEqual(doc["sources"]["processes"]["from"], "machine-survey")
        self.assertEqual(doc["sources"]["processes"]["at"], "2026-11-14T17:25:00Z")
        self.assertEqual(doc["sessions"], [])
        self.assertEqual(doc["active_hour"], 2)

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
        sessions_view.write(doc, ws.home / "store" / "raw" / sessions_view.FILE)
        self.assertEqual(sessions_view.load(ws.home), doc)
        (ws.home / "store" / "raw" / sessions_view.FILE).write_text("{broken", encoding="utf-8")
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
        self.assertEqual((empty["running"], empty["managed"], empty["activeHour"]), (None, None, None))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
