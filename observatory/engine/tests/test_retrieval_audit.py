#!/usr/bin/env python3
"""Retrieval receipts, scoped explain and redaction on the way out (PB-137 N-012).

Over the real MCP server module and a real store in a temporary workspace (the fixture of
tests/test_memory_access.py):

* a search answer names its receipt; the receipt holds the exact result revisions, the
  binding and the scope, the query only as an HMAC, and no statement text;
* `observatory_explain` re-reads exactly those revisions, says what each is now, and never
  searches again; another caller, or an id that does not exist, gets one refusal;
* a known value or a credential shape stored by a writer that did not redact it is absent
  from search, recall, explain, workflow answers, error details and the receipt log, while
  ids and lease tokens survive.
"""
from __future__ import annotations

import json
import pathlib
import stat
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_memory_access import ALPHA, BETA, Workspace, refused, tearDownModule  # noqa: E402,F401

#: A workspace secret with no recognisable shape: only the known-value filter can catch it.
KNOWN = "zebra" + "-orchard-" + "lantern-4417"
#: A credential SHAPE, assembled so no literal key sits in this file.
SHAPED = "sk" + "-proj-" + "a1b2c3d4" * 5


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()
        import memory_redact
        import retrieval_audit
        self.RA = retrieval_audit
        retrieval_audit._redactor = lambda: memory_redact.Redactor(
            known_loader=lambda: {KNOWN: "DB_PASSWORD"})

    def raw_note(self, statement: str, project: str = ALPHA, **kw) -> dict:
        """A record written straight to the ledger, as a collector would: no redaction."""
        from store import db as sdb
        from store import ledger as L
        conn = sdb.connect()
        try:
            out = L.append(conn, owner="agent:collector", statement=statement, project_id=project,
                           confidence=0.5, **kw)
            conn.commit()
            return out
        finally:
            conn.close()

    def receipts(self) -> list[dict]:
        path = self.RA.journal_path()
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


class Receipts(Base):
    def test_a_search_names_its_receipt_and_the_log_holds_no_text(self) -> None:
        self.raw_note("the exporter schema decision", why="because the partner asked")
        out = self.ws.srv.observatory_search(query="exporter schema", project_id=ALPHA)
        rid = out["receipt"]["receiptId"]
        self.assertTrue(rid and rid.startswith("rcpt_"), out["receipt"])
        rows = self.receipts()
        self.assertEqual([r["receiptId"] for r in rows], [rid])
        row = rows[0]
        self.assertEqual([(r["memoryId"], r["revision"]) for r in row["results"]],
                         [(r["memoryId"], r["revision"]) for r in out["results"]])
        self.assertEqual((row["binding"], row["projectId"]), ("local:stdio", ALPHA))
        text = json.dumps(row)
        for leaked in ("exporter", "schema", "partner"):
            self.assertNotIn(leaked, text, "the receipt holds neither the query nor the text")
        self.assertEqual(out["receipt"]["resultBytes"], row["resultBytes"])
        self.assertEqual(stat.S_IMODE(self.RA.journal_path().stat().st_mode), 0o600)

    def test_explain_reads_the_receipt_back_without_searching_again(self) -> None:
        first = self.raw_note("exporter schema first wording")
        out = self.ws.srv.observatory_search(query="exporter schema", project_id=ALPHA)
        rid = out["receipt"]["receiptId"]
        from store import db as sdb
        from store import ledger as L
        conn = sdb.connect()
        L.append(conn, owner="agent:collector", statement="exporter schema second wording",
                 memory_id=first["memoryId"], expected_revision=first["revision"],
                 project_id=ALPHA, confidence=0.5)
        self.raw_note("exporter schema a brand new record")
        conn.commit()
        conn.close()
        got = self.ws.srv.observatory_explain(receiptId=rid)
        self.assertEqual([(r["memoryId"], r["revision"]) for r in got["results"]],
                         [(first["memoryId"], first["revision"])],
                         "exactly the returned revisions; the new record was not searched for")
        item = got["results"][0]
        self.assertEqual((item["now"], item["latestRevision"]), ("superseded", 2))
        self.assertEqual(item["statement"], "exporter schema first wording")
        self.assertIn("lexical", item["matched"])

    def test_an_erased_result_is_explained_without_its_text(self) -> None:
        gone = self.raw_note("exporter schema withdrawn")
        out = self.ws.srv.observatory_search(query="exporter schema", project_id=ALPHA)
        from store import db as sdb
        from store import ledger as L
        conn = sdb.connect()
        L.tombstone(conn, gone["memoryId"], reason="test", approved_by="operator")
        conn.commit()
        conn.close()
        got = self.ws.srv.observatory_explain(receiptId=out["receipt"]["receiptId"])
        self.assertEqual(got["results"][0]["now"], "erased")
        self.assertNotIn("statement", got["results"][0])

    def test_only_the_receiving_caller_can_explain(self) -> None:
        self.ws.bind("agent:alpha-bot", [ALPHA], bearer="tok-alpha")
        self.ws.bind("agent:beta-bot", [BETA], bearer="tok-beta")
        self.raw_note("exporter schema alpha")
        with self.ws.http("tok-alpha"):
            mine = self.ws.srv.observatory_search(query="exporter schema", project_id=ALPHA)
            ok = self.ws.srv.observatory_explain(receiptId=mine["receipt"]["receiptId"])
        self.assertEqual(ok["results"][0]["now"], "current", ok)
        with self.ws.http("tok-beta"):
            theirs = self.ws.srv.observatory_explain(receiptId=mine["receipt"]["receiptId"])
            nothing = self.ws.srv.observatory_explain(receiptId="rcpt_00000000000000ff")
        local = self.ws.srv.observatory_explain(receiptId=mine["receipt"]["receiptId"])
        for out in (theirs, nothing, local):
            self.assertEqual((out.get("error"), out.get("code")),
                             ("explain refused", "unknown-receipt"), out)
        self.assertEqual(theirs["hint"], nothing["hint"], "one answer for both")
        self.assertNotIn(ALPHA, json.dumps(theirs), "no project is named to another caller")

    def test_a_result_the_caller_may_no_longer_see_loses_its_text(self) -> None:
        b = self.ws.bind("agent:alpha-bot", [ALPHA], bearer="tok-alpha")
        rec = self.raw_note("exporter schema internal")
        with self.ws.http("tok-alpha"):
            out = self.ws.srv.observatory_search(query="exporter schema", project_id=ALPHA)
        from store import db as sdb
        conn = sdb.connect()
        conn.execute("UPDATE ledger SET classification = 'confidential' WHERE memory_id = ?",
                     (rec["memoryId"],))
        conn.commit()
        conn.close()
        with self.ws.http("tok-alpha"):
            got = self.ws.srv.observatory_explain(receiptId=out["receipt"]["receiptId"])
        self.assertEqual(got["results"][0]["now"], "no-longer-visible", got)
        self.assertNotIn("statement", got["results"][0])
        self.assertEqual(b["bindingId"], self.receipts()[-1]["binding"])


