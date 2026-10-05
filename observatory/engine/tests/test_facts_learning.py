#!/usr/bin/env python3
"""Facts that expire, and lessons that cite their evidence and wait for a second witness
(PB-137 N-014).

* A fact (`function: semantic`) must say until when it holds; an expired fact is not current
  in recall or search, and is still there as history.
* `observatory_recall` reads current, expired or all records, and contested records still come
  back beside the supported ones.
* `observatory_learn` writes a lesson only from a resolvable failure/fix pair the caller can
  read, cites both revisions, and the lesson stays `proposed` with confidence below 1.
* Nobody promotes their own proposal: not by appending it as `observed`, not by corroborating
  it themselves, not by claiming confidence. The operator's review or an independent witness
  does.

Over the real MCP server module and a store in a temporary workspace (the fixture of
tests/test_memory_access.py).
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_memory_access import ALPHA, BETA, Workspace, tearDownModule  # noqa: E402,F401

OWNER = "agent:alpha-bot"


def at(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()
        from store import db as sdb
        from store import ledger as L
        self.L = L
        self.conn = sdb.connect()

    def tearDown(self) -> None:
        self.conn.close()

    def fact(self, statement: str, **kw) -> dict:
        out = self.L.append(self.conn, owner=OWNER, statement=statement, project_id=ALPHA,
                            function="semantic", confidence=0.5, **kw)
        self.conn.commit()
        return out


class Facts(Base):
    def test_a_fact_must_say_until_when_it_holds(self) -> None:
        with self.assertRaises(self.L.LedgerError) as ctx:
            self.fact("the exporter writes quarterly files")
        self.assertIn("valid_to", str(ctx.exception))
        with self.assertRaises(self.L.LedgerError):
            self.fact("the exporter writes quarterly files", valid_to="next spring")
        for unreadable in ("20991231T000000Z", "2099-12-31T00:00:00+0200", "2099-W01-1T00:00:00Z",
                           "2099-12-31T00Z", "2099-12-31"):
            with self.assertRaises(self.L.LedgerError, msg=unreadable):
                self.fact("the exporter writes quarterly files", valid_to=unreadable)
        self.assertIn("memoryId", self.fact("the exporter writes quarterly files", valid_to=at(30)))
        self.assertIn("memoryId", self.fact("the exporter writes weekly files",
                                            valid_to="2099-12-31T00:00:00+02:00"))
        # An episode needs no end.
        self.assertTrue(self.L.append(self.conn, owner=OWNER, statement="ran the exporter",
                                      project_id=ALPHA, confidence=0.5)["memoryId"])

    def test_an_expired_fact_is_history_not_current(self) -> None:
        old = self.fact("the exporter writes monthly files", valid_from=at(-60), valid_to=at(-1))
        new = self.fact("the exporter writes quarterly files", valid_to=at(30))
        later = self.fact("the exporter will write weekly files", valid_from=at(10), valid_to=at(40))
        ids = lambda out: [r["memory_id"] for r in out["records"]]                   # noqa: E731
        current = self.ws.srv.observatory_recall(projectId=ALPHA)
        self.assertEqual(ids(current), [new["memoryId"]])
        self.assertEqual(current["total"], 1)
        expired = self.ws.srv.observatory_recall(projectId=ALPHA, validity="expired")
        self.assertEqual(ids(expired), [old["memoryId"]])
        everything = self.ws.srv.observatory_recall(projectId=ALPHA, validity="all")
        self.assertEqual(sorted(ids(everything)),
                         sorted([old["memoryId"], new["memoryId"], later["memoryId"]]))
        found = self.ws.srv.observatory_search(query="exporter files", project_id=ALPHA)
        self.assertEqual([r["memoryId"] for r in found["results"]], [new["memoryId"]])

    def test_a_contested_fact_comes_back_beside_the_supported_one(self) -> None:
        a = self.fact("the exporter writes quarterly files", valid_to=at(30))
        b = self.fact("the exporter writes yearly files", valid_to=at(30))
        for mid, path in ((a["memoryId"], ("observed", "supported")),
                          (b["memoryId"], ("observed", "supported", "contested"))):
            rev = 1
            for state in path:
                rev = self.L.transition(self.conn, mid, to_state=state, owner="operator",
                                        expected_revision=rev)["revision"]
        self.conn.commit()
        out = self.ws.srv.observatory_recall(projectId=ALPHA)
        self.assertEqual(sorted(r["memory_id"] for r in out["records"]),
                         sorted([a["memoryId"], b["memoryId"]]))
        self.assertEqual(out["contested"], [b["memoryId"]])


class LegacyRows(Base):
    def test_a_row_written_before_the_rule_can_still_be_promoted(self) -> None:
        """L4: transition carries the prior validity; it must not re-judge it."""
        out = self.fact("a legacy fact", valid_to=at(30))
        self.conn.execute("UPDATE ledger SET valid_to = '2099-12-31' WHERE memory_id = ?",
                          (out["memoryId"],))
        self.conn.commit()
        moved = self.L.transition(self.conn, out["memoryId"], to_state="rejected",
                                  owner="operator", expected_revision=1)
        self.assertEqual(moved["state"], "rejected")


class Learning(Base):
    def pair(self, project: str = ALPHA) -> tuple[dict, dict]:
        fail = self.L.append(self.conn, owner=OWNER, statement="the export timed out on 40k rows",
                             project_id=project, confidence=0.5)
        fix = self.L.append(self.conn, owner=OWNER, statement="streaming the export fixed the timeout",
                            project_id=project, confidence=0.5)
        self.conn.commit()
        return fail, fix

    def test_a_lesson_cites_its_pair_and_stays_a_proposal(self) -> None:
        fail, fix = self.pair()
        out = self.ws.srv.observatory_learn(owner=OWNER, statement="stream large exports",
                                            failureId=fail["memoryId"], fixId=fix["memoryId"],
                                            projectId=ALPHA, why="the batch version timed out")
        self.assertEqual(out["state"], "proposed", out)
        row = self.conn.execute("SELECT * FROM ledger WHERE memory_id = ?", (out["memoryId"],)).fetchone()
        self.assertEqual((row["kind"], row["function"], row["confidence"]),
                         ("learning", "experiential", 0.5))
        self.assertEqual([(c["kind"], c["memoryId"]) for c in out["cites"]],
                         [("failure", fail["memoryId"]), ("fix", fix["memoryId"])])

    def test_a_lesson_needs_two_readable_records_of_its_project(self) -> None:
        fail, fix = self.pair()
        other, _ = self.pair(BETA)
        for failure, fixed in ((fail["memoryId"], fail["memoryId"]),
                               (other["memoryId"], fix["memoryId"]),
                               ("mem:nothing", fix["memoryId"])):
            out = self.ws.srv.observatory_learn(owner=OWNER, statement="x", failureId=failure,
                                                fixId=fixed, projectId=ALPHA)
            self.assertEqual(out["error"], "learning refused", out)
        foreign = self.ws.srv.observatory_learn(owner=OWNER, statement="x",
                                                failureId=other["memoryId"],
                                                fixId=fix["memoryId"], projectId=ALPHA)
        missing = self.ws.srv.observatory_learn(owner=OWNER, statement="x",
                                                failureId="mem:nothing",
                                                fixId=fix["memoryId"], projectId=ALPHA)
        self.assertEqual(foreign["code"], missing["code"], "foreign and missing read alike")

    def test_nobody_promotes_their_own_proposal(self) -> None:
        fail, fix = self.pair()
        lesson = self.ws.srv.observatory_learn(owner=OWNER, statement="stream large exports",
                                               failureId=fail["memoryId"], fixId=fix["memoryId"],
                                               projectId=ALPHA)
        mid = lesson["memoryId"]
        with self.assertRaises(self.L.IllegalTransition):
            self.L.transition(self.conn, mid, to_state="observed", owner=OWNER, expected_revision=1)
        with self.assertRaises(self.L.OwnerRefused):
            self.L.corroborate(self.conn, mid, by=OWNER, check={"how": "I am sure"},
                               expected_revision=1)
        # A correction with full confidence is still a proposal.
        again = self.ws.srv.observatory_record(owner=OWNER, statement="stream exports over 10k rows",
                                               projectId=ALPHA, memoryId=mid, expectedRevision=1)
        self.assertEqual(again["state"], "proposed", again)
        # An independent witness, or the operator, can.
        promoted = self.L.corroborate(self.conn, mid, by="service:ci", check={"how": "re-ran"},
                                      expected_revision=2)
        self.assertEqual(promoted["state"], "observed")

    def test_a_binding_learns_only_in_its_scope(self) -> None:
        fail, fix = self.pair()
        self.ws.bind(OWNER, [ALPHA], bearer="tok-a", effect="read", scopes=("memory.read",))
        with self.ws.http("tok-a"):
            out = self.ws.srv.observatory_learn(owner=OWNER, statement="x", failureId=fail["memoryId"],
                                                fixId=fix["memoryId"], projectId=ALPHA)
        self.assertEqual(out.get("code"), "effect-above-ceiling", out)


if __name__ == "__main__":
    unittest.main()
