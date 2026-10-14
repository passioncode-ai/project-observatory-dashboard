#!/usr/bin/env python3
"""The ask for a `why`: once per record, not when the rationale is already written,
delivered with the next prompt, and measured.

**What this replaces.** The Stop hook printed a request for a `why` as a Stop
`systemMessage` after EVERY turn that changed a watched repository. Measured over
fourteen days of one machine's transcripts: 3,593 asks, 2.93 million characters of
hook output (each stored twice in the transcript), and 34 `observatory_record` calls
carrying a memory id in the same window — at most 0.95 % answered. Three causes,
each with its case below:

1. **Every turn asked again.** A session is ONE record revised per turn, so the
   second changed turn asked for the same record's why a second time.
2. **The why already existed.** A turn that wrote the project's decision register
   (`docs/DECISIONS.md`, `docs/adr/*`, a `decisions.md`) or a task-pipeline ledger
   line (`decision:` / `stage:` in `.task-pipeline/run.md`) had written its
   rationale; asking for a second copy in the ledger is noise. The record now links
   that file in its evidence and the hook stays silent.
3. **The channel.** A Stop `systemMessage` is a notice shown to the person; it is
   not part of what the model reads. The ask now travels as the next
   `UserPromptSubmit`'s `additionalContext`, once (`tests/test_ask_why_reach.py`
   proves that channel against a recorded transcript).

And it is measured: every ask is journalled (`store/raw/why-asks.jsonl`), an ask is
answered when its record later carries a `why`, the receipt
`store/raw/record-turn.json` carries `whyAsks`, and `observatory.why_unanswered`
fires below a 10 % answer rate over 7 days with at least 20 asks.

The hook fails OPEN for the person's prompt and toward ASKING for the record: an
unreadable journal asks (the old behaviour), an unwritable pending store falls back
to the old Stop notice, and nothing here ever blocks a turn or a prompt.
"""
from __future__ import annotations
import json
import os
import pathlib
import sqlite3
import stat
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "dashboard"))
import tmp as tmpdir                                                # noqa: E402
import watched_repo                                                 # noqa: E402
# A synthetic workspace for the whole suite: the finding rule imports the engine's
# configuration, and no case here may read or write a real one.
from test_portable_mcp import setup as portable_setup              # noqa: E402
portable_setup()

PLUGIN = ROOT / "skill/plugins/observatory-log"
STOP = PLUGIN / "hooks/record-turn.sh"
ASK = PLUGIN / "hooks/ask-why.py"
PROMPT = PLUGIN / "hooks/prompt-why.py"
PY = sys.executable


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          timeout=120).stdout