class RedactionOnTheWayOut(Base):
    def test_search_recall_and_explain_never_return_a_secret(self) -> None:
        self.raw_note(f"exporter schema uses {KNOWN} and {SHAPED}", why=f"the key {SHAPED}")
        out = self.ws.srv.observatory_search(query="exporter schema", project_id=ALPHA)
        recall = self.ws.srv.observatory_recall(projectId=ALPHA)
        explained = self.ws.srv.observatory_explain(receiptId=out["receipt"]["receiptId"])
        for name, answer in (("search", out), ("recall", recall), ("explain", explained)):
            text = json.dumps(answer)
            self.assertNotIn(KNOWN, text, name)
            self.assertNotIn(SHAPED, text, name)
            self.assertIn("[redacted:DB_PASSWORD]", text, name)
            self.assertGreaterEqual(answer["redacted"]["knownValues"], 1, name)
        self.assertNotIn(KNOWN, self.RA.journal_path().read_text(encoding="utf-8"))
        self.assertTrue(out["results"][0]["memoryId"], "ids survive")

    def test_workflow_answers_are_redacted_and_keep_their_ids(self) -> None:
        wf = self.ws.start(ALPHA)
        from store import db as sdb
        conn = sdb.connect()
        # A secret the workspace learned AFTER the checkpoint was written.
        row = conn.execute("SELECT body_json FROM ledger WHERE memory_id = ?",
                           (f"ckpt:{wf['workflowId']}",)).fetchone()
        body = json.loads(row[0])
        body["goal"] = f"rotate {KNOWN}"
        conn.execute("UPDATE ledger SET body_json = ? WHERE memory_id = ?",
                     (json.dumps(body), f"ckpt:{wf['workflowId']}"))
        conn.commit()
        conn.close()
        latest = self.ws.srv.observatory_checkpoint_latest(workflowId=wf["workflowId"])
        listed = self.ws.srv.observatory_workflow_list(projectId=ALPHA)
        for name, answer in (("latest", latest), ("list", listed)):
            self.assertNotIn(KNOWN, json.dumps(answer), name)
            self.assertIn("redacted", answer, name)
        self.assertEqual(latest["workflowId"], wf["workflowId"])

    def test_a_handoff_keeps_its_transcript_pointer_and_lease(self) -> None:
        wf = self.ws.start(ALPHA)
        session = "0f8e2b4c-1a2b-4c3d-8e9f-0123456789ab"
        h = self.ws.srv.observatory_handoff_create(
            owner="agent:alpha-bot", idempotencyKey="key-handoff-0001",
            workflowId=wf["workflowId"], to={"provider": "anthropic"}, reason="limit",
            transcript={"provider": "anthropic", "sessionId": session}, leaseId=wf["leaseId"])
        self.assertIn("handoffId", h, h)
        got = self.ws.srv.observatory_handoff_get(handoffId=h["handoffId"])
        self.assertEqual(got["pack"]["transcript"]["sessionId"], session,
                         "a session UUID is a pointer, not a secret")
        took = self.ws.srv.observatory_handoff_accept(
            owner="agent:beta-bot", idempotencyKey="key-accept-0001", handoffId=h["handoffId"])
        self.assertTrue(took.get("leaseId") and "redacted" not in took["leaseId"], took)
        self.assertEqual(took["pack"]["transcript"]["sessionId"], session)

    def test_an_error_detail_is_redacted(self) -> None:
        from store import ledger as L
        out = self.ws.srv._write_error(L.LedgerError(f"could not parse {KNOWN}"))
        self.assertNotIn(KNOWN, out["detail"])
        self.assertIn("[redacted:DB_PASSWORD]", out["detail"])


if __name__ == "__main__":
    unittest.main()
