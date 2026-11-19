#!/usr/bin/env python3
"""A coding session handed to Fabric (fabric-session-links/1): the links, the runner session id behind them, and the
surfaces that offer them (the Agents page, a project's panel, the key server's terminal).

Every source is a fixture: Claude Code's per-process session files are written into a temporary home, the account
manager's answer is handed to an injected runner, and the terminal opener is replaced. Nothing opens a window, reads
the user's real home or runs the manager.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.append(str(ROOT / "dashboard"))

import session_links as S                                                          # noqa: E402
import sessions_view                                                               # noqa: E402
import agents_page                                                                 # noqa: E402
from i18n import Translator                                                        # noqa: E402

with tempfile.TemporaryDirectory(prefix="observatory-import-session-links-") as _home:
    with patch.dict(os.environ, {"OBSERVATORY_HOME": str(pathlib.Path(_home).resolve())}):
        import keyserver                                                           # noqa: E402

SID = "3f2a9c1e-0b7d-4e55-9a10-6c2d8e4f7a01"
OTHER = "8d1b4e2f-5a6c-4b3d-9e7f-0a1b2c3d4e5f"
STARTED = 1_790_000_000


def write_session(folder: pathlib.Path, pid: int, sid: str, *, started_s: float = STARTED, cwd: str = "/srv/x"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "cwd": cwd,
                                                     "startedAt": int(started_s * 1000)}), encoding="utf-8")


class Links(unittest.TestCase):
    def test_the_three_links_carry_ids_and_the_app(self):
        self.assertEqual(S.open_session(SID), f"fabric://session/open?session={SID}&from=project-observatory")
        self.assertEqual(S.continue_session(SID), f"fabric://session/continue?session={SID}&from=project-observatory")
        self.assertEqual(S.new_session("project:alpha-web"),
                         "fabric://session/new?project=alpha-web&from=project-observatory")

    def test_fabric_s_own_id_wins_over_the_slug(self):
        uuid = "5f8f0a54-0000-4000-8000-000000000001"
        self.assertEqual(S.new_session("project:alpha-web", f"fabric:{uuid}"),
                         f"fabric://session/new?project={uuid}&from=project-observatory")
        self.assertEqual(S.project_ref("project:alpha-web", "not-an-alias"), "alpha-web")

    def test_nothing_that_is_not_an_id_reaches_a_link(self):
        for bad in (None, "", "short", "a b c d e f g h", "../../etc/passwd", "x" * 200, 42, "id&from=evil"):
            self.assertIsNone(S.open_session(bad), bad)
            self.assertIsNone(S.links(bad), bad)
        self.assertIsNone(S.new_session(None))
        self.assertIsNone(S.new_session("repository:example-org/alpha"))
        self.assertIsNone(S.new_session("project:has space"))

    def test_links_are_both_or_none(self):
        self.assertEqual(S.links(SID), {"open": S.open_session(SID), "continue": S.continue_session(SID)})


class RunnerSessionId(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="observatory-session-links-")
        self.home = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_default_home_and_every_manager_home_are_read(self):
        base = self.home / S.MANAGER_DATA["darwin"][0]
        for d in (self.home / ".claude" / "sessions", base / "runtimes" / "claude-team" / "sessions",
                  base / "homes" / "h1" / "sessions"):
            d.mkdir(parents=True)
        (base / "homes" / "no-sessions").mkdir(parents=True)
        dirs = S.session_dirs(self.home, "darwin")
        self.assertEqual([d.relative_to(self.home).parts[-2] for d in dirs], [".claude", "claude-team", "h1"])
        self.assertEqual(S.session_dirs(self.home / "absent", "darwin"), [])

    def test_a_file_is_believed_only_for_its_own_process(self):
        d = self.home / "sessions"
        write_session(d, 4242, SID)
        self.assertEqual(S.runner_session(4242, STARTED + 3, [d])["sessionId"], SID)
        # The pid was reused by a process that started an hour later: the file is another session's.
        self.assertIsNone(S.runner_session(4242, STARTED + 3600, [d]))
        self.assertIsNone(S.runner_session(4243, STARTED, [d]))
        self.assertIsNone(S.runner_session(True, STARTED, [d]))
        (d / "77.json").write_text(json.dumps({"pid": 78, "sessionId": SID, "startedAt": STARTED * 1000}))
        self.assertIsNone(S.runner_session(77, STARTED, [d]), "a file that names another pid")
        (d / "79.json").write_text("{not json")
        self.assertIsNone(S.runner_session(79, STARTED, [d]))

    def test_the_resolver_reads_claude_code_only(self):
        d = self.home / "sessions"
        write_session(d, 4242, SID)
        resolve = S.resolver([d])
        self.assertEqual(resolve(4242, STARTED, "claude-code"), SID)
        self.assertIsNone(resolve(4242, STARTED, "codex"))

    def test_the_terminal_folder_is_a_running_session_s_only(self):
        d = self.home / "sessions"
        folder = self.home / "work"
        folder.mkdir()
        write_session(d, os.getpid(), SID, cwd=str(folder))
        self.assertEqual(S.folder_of(SID, [d]), str(folder))
        self.assertIsNone(S.folder_of(OTHER, [d]), "an unknown session opens nothing")
        self.assertIsNone(S.folder_of("../etc", [d]))
        dead = 2 ** 22 + 17                       # above any pid the OS hands out here
        write_session(d, dead, OTHER, cwd=str(folder))
        self.assertIsNone(S.folder_of(OTHER, [d]), "an ended session opens nothing")


def manager_answer(rows):
    return json.dumps({"ok": True, "data": {"observed_at": STARTED + 60, "sessions": rows}})


class Rows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="observatory-session-rows-")
        self.home = pathlib.Path(self.tmp.name)
        self.dirs = self.home / "sessions"
        write_session(self.dirs, 101, SID)

    def tearDown(self):
        self.tmp.cleanup()

    def summary(self, rows):
        return sessions_view.summary(self.home, runner=lambda argv, timeout: (0, manager_answer(rows)),
                                     manager="/bin/switchboard", attribute=lambda f: "project:alpha-web",
                                     session_dirs=[self.dirs])

    def test_each_running_session_carries_its_id_and_links_or_none(self):
        doc = self.summary([{"agent": "claude-code", "folder": "/srv/a/alpha-web", "pid": 101, "started_at": STARTED,
                             "last_write_at": STARTED + 30},
                            {"agent": "claude-code", "folder": "/srv/a/alpha-web", "pid": 102, "started_at": STARTED},
                            {"agent": "codex", "folder": "/srv/a/alpha-web", "pid": 103, "session_id": OTHER}])
        by_pid = {r["pid"]: r for r in doc["sessions"]}
        self.assertEqual(by_pid[101]["session_id"], SID)
        self.assertEqual(by_pid[101]["links"]["continue"], S.continue_session(SID))
        self.assertIsNone(by_pid[102]["session_id"])
        self.assertIsNone(by_pid[102]["links"])
        self.assertEqual(by_pid[103]["session_id"], OTHER, "the manager's own id is kept")
        self.assertNotIn("/srv", json.dumps(doc), "no folder path is written")

    def test_a_project_s_live_and_recent_sessions_carry_their_links(self):
        doc = self.summary([{"agent": "claude-code", "folder": "/srv/a/alpha-web", "pid": 101, "started_at": STARTED}])
        ledger = [{"sessionId": OTHER, "projectId": "project:alpha-web", "lastTurnAt": "2026-11-19T20:00:00Z",
                   "turns": 3, "active": True}]
        block = sessions_view.by_project(doc, ledger, [])["project:alpha-web"]
        self.assertEqual(block["live"][0]["links"]["open"], S.open_session(SID))
        self.assertEqual(block["recent"][0]["links"]["continue"], S.continue_session(OTHER))


class AgentsPage(unittest.TestCase):
    def test_a_session_with_an_id_offers_its_three_acts_and_the_terminal_is_built_disabled(self):
        html = agents_page.session_acts({"session_id": SID, "links": S.links(SID)}, Translator("ru"))
        self.assertIn(f'href="fabric://session/open?session={SID}&amp;from=project-observatory"', html)
        self.assertIn(f'href="fabric://session/continue?session={SID}&amp;from=project-observatory"', html)
        self.assertIn(f'data-session-terminal="{SID}" disabled', html)
        self.assertIn("Открыть в Fabric", html)

    def test_a_session_without_an_id_offers_none(self):
        html = agents_page.session_acts({"session_id": None, "links": None}, Translator("en"))
        self.assertNotIn("fabric://", html)
        self.assertNotIn("data-session-terminal", html)

    def test_the_project_panel_offers_a_new_session_and_the_page_script_posts_the_terminal(self):
        src = (ROOT / "dashboard" / "build_dashboard.py").read_text(encoding="utf-8")
        self.assertIn('PAYLOAD["session_new"]', src)
        self.assertIn('call("session-terminal", {sessionId: sid})', src)
        self.assertIn("sessionTerminals();", src)


class Terminal(unittest.TestCase):
    def setUp(self):
        self.opened = []
        self.patches = [patch.object(keyserver, "TERMINAL_OPENER", self.opened.append),
                        patch.object(keyserver, "audit", lambda *a, **k: None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_the_terminal_opens_in_the_running_session_s_folder_and_answers_no_path(self):
        with patch.object(S, "folder_of", lambda sid: "/srv/a/alpha-web" if sid == SID else None):
            answer = keyserver.ACTIONS["session-terminal"]({"sessionId": SID})
        self.assertEqual(self.opened, ["/srv/a/alpha-web"])
        self.assertEqual(answer, {"opened": True, "sessionId": SID})

    def test_refusals_are_codes(self):
        with patch.object(S, "folder_of", lambda sid: None):
            with self.assertRaises(LookupError) as unknown:
                keyserver.act_session_terminal({"sessionId": SID})
            with self.assertRaises(ValueError) as bad:
                keyserver.act_session_terminal({"sessionId": "../x"})
        self.assertEqual(keyserver.refusal(unknown.exception), (404, "session-not-running"))
        self.assertEqual(keyserver.refusal(bad.exception), (400, "invalid-session-id"))
        self.assertEqual(self.opened, [])


if __name__ == "__main__":
    unittest.main()