class Fixture:
    """A watched checkout, an isolated store and scratch, and an isolated hook state."""

    def __init__(self) -> None:
        self.work, self.repo, built = watched_repo.build()
        self.state = self.work / "xdg-state"
        self.env = {**os.environ, **built,
                    "OBSERVATORY_DB": str(self.work / "observatory.db"),
                    "OBSERVATORY_SCRATCH": str(self.work / "scratch"),
                    "OBSERVATORY_ROOT": str(ROOT),
                    "OBSERVATORY_PYTHON": PY,
                    "CLAUDE_PLUGIN_ROOT": str(PLUGIN),
                    "XDG_STATE_HOME": str(self.state)}

    @property
    def scratch(self) -> pathlib.Path:
        return self.work / "scratch"

    @property
    def pending_dir(self) -> pathlib.Path:
        return self.state / "project-observatory" / "ask-why"

    def stop(self, session: str = "sess-1") -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(STOP)], input=json.dumps(
            {"session_id": session, "cwd": str(self.repo), "stop_hook_active": False}),
            text=True, capture_output=True, env=self.env, timeout=300)

    def prompt(self, session: str = "sess-1", raw: str | None = None,
               ) -> subprocess.CompletedProcess:
        payload = raw if raw is not None else json.dumps(
            {"session_id": session, "cwd": str(self.repo), "prompt": "next",
             "hook_event_name": "UserPromptSubmit"})
        return subprocess.run([PY, str(PROMPT)], input=payload, text=True,
                              capture_output=True, env=self.env, timeout=60)

    def receipt(self) -> dict:
        return json.loads((self.scratch / "record-turn.json").read_text(encoding="utf-8"))

    def journal(self) -> list[dict]:
        f = self.scratch / "why-asks.jsonl"
        if not f.is_file():
            return []
        return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]

    def ledger(self, sql: str, *args) -> list:
        con = sqlite3.connect(f"file:{self.work / 'observatory.db'}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def context(self, p: subprocess.CompletedProcess) -> str:
        if not p.stdout.strip():
            return ""
        out = json.loads(p.stdout)
        hso = out.get("hookSpecificOutput") or {}
        return hso.get("additionalContext", "") if hso.get("hookEventName") == "UserPromptSubmit" else ""


class AskOncePerRecord(unittest.TestCase):
    def test_two_changed_turns_of_one_session_ask_exactly_once(self):
        fx = Fixture()
        first = fx.stop()
        self.assertEqual(first.returncode, 0, first.stderr[-300:])
        (fx.repo / "second.txt").write_text("the second turn's work\n", encoding="utf-8")
        second = fx.stop()
        self.assertEqual(second.returncode, 0, second.stderr[-300:])
        revs = fx.ledger("SELECT memory_id, revision FROM ledger WHERE kind='session'")
        self.assertEqual(len({r["memory_id"] for r in revs}), 1, "one record per session")
        self.assertEqual(len(revs), 2, "the second turn revised it")
        asks = fx.journal()
        self.assertEqual(len(asks), 1, f"one ask journalled, got {asks}")
        self.assertEqual(asks[0]["memoryId"], revs[0]["memory_id"])
        self.assertEqual(fx.receipt().get("ask"), False,
                         "the second turn's receipt says it did not ask")
        # The Stop hook speaks to nobody now: the ask waits for the next prompt.
        self.assertEqual(first.stdout.strip(), "", first.stdout[:200])
        self.assertEqual(second.stdout.strip(), "", second.stdout[:200])
        delivered = fx.context(fx.prompt())
        self.assertIn(asks[0]["memoryId"], delivered)
        self.assertIn("observatory_record(", delivered)
        self.assertEqual(fx.context(fx.prompt()), "", "delivered once, then silent")

    def test_the_next_prompt_gets_it_once_and_another_session_never(self):
        fx = Fixture()
        fx.stop("sess-a")
        self.assertEqual(fx.context(fx.prompt("sess-b")), "", "another session's ask")
        got = fx.context(fx.prompt("sess-a"))
        self.assertIn("why", got)
        self.assertIn("expected_revision=1", got)
        self.assertIn("invented", got, "the ask still refuses a fabricated reason")
        self.assertEqual(fx.context(fx.prompt("sess-a")), "")

    def test_an_explained_record_is_never_asked_about(self):
        fx = Fixture()
        fx.stop()
        mid = fx.journal()[0]["memoryId"]
        fx.prompt()                                     # the ask is consumed
        # The agent answers through the ledger, as the ask tells it to.
        code = ("import sys; sys.path.insert(0, '.')\n"
                "from store import db, ledger as L\n"
                "c = db.connect()\n"
                "r = c.execute(\"SELECT revision, statement FROM ledger WHERE memory_id=? "
                "ORDER BY revision DESC LIMIT 1\", (sys.argv[1],)).fetchone()\n"
                "L.append(c, owner='agent:claude-code', kind='session', memory_id=sys.argv[1],"
                " expected_revision=r[0], statement=r[1], why='the constraint',"
                " session_id='sess-1', state='proposed')\n"
                "c.commit()\n")
        p = subprocess.run([PY, "-c", code, mid], cwd=ROOT, env=fx.env, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        (fx.repo / "third.txt").write_text("more\n", encoding="utf-8")
        fx.stop()
        self.assertEqual(len(fx.journal()), 1, "no second ask")
        self.assertEqual(fx.context(fx.prompt()), "")
        self.assertEqual(fx.receipt()["whyAsks"]["answered"], 1, fx.receipt().get("whyAsks"))


class SkipWhenTheRationaleExists(unittest.TestCase):
    def test_a_turn_editing_the_decision_register_is_not_asked_about(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(
            "# Decisions\n\n## DEC-0001 one record per decision\n\nBecause.\n", encoding="utf-8")
        p = fx.stop()
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        self.assertEqual(p.stdout.strip(), "")
        self.assertEqual(fx.journal(), [], "nothing asked")
        self.assertEqual(fx.context(fx.prompt()), "", "nothing pending")
        rcpt = fx.receipt()
        self.assertEqual((rcpt.get("rationale") or {}).get("ref"), "docs/DECISIONS.md", rcpt)
        ev = json.loads(fx.ledger("SELECT evidence_json FROM ledger WHERE kind='session'")[0][0])
        link = [e for e in ev if e.get("uri") == "file:docs/DECISIONS.md"]
        self.assertEqual(len(link), 1, f"the record links the rationale: {ev}")
        self.assertIn("DEC-0001", link[0].get("anchor", ""), link)
        why = fx.ledger("SELECT why FROM ledger WHERE kind='session'")[0][0]
        self.assertIsNone(why, "a link to the canonical rationale, never a second copy")

    def test_an_adr_or_a_committed_register_counts_too(self):
        fx = Fixture()
        (fx.repo / "docs/adr").mkdir(parents=True)
        (fx.repo / "docs/adr/0002-store.md").write_text("# 0002 The store\n", encoding="utf-8")
        _git(fx.repo, "add", "-A")
        _git(fx.repo, "commit", "-qm", "adr")
        # A commit is only "unpushed" against an upstream: give it one.
        bare = fx.work / "origin.git"
        _git(fx.work, "init", "-q", "--bare", str(bare))
        _git(fx.repo, "remote", "add", "upstream-fixture", str(bare))
        _git(fx.repo, "push", "-q", "upstream-fixture", "HEAD~1:refs/heads/main")
        _git(fx.repo, "branch", "-q", "--set-upstream-to=upstream-fixture/main")
        fx.stop()
        self.assertEqual(fx.journal(), [])
        self.assertEqual((fx.receipt().get("rationale") or {}).get("ref"),
                         "docs/adr/0002-store.md", fx.receipt())

    def test_a_task_pipeline_decision_line_counts_and_any_other_line_does_not(self):
        fx = Fixture()
        (fx.repo / ".task-pipeline").mkdir()
        (fx.repo / ".task-pipeline/run.md").write_text(
            "# run\n\n- note: looked around\n", encoding="utf-8")
        fx.stop("sess-plain")
        self.assertEqual(len(fx.journal()), 1, "a run ledger without a decision line asks")

        fx2 = Fixture()
        (fx2.repo / ".task-pipeline").mkdir()
        (fx2.repo / ".task-pipeline/run.md").write_text(
            "# run\n\n- decision: keep the slot, add a journal\n", encoding="utf-8")
        fx2.stop()
        self.assertEqual(fx2.journal(), [])
        r = fx2.receipt().get("rationale") or {}
        self.assertEqual((r.get("source"), r.get("ref")), ("task-pipeline", ".task-pipeline/run.md"), r)
        self.assertIn("keep the slot", r.get("anchor", ""))

    def test_a_file_merely_named_like_a_register_elsewhere_is_still_a_register(self):
        fx = Fixture()
        (fx.repo / "artifacts").mkdir()
        (fx.repo / "artifacts/decisions.md").write_text("## D1 x\n", encoding="utf-8")
        fx.stop()
        self.assertEqual(fx.journal(), [])


class FailOpen(unittest.TestCase):
    def test_an_unreadable_journal_still_asks(self):
        fx = Fixture()
        (fx.scratch / "why-asks.jsonl").mkdir()          # cannot be read or appended
        p = fx.stop()
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        self.assertIn("observatory_record(", fx.context(fx.prompt()),
                      "an error in the ask state asks, as before")

    def test_an_unwritable_pending_store_falls_back_to_the_stop_notice(self):
        fx = Fixture()
        fx.state.mkdir(parents=True)
        (fx.state / "project-observatory").write_text("not a directory", encoding="utf-8")
        p = fx.stop()
        self.assertEqual(p.returncode, 0)
        msg = json.loads(p.stdout or "{}").get("systemMessage", "")
        self.assertIn("observatory_record(", msg, "the ask is never lost")
        self.assertNotIn("decision", json.loads(p.stdout or "{}"))

    def test_the_prompt_hook_never_fails_a_prompt(self):
        fx = Fixture()
        for raw in ("", "not json", "[]", json.dumps({"session_id": "../../etc/passwd"}),
                    json.dumps({"session_id": ""}), json.dumps({"no": "session"})):
            p = fx.prompt(raw=raw)
            self.assertEqual(p.returncode, 0, raw)
            self.assertEqual(p.stdout.strip(), "", raw)
        fx.pending_dir.mkdir(parents=True)
        (fx.pending_dir / "sess-1.json").write_text("{torn", encoding="utf-8")
        p = fx.prompt()
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))
        self.assertFalse((fx.pending_dir / "sess-1.json").exists(), "a torn file is dropped")

    def test_a_fault_is_still_spoken_at_stop(self):
        p = subprocess.run([PY, str(ASK)], input=json.dumps(
            {"recorded": False, "fault": True, "reason": "IllegalTransition: x"}),
            text=True, capture_output=True, timeout=60)
        self.assertIn("could NOT record", json.loads(p.stdout)["systemMessage"])

    def test_an_old_recorder_without_the_ask_field_is_asked_as_before(self):
        fx = Fixture()
        p = subprocess.run([PY, str(ASK)], input=json.dumps(
            {"recorded": True, "hasWhy": False, "memoryId": "mem:old", "revision": 3,
             "project": "project:watched", "files": 1, "session": "sess-old"}),
            text=True, capture_output=True, env=fx.env, timeout=60)
        self.assertEqual(p.stdout.strip(), "")
        self.assertIn("mem:old", fx.context(fx.prompt("sess-old")))


class PendingStoreHygiene(unittest.TestCase):
    def test_the_pending_ask_is_private_and_holds_no_value(self):
        fx = Fixture()
        fx.stop()
        f = fx.pending_dir / "sess-1.json"
        self.assertTrue(f.is_file())
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(fx.pending_dir.stat().st_mode), 0o700)
        doc = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(sorted(doc), ["asks"])
        self.assertEqual(sorted(doc["asks"][0]), ["at", "context", "memoryId", "revision"])

    def test_an_abandoned_ask_is_pruned(self):
        fx = Fixture()
        fx.pending_dir.mkdir(parents=True)
        old = fx.pending_dir / "gone-session.json"
        old.write_text(json.dumps({"asks": []}), encoding="utf-8")
        os.utime(old, (1, 1))
        fx.stop()
        self.assertFalse(old.exists(), "a session that never prompted again leaves nothing")


