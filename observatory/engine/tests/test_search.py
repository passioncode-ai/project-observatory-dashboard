#!/usr/bin/env python3
"""`observatory_search(kinds=[...])`: a kind filter applied BEFORE the candidate window.

Measured on a real estate (2026-10-12): `observatory_search("retrospective lesson release
waves")` across projects answered `count 0, abstain: true` with the lexical window full —
the companion's per-session turn records matched the same words and took all 300 places
before a lesson could rank. A filter applied to the page could only remove what had
already crowded the answer out; the filter belongs in the candidate query, beside scope.

* With `kinds=["lesson"]` and no project, a lesson planted among 400 session rows that
  match the same words is found, and the window is not full.
* Every hit names its `kind`, filtered or not.
* An empty `kinds` list asks for nothing and gets nothing; a binding's hidden kinds stay
  hidden whatever it asks for.

Over the real MCP server module and a store in a temporary workspace (the fixture of
tests/test_memory_access.py).
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_memory_access import ALPHA, BETA, Workspace, tearDownModule  # noqa: E402,F401

QUERY = "retrospective lesson release waves"
SESSIONS = 400


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()
        from store import db as sdb
        from store import ledger as L
        self.L, self.conn = L, sdb.connect()
        # Turn records that carry every word of the question, several times over, so
        # they outrank the lesson and fill the window — the measured shape.
        for i in range(SESSIONS):
            self.L.append(self.conn, owner="agent:companion", kind="session",
                          project_id=(ALPHA, BETA)[i % 2], confidence=0.5,
                          statement=f"session {i}: retrospective lesson release waves; "
                                    f"retrospective release waves lesson again")
        self.lesson = self.L.append(
            self.conn, owner="agent:task-pipeline", kind="lesson", function="experiential",
            scope="global", confidence=0.5,
            statement="lesson: retro-before-next-wave — a release retrospective runs before "
                      "the next waves are cut",
            why="a release closes; check: waves gate retro-present",
            body={"schema": "lesson/1", "classKey": "retro-before-next-wave",
                  "title": "a release retrospective runs before the next waves are cut",
                  "trigger": "a release closes", "check": "waves gate retro-present",
                  "nouns": ["retrospective", "release", "waves"],
                  "origin": {"projectId": "p-0123456789ab", "commit": "commit:0123456789ab"}})
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()


class KindsBeforeTheWindow(Base):
    def test_unfiltered_the_window_is_full_of_sessions(self) -> None:
        out = self.ws.srv.observatory_search(query=QUERY)
        self.assertIn("window", [d.get("source") for d in out["degraded"]], out["degraded"])

    def test_a_lesson_among_400_session_rows_is_found_by_kind(self) -> None:
        out = self.ws.srv.observatory_search(query=QUERY, kinds=["lesson"])
        self.assertFalse(out["abstain"], out)
        self.assertEqual([h["memoryId"] for h in out["results"]], [self.lesson["memoryId"]])
        self.assertEqual(out["results"][0]["kind"], "lesson")
        self.assertEqual(out["results"][0]["lesson"]["classKey"], "retro-before-next-wave")
        self.assertNotIn("window", [d.get("source") for d in out["degraded"]])
        self.assertEqual(out["kinds"], ["lesson"])

    def test_every_hit_names_its_kind(self) -> None:
        out = self.ws.srv.observatory_search(query=QUERY, limit=50)
        self.assertTrue(out["results"])
        self.assertEqual({h["kind"] for h in out["results"]} - {"session", "lesson"}, set())
        mixed = self.ws.srv.observatory_search(query=QUERY, kinds=["lesson", "session"], limit=50)
        self.assertTrue(all(h["kind"] in ("lesson", "session") for h in mixed["results"]))

    def test_an_empty_list_asks_for_nothing(self) -> None:
        out = self.ws.srv.observatory_search(query=QUERY, kinds=[])
        self.assertEqual((out["count"], out["abstain"]), (0, True))

    def test_recall_takes_the_same_filter_and_counts_its_scope(self) -> None:
        out = self.ws.srv.observatory_recall(kinds=["lesson"], limit=5)
        self.assertEqual(out["total"], 1)
        self.assertEqual([r["kind"] for r in out["records"]], ["lesson"])
        self.assertEqual(self.ws.srv.observatory_recall(kinds=["session"], limit=1)["total"],
                         SESSIONS)

    def test_a_binding_cannot_ask_its_way_into_hidden_kinds(self) -> None:
        self.ws.start(ALPHA)
        self.ws.bind("agent:alpha-bot", [ALPHA], bearer="tok-alpha",
                     scopes=("memory.read", "memory.search"))
        with self.ws.http("tok-alpha"):
            out = self.ws.srv.observatory_search(query="work alpha", project_id=ALPHA,
                                                 kinds=["checkpoint"])
        self.assertEqual(out.get("results"), [], out)


if __name__ == "__main__":
    unittest.main()