class Measured(unittest.TestCase):
    def test_the_receipt_carries_the_rate(self):
        fx = Fixture()
        fx.stop()
        w = fx.receipt().get("whyAsks") or {}
        self.assertEqual((w.get("asked"), w.get("answered")), (1, 0), w)
        self.assertEqual(w.get("rate"), 0.0)
        self.assertEqual((w.get("window") or {}).get("days"), 7, w)

    def test_the_measurement_counts_distinct_records_and_their_answers(self):
        import why_asks
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE ledger (memory_id TEXT, revision INT, why TEXT)")
        con.executemany("INSERT INTO ledger VALUES (?,?,?)",
                        [("m1", 1, None), ("m1", 2, "because"), ("m2", 1, None),
                         ("m3", 1, "  ")])
        rows = [{"at": "2026-10-14T10:00:00Z", "memoryId": m} for m in ("m1", "m2", "m3", "m1")]
        rows.append({"at": "2020-01-01T00:00:00Z", "memoryId": "m-old"})
        got = why_asks.measure(con, rows=rows, now="2026-10-14T12:00:00Z")
        self.assertEqual((got["asked"], got["answered"], got["rate"]), (3, 1, 0.333), got)
        self.assertEqual(why_asks.measure(con, rows=[], now="2026-10-14T12:00:00Z")["rate"], None)


class TheFinding(unittest.TestCase):
    def findings(self, asked, answered):
        import build_findings
        rate = None if not asked else round(answered / asked, 3)
        return build_findings.why_unanswered_findings(
            {"asked": asked, "answered": answered, "rate": rate,
             "window": {"days": 7, "since": "2026-10-07T00:00:00Z"}})

    def test_it_fires_below_ten_percent_with_twenty_asks(self):
        out = self.findings(25, 1)
        self.assertEqual([f["type"] for f in out], ["observatory.why_unanswered"])
        f = out[0]
        self.assertEqual(f["title_args"]["n"], 24)
        self.assertIn("1 of 25", f["detail"])
        self.assertTrue(f["action"] and f["evidence"])

    def test_it_is_silent_otherwise(self):
        self.assertEqual(self.findings(19, 0), [], "too few asks to judge")
        self.assertEqual(self.findings(30, 3), [], "10 % is the floor, not below it")
        self.assertEqual(self.findings(0, 0), [])
        import build_findings
        self.assertEqual(build_findings.why_unanswered_findings(None), [])

    def test_the_type_is_labelled_and_translated(self):
        import finding_types
        self.assertIn("observatory.why_unanswered", finding_types.LABELS)
        ru = json.loads((ROOT / "dashboard/locales/ru.json").read_text(encoding="utf-8"))
        self.assertIn(finding_types.LABELS["observatory.why_unanswered"], ru)
        self.assertIn(self.findings(25, 1)[0]["title_id"], ru)


if __name__ == "__main__":
    unittest.main(verbosity=2)
