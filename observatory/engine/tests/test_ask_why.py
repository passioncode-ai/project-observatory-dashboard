#!/usr/bin/env python3
"""The ask for a `why`: once per record, not when this session already wrote the
rationale, delivered with the next prompt, and measured on what was delivered.

**What this replaces.** The Stop hook printed a request for a `why` as a Stop
`systemMessage` after EVERY turn that changed a watched repository. Measured over
fourteen days of one machine's transcripts: 3,593 asks, 2.93 million characters of
hook output (each stored twice in the transcript), and 34 `observatory_record` calls
carrying a memory id in the same window — at most 0.95 % answered. Three causes,
each with its cases below:

1. **Every turn asked again.** A session is ONE record revised per turn, so the
   second changed turn asked for the same record's why a second time.
2. **The why already existed.** A turn that wrote the project's decision register
   (`docs/DECISIONS.md`, `docs/adr/*`, a `decisions.md`) or a task-pipeline ledger
   line (`decision:` / `stage:` in `.task-pipeline/run.md`) had written its
   rationale. The record now links that file and the hook stays silent — but only
   for a rationale THIS session's own tools wrote (read from its transcript), with
   at least one non-blank added line, not deleted, and outside a vendored tree.
3. **The channel.** A Stop `systemMessage` is a notice shown to the person; it is
   not part of what the model reads. The ask now travels as the next
   `UserPromptSubmit`'s `additionalContext`, once (`tests/test_ask_why_reach.py`).

And it is measured on DELIVERED asks: the prompt hook journals each delivery, a
headless session that never sends a second prompt adds nothing to the denominator,
and `observatory.why_unanswered` fires below 10 % over 7 days with at least 20
delivered asks — through `build_findings.collect()`, which a case here drives.

The hook fails OPEN for the person's prompt and toward ASKING for the record.
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
from unittest import mock

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
    """A watched checkout, an isolated store and scratch, an isolated hook state, and
    the session's own transcript."""

    def __init__(self) -> None:
        self.work, self.repo, built = watched_repo.build()
        self.state = self.work / "xdg-state"
        self.transcripts = self.work / "transcripts"
        self.transcripts.mkdir()
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

    def transcript(self, session: str) -> pathlib.Path:
        return self.transcripts / f"{session}.jsonl"

    def wrote(self, text: str, *, session: str = "sess-1", tool: str = "Edit",
              rel: str = "docs/DECISIONS.md", use_id: str = "toolu_fixture",
              sidechain: bool = False, failed: bool = False) -> None:
        """Record in the session's transcript that ITS tool wrote `text` — or, with
        `sidechain`, that a subagent did; with `failed`, that the call errored."""
        key = {"Edit": "new_string", "Write": "content", "Bash": "command"}[tool]
        inp = {key: text} if tool == "Bash" else {"file_path": str(self.repo / rel), key: text}
        rec = {"type": "assistant", "sessionId": session, "message": {
            "role": "assistant", "content": [{"type": "tool_use", "id": use_id,
                                              "name": tool, "input": inp}]}}
        if sidechain:
            rec["isSidechain"] = True
        with self.transcript(session).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
            # Every call gets its result, as in a real transcript: an error, or success.
            fh.write(json.dumps({"type": "user", "sessionId": session, "message": {
                "role": "user", "content": [{"type": "tool_result", "tool_use_id": use_id,
                                             "is_error": bool(failed),
                                             "content": "refused" if failed else "ok"}]}}) + "\n")

    def bash(self, command: str, *, session: str = "sess-1") -> None:
        """Record that the session ran `command` through its Bash tool."""
        self.wrote(command, session=session, tool="Bash")

    def filler(self, megabytes: int, *, session: str = "sess-1") -> None:
        """Pad the transcript with ordinary conversation, the bulk a long session has."""
        line = json.dumps({"type": "user", "sessionId": session, "message": {
            "role": "user", "content": "an ordinary message in a long session " * 4}}) + "\n"
        with self.transcript(session).open("a", encoding="utf-8") as fh:
            fh.write(line * (megabytes * (1 << 20) // len(line)))

    def timed_stop(self, session: str = "sess-1", limit: float = 60.0) -> float:
        import time as _t
        t0 = _t.monotonic()
        try:
            p = self.stop(session, timeout=limit)
        except subprocess.TimeoutExpired:
            return limit
        assert p.returncode == 0, p.stderr[-300:]
        return _t.monotonic() - t0

    def read_back(self, text: str, *, session: str = "sess-1") -> None:
        """Record that the session READ `text` (a tool result) — never attribution."""
        rec = {"type": "user", "sessionId": session, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_fixture", "content": text}]}}
        with self.transcript(session).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def stop(self, session: str = "sess-1", transcript: bool = True,
             timeout: float = 300) -> subprocess.CompletedProcess:
        payload = {"session_id": session, "cwd": str(self.repo), "stop_hook_active": False}
        if transcript:
            t = self.transcript(session)
            t.touch()
            payload["transcript_path"] = str(t)
        return subprocess.run(["bash", str(STOP)], input=json.dumps(payload),
                              text=True, capture_output=True, env=self.env, timeout=timeout)

    def prompt(self, session: str = "sess-1", raw: str | None = None,
               ) -> subprocess.CompletedProcess:
        payload = raw if raw is not None else json.dumps(
            {"session_id": session, "cwd": str(self.repo), "prompt": "next",
             "hook_event_name": "UserPromptSubmit"})
        return subprocess.run([PY, str(PROMPT)], input=payload, text=True,
                              capture_output=True, env=self.env, timeout=60)

    def answer(self, delivered: str, why: str) -> subprocess.CompletedProcess:
        """Answer a delivered ask exactly as it prescribes: the real MCP tool
        `observatory_record(owner, memory_id, expected_revision, statement, why)`,
        with the memory id and revision read from the ask's own text."""
        import re
        m = re.search(r'observatory_record\(owner="([^"]+)", memory_id="([^"]+)", '
                      r'expected_revision=(\d+)', delivered)
        assert m, delivered[-400:]
        code = ("import importlib.util, json, sqlite3, sys\n"
                "spec = importlib.util.spec_from_file_location('srv_answer', 'mcp/server.py')\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "sys.modules['srv_answer'] = mod\n"
                "spec.loader.exec_module(mod)\n"
                "from store import db\n"
                "st = db.connect().execute('SELECT statement FROM ledger WHERE memory_id=?"
                " AND revision=?', (sys.argv[2], int(sys.argv[3]))).fetchone()[0]\n"
                "out = mod.observatory_record(owner=sys.argv[1], memoryId=sys.argv[2],"
                " expectedRevision=int(sys.argv[3]), statement=st, why=sys.argv[4])\n"
                "assert 'memoryId' in out, out\n")
        return subprocess.run([PY, "-c", code, *m.groups(), why], cwd=ROOT, env=self.env,
                              capture_output=True, text=True, timeout=120)

    def receipt(self) -> dict:
        return json.loads((self.scratch / "record-turn.json").read_text(encoding="utf-8"))

    def journal(self, event: str | None = "asked") -> list[dict]:
        f = self.scratch / "why-asks.jsonl"
        if not f.is_file():
            return []
        rows = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        return [r for r in rows if event is None or (r.get("event") or "asked") == event]

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

    def change(self, name: str = "src.py") -> None:
        """A real code change beside whatever else the case plants: a new file the
        repository tracks (intent-to-add), which is a change to TRACKED content."""
        (self.repo / name).write_text(f"print('{name}')\n", encoding="utf-8")
        _git(self.repo, "add", "-N", "--", name)

    def session_start(self, session: str = "sess-1") -> subprocess.CompletedProcess:
        """The SessionStart hook's engine half, which takes the session's baseline."""
        payload = {"session_id": session, "cwd": str(self.repo), "hook_event_name": "SessionStart"}
        return subprocess.run([PY, str(ROOT / "tools/session_start.py")], input=json.dumps(payload),
                              text=True, capture_output=True, env=self.env, timeout=120)

    def turn_state(self) -> pathlib.Path:
        return self.state / "project-observatory" / "turns"

    def commit_all(self, message: str = "base") -> None:
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", message)


REGISTER = "# Decisions\n\n## DEC-0001 one record per decision\n\nBecause the ask repeated.\n"


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

    def test_in_the_real_order_the_second_prompt_gets_nothing(self):
        # Stop, prompt, Stop, prompt — what a live session does. The hook's own
        # `ask` check is what keeps the second prompt silent: the pending file was
        # consumed by the first prompt, so only the hook can refuse to leave another.
        fx = Fixture()
        fx.stop()
        first = fx.context(fx.prompt())
        self.assertIn("observatory_record(", first)
        (fx.repo / "second.txt").write_text("the second turn's work\n", encoding="utf-8")
        p = fx.stop()
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))
        self.assertEqual(fx.context(fx.prompt()), "", "the same record is not asked again")
        self.assertFalse((fx.pending_dir / "sess-1.json").exists())

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
        # The answer goes through the REAL tool, as the ask prescribes: a `note` with
        # no session id. A lookup keyed on the session's own `kind='session'` rows
        # never saw it and revised with a stale revision.
        fx = Fixture()
        fx.stop()
        delivered = fx.context(fx.prompt())             # the ask is consumed
        p = fx.answer(delivered, "the constraint")
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        mid = fx.journal()[0]["memoryId"]
        kind, session = fx.ledger("SELECT kind, session_id FROM ledger WHERE memory_id=?"
                                  " ORDER BY revision DESC LIMIT 1", mid)[0]
        self.assertEqual((kind, session), ("note", None), "the tool's own shape")
        (fx.repo / "third.txt").write_text("more\n", encoding="utf-8")
        fx.stop()
        self.assertEqual(len(fx.journal()), 1, "no second ask")
        self.assertEqual(fx.context(fx.prompt()), "")
        w = fx.receipt()["whyAsks"]
        self.assertEqual((w["delivered"], w["answered"], w["rate"]), (1, 1, 1.0), w)

    def test_answering_the_ask_does_not_stop_the_recording(self):
        fx = Fixture()
        fx.stop()
        p = fx.answer(fx.context(fx.prompt()), "the constraint")
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        mid = fx.journal()[0]["memoryId"]
        for n, name in enumerate(("third.txt", "fourth.txt"), start=1):
            (fx.repo / name).write_text("more\n", encoding="utf-8")
            p = fx.stop()
            self.assertEqual((p.returncode, p.stdout.strip()), (0, ""), p.stderr[-400:])
            r = fx.receipt()
            self.assertTrue(r.get("recorded"), r)
            self.assertFalse(r.get("fault"), r)
            self.assertEqual(r.get("memoryId"), mid, "the same record, revised")
            self.assertTrue(r.get("hasWhy"), r)
        rows = fx.ledger("SELECT revision, kind, session_id, why FROM ledger WHERE memory_id=?"
                         " ORDER BY revision", mid)
        self.assertEqual([r[1] for r in rows], ["session", "note", "session", "session"])
        self.assertEqual(rows[-1][2], "sess-1")
        self.assertEqual(rows[-1][3], "the constraint", "the why is carried forward")
        self.assertEqual(len(fx.journal()), 1, "no second ask")
        self.assertEqual(fx.context(fx.prompt()), "")

    def _after(self, act: str) -> None:
        """Three changed turns on a record, then `act` takes it out of this
        session's hands, then three more changed turns: exactly one new record,
        asked about once, revised turn after turn."""
        fx = Fixture()
        fx.stop()
        mid = fx.journal()[0]["memoryId"]
        fx.prompt()
        for i in range(3):
            (fx.repo / f"a{i}.txt").write_text("x\n", encoding="utf-8")
            fx.stop()
        rev = fx.ledger("SELECT max(revision) FROM ledger WHERE memory_id=?", mid)[0][0]
        self.assertGreaterEqual(rev, 4, "the old record's revision is above a new one's 1")
        code = {
            "foreign": ("import importlib.util, sys\n"
                        "spec = importlib.util.spec_from_file_location('s', 'mcp/server.py')\n"
                        "m = importlib.util.module_from_spec(spec); sys.modules['s'] = m\n"
                        "spec.loader.exec_module(m)\n"
                        "out = m.observatory_record(owner='agent:claude-code', memoryId=sys.argv[1],"
                        " expectedRevision=int(sys.argv[2]), statement='claim', why='elsewhere',"
                        " projectId='project:someone-else')\n"
                        "assert 'memoryId' in out, out\n"),
            "promoted": ("import sys; sys.path.insert(0, '.')\n"
                         "from store import db, ledger as L\n"
                         "c = db.connect()\n"
                         "L.corroborate(c, sys.argv[1], by='service:corroborate',"
                         " check={'how': 'the commit resolves'}, expected_revision=int(sys.argv[2]))\n"
                         "c.commit()\n"),
            "rejected": ("import sys; sys.path.insert(0, '.')\n"
                         "from store import db, ledger as L\n"
                         "c = db.connect()\n"
                         "L.transition(c, sys.argv[1], to_state='rejected', owner='agent:claude-code',"
                         " expected_revision=int(sys.argv[2]))\n"
                         "c.commit()\n"),
        }[act]
        p = subprocess.run([PY, "-c", code, mid, str(rev)], cwd=ROOT, env=fx.env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        took = fx.ledger("SELECT max(revision) FROM ledger WHERE memory_id=?", mid)[0][0]
        seen, revisions = set(), []
        for i in range(3):
            fx.change(f"b{i}.txt")                # tracked: the new record is asked about
            p = fx.stop()
            self.assertEqual(p.returncode, 0, p.stderr[-400:])
            r = fx.receipt()
            self.assertTrue(r.get("recorded"), r)
            self.assertFalse(r.get("fault"), r)
            seen.add(r["memoryId"])
            revisions.append(r["revision"])
            if i == 0:
                self.assertEqual(r.get("continues"), mid, r)
            fx.context(fx.prompt())
        self.assertEqual(len(seen), 1, f"one new record, not one per turn: {seen}")
        self.assertNotIn(mid, seen)
        self.assertEqual(revisions, sorted(set(revisions)), f"revisions climb: {revisions}")
        asked = [j["memoryId"] for j in fx.journal()]
        self.assertEqual(asked, [mid, *seen], "the new record is asked about once")
        self.assertEqual(fx.ledger("SELECT max(revision) FROM ledger WHERE memory_id=?",
                                   mid)[0][0], took, "the record it continues is untouched")

    def test_a_record_another_project_took_is_continued_once(self):
        self._after("foreign")

    def test_a_promoted_record_is_continued_once(self):
        self._after("promoted")

    def test_a_rejected_record_is_continued_once(self):
        self._after("rejected")


class SkipWhenThisSessionWroteTheRationale(unittest.TestCase):
    def test_a_turn_editing_the_decision_register_is_not_asked_about(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.wrote(REGISTER, tool="Write")
        p = fx.stop()
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        self.assertEqual(p.stdout.strip(), "")
        self.assertEqual(fx.journal(), [], "nothing asked")
        self.assertEqual(fx.context(fx.prompt()), "", "nothing pending")
        rcpt = fx.receipt()
        self.assertEqual((rcpt.get("rationale") or {}).get("ref"), "docs/DECISIONS.md", rcpt)
        ev = json.loads(fx.ledger("SELECT evidence_json FROM ledger WHERE kind='session'"
                                  " ORDER BY revision DESC")[0][0])
        link = [e for e in ev if e.get("uri") == "file:docs/DECISIONS.md"]
        self.assertEqual(len(link), 1, f"the record links the rationale: {ev}")
        self.assertIn("DEC-0001", link[0].get("anchor", ""), link)
        whys = [r[0] for r in fx.ledger("SELECT why FROM ledger WHERE kind='session'")]
        self.assertEqual(set(whys), {None}, "a link to the canonical rationale, never a second copy")

    def test_an_edit_to_a_tracked_register_counts(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.commit_all()
        added = "## DEC-0002 measure delivered asks only\n"
        with (fx.repo / "docs/DECISIONS.md").open("a", encoding="utf-8") as fh:
            fh.write("\n" + added)
        fx.change()
        fx.wrote(added)
        fx.stop()
        self.assertEqual(fx.journal(), [])
        self.assertIn("DEC-0002", fx.receipt()["rationale"]["anchor"])

    def test_a_shell_heredoc_into_the_register_counts(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.bash("cat > docs/DECISIONS.md <<'EOF'\n" + REGISTER + "EOF")
        fx.stop()
        self.assertEqual(fx.journal(), [])
        self.assertEqual(fx.receipt()["rationale"]["ref"], "docs/DECISIONS.md")

    def test_the_rationale_is_a_follow_up_revision_of_the_same_record(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.wrote(REGISTER, tool="Write")
        fx.stop()
        revs = fx.ledger("SELECT memory_id, revision, evidence_json FROM ledger WHERE kind='session'"
                         " ORDER BY revision")
        self.assertEqual(len({r["memory_id"] for r in revs}), 1)
        self.assertEqual([r["revision"] for r in revs], [1, 2], "recorded first, then linked")
        self.assertFalse([e for e in json.loads(revs[0]["evidence_json"]) if e.get("kind") == "rationale"])
        self.assertTrue([e for e in json.loads(revs[1]["evidence_json"]) if e.get("kind") == "rationale"])
        self.assertEqual(fx.receipt()["revision"], 2)

    def test_a_register_with_several_headings_anchors_on_the_newest(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.commit_all()
        added = "## DEC-0002 the first new one\n\nWhy.\n\n## DEC-0003 the newest one\n\nWhy too.\n"
        with (fx.repo / "docs/DECISIONS.md").open("a", encoding="utf-8") as fh:
            fh.write("\n" + added)
        fx.change()
        fx.wrote("## DEC-0003 the newest one")       # only the newest heading is in the call
        fx.stop()
        self.assertEqual(fx.journal(), [])
        self.assertIn("DEC-0003", fx.receipt()["rationale"]["anchor"])

    def test_the_link_survives_a_commit_and_push_and_a_later_turn(self):
        fx = Fixture()
        bare = fx.work / "origin.git"
        _git(fx.work, "init", "-q", "--bare", str(bare))
        _git(fx.repo, "remote", "add", "upstream-fixture", str(bare))
        _git(fx.repo, "push", "-q", "-u", "upstream-fixture", "HEAD:refs/heads/main")
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.wrote(REGISTER, tool="Write")
        fx.stop()
        self.assertEqual(fx.receipt()["rationale"]["ref"], "docs/DECISIONS.md")
        fx.commit_all("decisions")
        _git(fx.repo, "push", "-q", "upstream-fixture", "HEAD:refs/heads/main")
        fx.change("later.py")
        fx.stop()
        self.assertEqual(fx.journal(), [], "the record stays explained")
        ev = json.loads(fx.ledger("SELECT evidence_json FROM ledger WHERE kind='session'"
                                  " ORDER BY revision DESC")[0][0])
        self.assertTrue([e for e in ev if e.get("uri") == "file:docs/DECISIONS.md"
                         and e.get("kind") == "rationale"], ev)
        self.assertEqual(fx.context(fx.prompt()), "")

    def test_the_link_survives_the_write_scrolling_out_of_the_tail(self):
        import record_turn
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.wrote(REGISTER, tool="Write")
        fx.stop()
        fx.filler(6)                                   # past the 4 MB tail
        fx.change("later.py")
        fx.stop()
        self.assertEqual(fx.journal(), [])
        self.assertEqual(fx.receipt()["rationale"]["ref"], "docs/DECISIONS.md")

    def test_sed_in_place_on_a_later_line_of_the_command(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.bash("git status\nsed -i 's/x/y/' docs/DECISIONS.md\n# " + REGISTER.replace("\n", " "))
        fx.stop()
        self.assertEqual(fx.journal(), [])
        self.assertEqual(fx.receipt()["rationale"]["ref"], "docs/DECISIONS.md")

    def test_one_revision_per_changed_turn_once_linked(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.wrote(REGISTER, tool="Write")
        fx.stop()
        self.assertEqual(len(fx.ledger("SELECT 1 FROM ledger WHERE kind='session'")), 2)
        for name in ("b.py", "c.py"):
            fx.change(name)
            fx.stop()
        self.assertEqual(len(fx.ledger("SELECT 1 FROM ledger WHERE kind='session'")), 4,
                         "2, then one per later changed turn (not 2, 4, 6)")

    def test_an_adr_in_an_unpushed_commit_counts_too(self):
        fx = Fixture()
        (fx.repo / "docs/adr").mkdir(parents=True)
        adr = "# 0002 The store\n\nOne store per workspace.\n"
        (fx.repo / "docs/adr/0002-store.md").write_text(adr, encoding="utf-8")
        fx.commit_all("adr")
        bare = fx.work / "origin.git"
        _git(fx.work, "init", "-q", "--bare", str(bare))
        _git(fx.repo, "remote", "add", "upstream-fixture", str(bare))
        _git(fx.repo, "push", "-q", "upstream-fixture", "HEAD~1:refs/heads/main")
        _git(fx.repo, "branch", "-q", "--set-upstream-to=upstream-fixture/main")
        fx.wrote(adr, tool="Write", rel="docs/adr/0002-store.md")
        fx.stop()
        self.assertEqual(fx.journal(), [])
        self.assertEqual((fx.receipt().get("rationale") or {}).get("ref"),
                         "docs/adr/0002-store.md", fx.receipt())

    def test_a_task_pipeline_decision_line_counts_and_any_other_line_does_not(self):
        fx = Fixture()
        (fx.repo / ".task-pipeline").mkdir()
        (fx.repo / ".task-pipeline/run.md").write_text(
            "# run\n\n- note: looked around the code\n", encoding="utf-8")
        fx.wrote("- note: looked around the code", tool="Bash")
        fx.stop("sess-1")
        self.assertEqual(len(fx.journal()), 1, "a run ledger without a decision line asks")

        fx2 = Fixture()
        (fx2.repo / ".task-pipeline").mkdir()
        (fx2.repo / ".task-pipeline/run.md").write_text(
            "# run\n\n- decision: keep the slot, add a journal\n", encoding="utf-8")
        fx2.bash("printf '%s\\n' '- decision: keep the slot, add a journal' >> .task-pipeline/run.md")
        fx2.stop()
        self.assertEqual(fx2.journal(), [])
        r = fx2.receipt().get("rationale") or {}
        self.assertEqual((r.get("source"), r.get("ref")), ("task-pipeline", ".task-pipeline/run.md"), r)
        self.assertIn("keep the slot", r.get("anchor", ""))

    def test_an_ignored_run_ledger_is_read_from_disk(self):
        # Many projects ignore `.task-pipeline/`; `git status` never lists it.
        fx = Fixture()
        (fx.repo / ".gitignore").write_text(".task-pipeline/\n", encoding="utf-8")
        fx.commit_all("ignore")
        (fx.repo / ".task-pipeline").mkdir()
        (fx.repo / ".task-pipeline/run.md").write_text(
            "# run\n\n- stage: plan approved, two hooks\n", encoding="utf-8")
        fx.change()
        fx.wrote("python3 stage_checkpoint.py record --stage 'plan approved, two hooks'", tool="Bash")
        fx.stop()
        self.assertEqual(fx.journal(), [])
        self.assertEqual((fx.receipt().get("rationale") or {}).get("source"), "task-pipeline")

    def test_a_register_in_a_run_artifacts_folder_counts(self):
        fx = Fixture()
        (fx.repo / "artifacts").mkdir()
        (fx.repo / "artifacts/decisions.md").write_text("## D1 keep the receipt\n", encoding="utf-8")
        fx.wrote("## D1 keep the receipt", tool="Write", rel="artifacts/decisions.md")
        fx.stop()
        self.assertEqual(fx.journal(), [])


class TheCarriedLinkIsRechecked(unittest.TestCase):
    """Round 6: a carried link stays only while it is still true."""

    def linked(self, fx: Fixture) -> None:
        fx.stop()
        self.assertEqual(fx.receipt()["rationale"]["ref"], "docs/DECISIONS.md")
        self.assertEqual(fx.journal(), [])

    def asked_next(self, fx: Fixture, name: str = "later.py") -> None:
        fx.change(name)
        fx.stop()
        self.assertIsNone(fx.receipt().get("rationale"), fx.receipt())
        self.assertEqual(len(fx.journal()), 1, "the link is no longer true: asked")

    def tracked_fixture(self) -> tuple[Fixture, str]:
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.commit_all("register")
        add = "\n## DEC-0002 choose the store per workspace\n\nBecause tenants.\n"
        with (fx.repo / "docs/DECISIONS.md").open("a", encoding="utf-8") as fh:
            fh.write(add)
        fx.wrote(add)
        return fx, add

    def test_the_register_deleted(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.wrote(REGISTER, tool="Write")
        self.linked(fx)
        (fx.repo / "docs/DECISIONS.md").unlink()
        self.asked_next(fx)

    def test_the_addition_checked_out(self):
        fx, _ = self.tracked_fixture()
        self.linked(fx)
        _git(fx.repo, "checkout", "--", "docs/DECISIONS.md")
        self.asked_next(fx)

    def test_the_heading_rewritten_by_another_writer(self):
        fx, _ = self.tracked_fixture()
        self.linked(fx)
        p = fx.repo / "docs/DECISIONS.md"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "DEC-0002 choose the store per workspace", "DEC-0002 WITHDRAWN"), encoding="utf-8")
        self.asked_next(fx)

    def test_committed_then_reverted(self):
        fx, _ = self.tracked_fixture()
        self.linked(fx)
        fx.commit_all("dec 2")
        _git(fx.repo, "revert", "--no-edit", "HEAD")
        self.asked_next(fx)

    def test_the_same_session_in_another_repository(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.wrote(REGISTER, tool="Write")
        self.linked(fx)
        # A second watched repository, registered beside the first; same session.
        other = fx.work / "other"
        other.mkdir()
        _git(other, "init", "-q", "-b", "main")
        _git(other, "config", "user.email", "fixture@example.invalid")
        _git(other, "config", "user.name", "Fixture")
        (other / "README.md").write_text("other\n", encoding="utf-8")
        _git(other, "add", "-A")
        _git(other, "commit", "-qm", "first")
        (other / "work.py").write_text("x = 1\n", encoding="utf-8")
        _git(other, "add", "-N", "work.py")       # tracked content: what is asked about
        # The other repository even holds a register with the same anchor.
        (other / "docs").mkdir()
        (other / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        reg = pathlib.Path(fx.env["OBSERVATORY_REGISTRY"])
        repos = json.loads((reg / "repositories.json").read_text())
        repos["repositories"].append({"id": "repository:fixture/other", "name_with_owner": "fixture/other",
                                      "host": "github", "local": {"path": _git(other, "rev-parse", "--show-toplevel").strip()}})
        (reg / "repositories.json").write_text(json.dumps(repos))
        projects = json.loads((reg / "projects.json").read_text())
        projects["projects"].append(dict(projects["projects"][0], id="project:other", name="other"))
        (reg / "projects.json").write_text(json.dumps(projects))
        rels = json.loads((reg / "relations.json").read_text())
        rels["relations"].append({"id": "relation:other", "type": "implemented_by",
                                  "from": "project:other", "to": "repository:fixture/other", "source_refs": []})
        (reg / "relations.json").write_text(json.dumps(rels))
        fx.repo = other
        fx.stop()
        self.assertIsNone(fx.receipt().get("rationale"), fx.receipt())
        self.assertEqual(len(fx.journal()), 1)

    def test_a_link_another_writer_put_in_the_evidence_is_not_carried(self):
        # An `observatory_record` revision (no Stop-hook provenance) whose evidence
        # claims `kind: rationale` is not this hook's link and is never carried.
        fx = Fixture()
        fx.stop()
        fx.prompt()
        mid = fx.journal()[0]["memoryId"]
        code = ("import sys, json; sys.path.insert(0, '.')\n"
                "from store import db, ledger as L\n"
                "c = db.connect()\n"
                "r = c.execute(\"SELECT revision, statement, evidence_json FROM ledger WHERE memory_id=? "
                "ORDER BY revision DESC LIMIT 1\", (sys.argv[1],)).fetchone()\n"
                "ev = json.loads(r[2]) + [{'uri': 'file:README.md', 'kind': 'rationale', 'repo': 'repository:fixture/watched', 'anchor': 'a watched project'}]\n"
                "L.append(c, owner='agent:claude-code', kind='session', memory_id=sys.argv[1],"
                " expected_revision=r[0], statement=r[1], evidence=ev, session_id='sess-1', state='proposed')\n"
                "c.commit()\n")
        p = subprocess.run([PY, "-c", code, mid], cwd=ROOT, env=fx.env, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        fx.change("later.py")
        fx.stop()
        self.assertIsNone(fx.receipt().get("rationale"), fx.receipt())

    def test_a_new_decision_replaces_a_stale_anchor(self):
        fx, _ = self.tracked_fixture()
        self.linked(fx)
        self.assertIn("DEC-0002", fx.receipt()["rationale"]["anchor"])
        add = "\n## DEC-0003 a newer decision\n\nBecause it changed.\n"
        with (fx.repo / "docs/DECISIONS.md").open("a", encoding="utf-8") as fh:
            fh.write(add)
        fx.wrote(add, use_id="toolu_second")
        fx.stop()
        self.assertIn("DEC-0003", fx.receipt()["rationale"]["anchor"])
        self.assertEqual(fx.receipt()["rationale"]["source"], "decision-register")

    MADR = ("# Decisions\n\n## DEC-0001 one store per workspace\n\n### Context\n\nTenants.\n\n"
            "### Decision\n\nOne store.\n\n### Consequences\n\nMore files.\n")

    def madr_fixture(self) -> Fixture:
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(self.MADR, encoding="utf-8")
        fx.commit_all("register")
        add = ("\n## DEC-0002 measure delivered asks\n\n### Context\n\nHeadless.\n\n"
               "### Decision\n\nDelivered only.\n\n### Consequences\n\nA journal row.\n")
        with (fx.repo / "docs/DECISIONS.md").open("a", encoding="utf-8") as fh:
            fh.write(add)
        fx.wrote(add)
        fx.stop()
        self.assertEqual(fx.receipt()["rationale"]["anchor"], "DEC-0002 measure delivered asks",
                         "the entry's own heading, not a repeated subheading")
        return fx

    def test_a_madr_entry_checked_out(self):
        fx = self.madr_fixture()
        _git(fx.repo, "checkout", "--", "docs/DECISIONS.md")
        self.asked_next(fx)

    def rewrite(self, fx: Fixture, old: str, new: str) -> None:
        p = fx.repo / "docs/DECISIONS.md"
        text = p.read_text(encoding="utf-8")
        assert old in text
        p.write_text(text.replace(old, new), encoding="utf-8")

    def test_a_struck_withdrawn_heading(self):
        fx = self.madr_fixture()
        self.rewrite(fx, "## DEC-0002 measure delivered asks",
                     "## ~~DEC-0002 measure delivered asks~~ WITHDRAWN")
        self.asked_next(fx)

    def test_the_anchor_left_only_in_a_comment(self):
        fx = self.madr_fixture()
        self.rewrite(fx, "## DEC-0002 measure delivered asks",
                     "<!-- ## DEC-0002 measure delivered asks -->\n## DEC-0002 (removed)")
        self.asked_next(fx)

    def test_the_anchor_left_only_in_a_code_fence(self):
        fx = self.madr_fixture()
        self.rewrite(fx, "## DEC-0002 measure delivered asks",
                     "```\n## DEC-0002 measure delivered asks\n```\n## DEC-0002 (removed)")
        self.asked_next(fx)

    def test_the_anchor_left_only_in_an_index_line(self):
        fx = self.madr_fixture()
        self.rewrite(fx, "## DEC-0002 measure delivered asks",
                     "- DEC-0002 measure delivered asks (superseded)")
        self.asked_next(fx)

    def test_a_duplicated_anchor_is_not_still_true(self):
        fx = self.madr_fixture()
        with (fx.repo / "docs/DECISIONS.md").open("a", encoding="utf-8") as fh:
            fh.write("\n## DEC-0002 measure delivered asks\n\nA second, conflicting copy.\n")
        self.asked_next(fx)

    def test_a_register_replaced_by_a_symlink(self):
        fx = self.madr_fixture()
        other = fx.repo / "docs/elsewhere.md"
        other.write_text("## DEC-0002 measure delivered asks\n", encoding="utf-8")
        (fx.repo / "docs/DECISIONS.md").unlink()
        (fx.repo / "docs/DECISIONS.md").symlink_to("elsewhere.md")
        self.asked_next(fx)

    def test_a_register_linked_outside_the_checkout(self):
        fx = self.madr_fixture()
        outside = fx.work / "outside.md"
        outside.write_text("## DEC-0002 measure delivered asks\n", encoding="utf-8")
        (fx.repo / "docs/DECISIONS.md").unlink()
        (fx.repo / "docs/DECISIONS.md").symlink_to(outside)
        self.asked_next(fx)

    def test_an_anchor_near_the_end_of_a_large_register(self):
        # Over 512 KB, the entry at its very end: the whole file is read, so the link
        # is still true and carried.
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        bulk = "# Decisions\n\n" + "".join(f"## DEC-{i:05d} old\n\nWhy {i} {'x' * 60}.\n\n"
                                            for i in range(1, 7001))
        self.assertGreater(len(bulk), 512 * 1024)
        (fx.repo / "docs/DECISIONS.md").write_text(bulk, encoding="utf-8")
        fx.commit_all("register")
        add = "\n## DEC-99999 the newest entry\n\nWhy.\n"
        with (fx.repo / "docs/DECISIONS.md").open("a", encoding="utf-8") as fh:
            fh.write(add)
        fx.wrote(add)
        self.linked(fx)
        fx.change("later.py")
        fx.stop()
        self.assertEqual(fx.receipt()["rationale"]["source"], "carried")
        self.assertEqual(fx.journal(), [])

    H = "## DEC-0002 measure delivered asks"

    def test_the_heading_inside_a_multi_line_comment(self):
        fx = self.madr_fixture()
        self.rewrite(fx, self.H, "<!--\n" + self.H + "\n-->")
        self.asked_next(fx)

    def test_the_heading_inside_a_nested_four_backtick_fence(self):
        fx = self.madr_fixture()
        self.rewrite(fx, self.H, "````\n```\n" + self.H + "\n```\n````")
        self.asked_next(fx)

    def test_tildes_do_not_close_a_backtick_fence(self):
        fx = self.madr_fixture()
        self.rewrite(fx, self.H, "```\n~~~\n" + self.H + "\n```")
        self.asked_next(fx)

    def test_the_heading_inside_a_tilde_fence(self):
        fx = self.madr_fixture()
        self.rewrite(fx, self.H, "~~~\n" + self.H + "\n~~~")
        self.asked_next(fx)

    def test_an_unclosed_fence_runs_to_the_end(self):
        fx = self.madr_fixture()
        self.rewrite(fx, self.H, "```\n" + self.H)
        self.asked_next(fx)

    def test_a_heading_indented_as_code(self):
        fx = self.madr_fixture()
        self.rewrite(fx, self.H, "    " + self.H)
        self.asked_next(fx)

    def test_a_line_separator_does_not_split_a_line(self):
        fx = self.madr_fixture()
        self.rewrite(fx, self.H, "WITHDRAWN\u2028" + self.H)
        self.asked_next(fx)

    def test_a_line_ending_change_drops_the_carried_link(self):
        # A byte-level check: converting the register to CRLF is an edit, so the
        # carried link no longer holds (a fresh attribution may link it again).
        fx = self.madr_fixture()
        p = fx.repo / "docs/DECISIONS.md"
        p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
        self.asked_next(fx)
        self.assertIs(fx.receipt().get("recorded"), True, "dropped cleanly, not a fault")

    # ---- round 9: structures the scanner does not model; the byte check covers them

    def test_a_list_item_fence_around_the_heading(self):
        fx = self.madr_fixture()
        self.rewrite(fx, self.H, "- ```\n" + self.H + "\n  ```")
        self.asked_next(fx)

    def test_a_list_item_comment_around_the_heading(self):
        fx = self.madr_fixture()
        self.rewrite(fx, self.H, "- <!--\n" + self.H)
        p = fx.repo / "docs/DECISIONS.md"
        p.write_text(p.read_text(encoding="utf-8") + "-->\n", encoding="utf-8")
        self.asked_next(fx)

    def test_a_list_item_opened_above_the_heading(self):
        # The entry's own bytes are untouched; only a line above changed.
        fx = self.madr_fixture()
        self.rewrite(fx, self.H, "- withdrawn:\n\n    ```\n" + self.H)
        self.asked_next(fx)

    def test_withdrawn_appended_past_the_display_length(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        head = "## DEC-0001 " + "a" * 100
        body = "# Decisions\n\n" + head + "\n\nWhy.\n"
        (fx.repo / "docs/DECISIONS.md").write_text(body, encoding="utf-8")
        fx.wrote(body, tool="Write")
        self.linked(fx)
        self.assertEqual(fx.receipt()["rationale"]["line"], head)
        p = fx.repo / "docs/DECISIONS.md"
        p.write_text(body.replace(head, head + " " + "b" * 40 + " WITHDRAWN"), encoding="utf-8")
        self.asked_next(fx)

    def test_a_heading_longer_than_the_link_limit_is_never_linked(self):
        import record_turn
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        body = "# Decisions\n\n## DEC-0001 " + "a" * record_turn.LINK_HEADING_CHARS + "\n\nWhy.\n"
        (fx.repo / "docs/DECISIONS.md").write_text(body, encoding="utf-8")
        fx.wrote(body, tool="Write")
        fx.stop()
        self.assertIsNone(fx.receipt().get("rationale"))
        self.assertEqual(len(fx.journal()), 1)

    def test_a_comment_opened_with_the_short_form(self):
        fx = self.madr_fixture()
        self.rewrite(fx, self.H, "<!-->\n" + self.H)
        self.asked_next(fx)

    def test_an_html_block_around_the_heading(self):
        fx = self.madr_fixture()
        self.rewrite(fx, self.H, "<details>\n\n" + self.H)
        self.asked_next(fx)

    def test_a_setext_duplicate_of_the_heading(self):
        fx = self.madr_fixture()
        p = fx.repo / "docs/DECISIONS.md"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "# Decisions\n", "# Decisions\n\nDEC-0002 measure delivered asks\n---------\n", 1),
            encoding="utf-8")
        self.asked_next(fx)

    def test_a_backtick_fence_with_an_info_string(self):
        fx = self.madr_fixture()
        self.rewrite(fx, self.H, "```markdown\n" + self.H)
        self.asked_next(fx)

    def test_a_one_character_body_edit(self):
        fx = self.madr_fixture()
        self.rewrite(fx, "Delivered only.", "Delivered only!")
        self.asked_next(fx)

    def test_an_unchanged_entry_with_a_later_entry_appended_stays_carried(self):
        fx = self.madr_fixture()
        with (fx.repo / "docs/DECISIONS.md").open("a", encoding="utf-8") as fh:
            fh.write("\n## DEC-0003 someone else's later decision\n\nWhy.\n")
        fx.change("later.py")
        fx.stop()
        self.assertEqual(fx.receipt()["rationale"]["source"], "carried")
        self.assertEqual(fx.journal(), [])

    def test_a_symlinked_parent_directory(self):
        fx = self.madr_fixture()
        docs = fx.repo / "docs"
        real = fx.repo / "real-docs"
        docs.rename(real)
        docs.symlink_to("real-docs")
        self.asked_next(fx)

    def test_a_fifo_in_place_of_the_register(self):
        fx = self.madr_fixture()
        p = fx.repo / "docs/DECISIONS.md"
        p.unlink()
        os.mkfifo(p)
        fx.change("later.py")
        took = fx.timed_stop(limit=30)
        self.assertLess(took, 10, "a FIFO must never block the hook")
        self.assertIsNone(fx.receipt().get("rationale"))
        self.assertEqual(len(fx.journal()), 1)

    def test_a_hard_linked_register(self):
        fx = self.madr_fixture()
        os.link(fx.repo / "docs/DECISIONS.md", fx.work / "second-name.md")
        self.asked_next(fx)

    def test_after_a_drop_only_a_later_write_links_again(self):
        fx = self.madr_fixture()
        self.rewrite(fx, "Delivered only.", "Delivered only, edited by another writer.")
        self.asked_next(fx, "first.py")
        # The session writes the entry again: that later write links it afresh.
        fx.wrote("## DEC-0002 measure delivered asks\n\nDelivered only, edited by another writer.\n",
                 use_id="toolu_again")
        fx.change("second.py")
        fx.stop()
        self.assertEqual((fx.receipt().get("rationale") or {}).get("source"), "decision-register")

    def test_a_link_from_before_the_byte_check_is_not_carried(self):
        # A hook-written link with only an anchor (no raw line, no digest), as links
        # were written before this check: it is never carried.
        fx = self.madr_fixture()
        mid = fx.receipt()["memoryId"]
        code = ("import sys, json; sys.path.insert(0, '.')\n"
                "from store import db, ledger as L\n"
                "c = db.connect()\n"
                "r = c.execute(\"SELECT revision, statement, evidence_json FROM ledger WHERE memory_id=? "
                "ORDER BY revision DESC LIMIT 1\", (sys.argv[1],)).fetchone()\n"
                "ev = [e for e in json.loads(r[2]) if e.get('kind') != 'rationale'] + [{'uri': "
                "'file:docs/DECISIONS.md', 'kind': 'rationale', 'repo': 'repository:fixture/watched', "
                "'anchor': 'DEC-0002 measure delivered asks'}]\n"
                "L.append(c, owner='agent:claude-code', kind='session', memory_id=sys.argv[1],"
                " expected_revision=r[0], statement=r[1], evidence=ev, session_id='sess-1', state='proposed',"
                " provenance=[{'source': 'observatory-log/Stop', 'rationale': 'decision-register'}])\n"
                "c.commit()\n")
        p = subprocess.run([PY, "-c", code, mid], cwd=ROOT, env=fx.env, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        self.asked_next(fx)
        self.assertIs(fx.receipt().get("recorded"), True, "dropped cleanly, not a fault")

    def test_a_register_over_the_read_limit_asks(self):
        import record_turn
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        filler = "line of an older decision\n" * (record_turn.CARRY_READ_LIMIT // 26 + 10)
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER + filler, encoding="utf-8")
        self.assertGreater((fx.repo / "docs/DECISIONS.md").stat().st_size, 8 << 20)
        fx.wrote(REGISTER, tool="Write")
        fx.stop()
        self.assertIsNone(fx.receipt().get("rationale"), fx.receipt())
        self.assertEqual(len(fx.journal()), 1)

    # ---- round 10: no Markdown boundary is guessed; the whole file is compared

    def fenced_fixture(self, inner: str) -> Fixture:
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(self.MADR, encoding="utf-8")
        fx.commit_all("register")
        add = ("\n## DEC-0002 measure delivered asks\n\nSet it up:\n\n" + inner +
               "\nThen the body that matters.\n")
        with (fx.repo / "docs/DECISIONS.md").open("a", encoding="utf-8") as fh:
            fh.write(add)
        fx.wrote(add)
        self.linked(fx)
        return fx

    def test_an_edit_below_a_hash_line_in_a_sh_fence(self):
        fx = self.fenced_fixture("```sh\n# install the hook\nmake install\n```\n")
        self.rewrite(fx, "Then the body that matters.", "Then the body that changed.")
        self.asked_next(fx)

    def test_an_edit_below_a_hash_line_in_a_python_fence(self):
        fx = self.fenced_fixture("```python\n# the counter\nn = 0\n```\n")
        self.rewrite(fx, "n = 0", "n = 1")
        self.asked_next(fx)

    def test_an_edit_below_a_hash_line_in_a_comment(self):
        fx = self.fenced_fixture("<!--\n# reviewer note\nkeep this\n-->\n")
        self.rewrite(fx, "Then the body that matters.", "Then the body that changed.")
        self.asked_next(fx)

    def test_an_append_without_a_heading_asks(self):
        fx = self.madr_fixture()
        with (fx.repo / "docs/DECISIONS.md").open("a", encoding="utf-8") as fh:
            fh.write("\nOne more paragraph added under DEC-0002 by someone else.\n")
        self.asked_next(fx)

    def test_an_append_of_a_deeper_heading_asks(self):
        fx = self.madr_fixture()
        with (fx.repo / "docs/DECISIONS.md").open("a", encoding="utf-8") as fh:
            fh.write("\n### Amendment\n\nChanges DEC-0002.\n")
        self.asked_next(fx)

    def test_an_edit_to_another_entry_above_asks(self):
        fx = self.madr_fixture()
        self.rewrite(fx, "One store.", "Two stores.")
        self.asked_next(fx)

    def test_an_append_after_an_unclosed_fence_is_not_carried(self):
        fx = self.fenced_fixture("```sh\n# install the hook\n")
        with (fx.repo / "docs/DECISIONS.md").open("a", encoding="utf-8") as fh:
            fh.write("\n## DEC-0003 later\n\nWhy.\n")
        self.asked_next(fx)

    def test_a_drop_is_remembered_for_every_later_turn(self):
        fx = self.madr_fixture()
        self.rewrite(fx, "Delivered only.", "Delivered only, edited by another writer.")
        for name in ("one.py", "two.py", "three.py"):
            fx.change(name)
            fx.stop()
            self.assertIsNone(fx.receipt().get("rationale"), (name, fx.receipt().get("rationale")))
        ev = json.loads(fx.ledger("SELECT evidence_json FROM ledger WHERE kind='session'"
                                  " ORDER BY revision DESC")[0][0])
        self.assertTrue([e for e in ev if e.get("kind") == "rationale-dropped"], ev)

    def test_a_link_still_true_is_carried(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.wrote(REGISTER, tool="Write")
        self.linked(fx)
        fx.change("later.py")
        fx.stop()
        self.assertEqual(fx.receipt()["rationale"]["source"], "carried")
        self.assertEqual(fx.journal(), [])


class NotARationale(unittest.TestCase):
    """What the review found the skip fired on — each beside a real code change."""

    def asked(self, fx: Fixture, transcript: bool = True) -> None:
        p = fx.stop(transcript=transcript)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        self.assertEqual(len(fx.journal()), 1, f"asked: {fx.receipt()}")
        self.assertIsNone(fx.receipt().get("rationale"), fx.receipt())
        ev = json.loads(fx.ledger("SELECT evidence_json FROM ledger WHERE kind='session'")[0][0])
        self.assertFalse([e for e in ev if e.get("kind") == "rationale"], ev)

    def test_deleting_the_register(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.commit_all()
        (fx.repo / "docs/DECISIONS.md").unlink()
        fx.change()
        fx.wrote(REGISTER)                   # even a transcript naming its old text
        self.asked(fx)

    def test_a_whitespace_only_edit(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.commit_all()
        (fx.repo / "docs/DECISIONS.md").write_text(
            REGISTER.replace("## DEC-0001", "##   DEC-0001") + "\n\n", encoding="utf-8")
        fx.change()
        fx.wrote("##   DEC-0001 one record per decision")
        self.asked(fx)

    def test_an_empty_untracked_register(self):
        fx = Fixture()
        (fx.repo / "src").mkdir()
        (fx.repo / "src/decisions.md").write_text("\n  \n", encoding="utf-8")
        fx.change()
        self.asked(fx)

    def test_a_vendored_libraries_adr(self):
        fx = Fixture()
        adr = fx.repo / "vendor/somelib/docs/adr/0001-their-choice.md"
        adr.parent.mkdir(parents=True)
        adr.write_text("# 0001 Their choice\n\nNot ours.\n", encoding="utf-8")
        fx.change()
        fx.wrote("# 0001 Their choice", tool="Write",
                 rel="vendor/somelib/docs/adr/0001-their-choice.md")
        self.asked(fx)

    def test_another_sessions_register_edit_in_the_same_checkout(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.wrote(REGISTER, session="someone-else", tool="Write")
        fx.change()
        self.asked(fx)

    def test_reading_the_register_is_not_writing_it(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.read_back(REGISTER)
        fx.change()
        self.asked(fx)

    def test_a_common_heading_in_a_pull_request_body(self):
        # Round 3: the session's `gh pr create` heredoc carried `## Context`, which the
        # register another session is writing also adds.
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text("# Decisions\n\n## Context\n\nWritten by session B.\n",
                                                  encoding="utf-8")
        fx.change()
        fx.bash("gh pr create --title t --body \"$(cat <<'EOF'\n## Context\n\nWhy this PR.\nEOF\n)\"")
        self.asked(fx)

    def test_a_read_only_grep_of_the_register(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text("# Decisions\n\nWritten by session B.\n",
                                                  encoding="utf-8")
        fx.change()
        fx.bash("grep 'Written by session B.' docs/DECISIONS.md")
        self.asked(fx)

    def test_an_ignored_ledger_line_this_session_did_not_write(self):
        # This session writes the ledger (a note), but the `stage:` line is not its.
        fx = Fixture()
        (fx.repo / ".gitignore").write_text(".task-pipeline/\n", encoding="utf-8")
        fx.commit_all("ignore")
        (fx.repo / ".task-pipeline").mkdir()
        (fx.repo / ".task-pipeline/run.md").write_text(
            "# run\n\n- stage: plan approved by another session\n", encoding="utf-8")
        fx.change()
        fx.bash("echo '- note: picked up the plan' >> .task-pipeline/run.md")
        self.asked(fx)

    def test_a_shell_write_to_another_file_with_the_same_text(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.change()
        fx.bash("cat > notes.md <<'EOF'\n" + REGISTER + "EOF")
        self.asked(fx)

    def test_a_small_edit_to_a_register_another_session_wrote(self):
        # The session's Edit does target the register, but its text does not carry
        # the addition: the heading and most lines are another session's.
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(
            "# Decisions\n\n## DEC-0009 chosen by session B\n\nWritten by session B.\n\n"
            "Session B's second paragraph.\n\nfixed a typo here\n", encoding="utf-8")
        fx.change()
        fx.wrote("fixed a typo here")
        self.asked(fx)

    def test_a_failed_write_to_the_register(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.wrote(REGISTER, tool="Write", use_id="toolu_failed", failed=True)
        fx.change()
        self.asked(fx)

    def test_a_subagents_write_to_the_register(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.wrote(REGISTER, tool="Write", sidechain=True)
        fx.change()
        self.asked(fx)

    def test_a_ledger_line_found_only_in_a_call_that_wrote_elsewhere(self):
        # This session wrote the ledger (a note) AND, in another call, wrote the
        # stage line's text into a different file. Only ledger-writing calls count.
        fx = Fixture()
        (fx.repo / ".gitignore").write_text(".task-pipeline/\n", encoding="utf-8")
        fx.commit_all("ignore")
        (fx.repo / ".task-pipeline").mkdir()
        (fx.repo / ".task-pipeline/run.md").write_text(
            "# run\n\n- stage: plan approved by the reviewer\n", encoding="utf-8")
        fx.change()
        fx.bash("echo '- note: picked up' >> .task-pipeline/run.md")
        fx.bash("echo 'plan approved by the reviewer' > notes.txt")
        self.asked(fx)

    def test_a_register_write_older_than_the_transcript_tail(self):
        import record_turn
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.wrote(REGISTER, tool="Write")
        fx.filler(5)                                     # the real 4 MB tail, passed
        facts = record_turn.diff_facts(fx.repo)
        self.assertIsNone(record_turn.rationale_of(fx.repo, facts, str(fx.transcript("sess-1"))),
                          "only the tail is read")

    def test_a_read_only_sed_of_the_register(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.change()
        fx.bash("sed -n '1,40p' docs/DECISIONS.md && echo '" + REGISTER.replace("'", "") + "'")
        self.asked(fx)

    def test_a_heredoc_over_the_scan_limit_into_the_register(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        body = REGISTER + "x" * 65536            # the documented limit, not the constant
        (fx.repo / "docs/DECISIONS.md").write_text(body, encoding="utf-8")
        fx.bash("cat > docs/DECISIONS.md <<'EOF'\n" + body + "\nEOF")
        self.asked(fx)

    def test_a_freshly_written_register_that_is_a_symlink_inside(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/real.md").write_text(REGISTER, encoding="utf-8")
        (fx.repo / "docs/DECISIONS.md").symlink_to("real.md")
        fx.wrote(REGISTER, tool="Write")
        self.asked(fx)

    def test_a_freshly_written_register_that_is_a_symlink_outside(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        outside = fx.work / "outside.md"
        outside.write_text(REGISTER, encoding="utf-8")
        (fx.repo / "docs/DECISIONS.md").symlink_to(outside)
        fx.wrote(REGISTER, tool="Write")
        self.asked(fx)

    def test_a_ledger_that_is_a_symlink(self):
        fx = Fixture()
        (fx.repo / ".gitignore").write_text(".task-pipeline/\n", encoding="utf-8")
        fx.commit_all("ignore")
        (fx.repo / ".task-pipeline").mkdir()
        target = fx.work / "elsewhere-run.md"
        target.write_text("# run\n\n- decision: keep the slot, add a journal\n", encoding="utf-8")
        (fx.repo / ".task-pipeline/run.md").symlink_to(target)
        fx.change()
        fx.bash("printf '%s\\n' '- decision: keep the slot, add a journal' >> .task-pipeline/run.md")
        self.asked(fx)

    def test_heading_syntax_the_scanner_rejects_is_never_matched_by_lines(self):
        # Everything sits in an unclosed fence: no heading, and no line-majority.
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        body = "```\n## DEC-0009 inside a fence\n\nBecause of a reason here.\n"
        (fx.repo / "docs/DECISIONS.md").write_text(body, encoding="utf-8")
        fx.wrote(body, tool="Write")
        self.asked(fx)

    def test_no_transcript_means_no_attribution(self):
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.change()
        self.asked(fx, transcript=False)


class WithinTheBudget(unittest.TestCase):
    """The attribution runs AFTER the record, inside `ATTRIBUTION_SECONDS`. Round 3
    measured the old per-line scan at 2.4 s for 100 register lines and a killed Stop
    hook (no record) at 1,500; these cases bound the whole Stop."""

    LIMIT = 8.0       # the 2 s budget, plus interpreter start, git and the append

    def test_a_1500_line_register_and_a_large_transcript(self):
        fx = Fixture()
        body = "# Decisions\n\n" + "".join(
            f"## DEC-{i:04d} a decision about part {i}\n\nBecause of constraint {i}.\n\n"
            for i in range(1, 501))
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(body, encoding="utf-8")
        self.assertGreaterEqual(len([l for l in body.splitlines() if l.strip()]), 1000)
        fx.filler(24)
        fx.wrote(body, tool="Write")
        took = fx.timed_stop(limit=60)
        self.assertLess(took, self.LIMIT, f"the Stop took {took:.1f} s")
        self.assertEqual(len(fx.ledger("SELECT 1 FROM ledger WHERE kind='session'")) >= 1, True,
                         "the record exists")
        self.assertEqual((fx.receipt().get("rationale") or {}).get("ref"), "docs/DECISIONS.md")
        self.assertEqual(fx.journal(), [])

    def test_a_400_line_ignored_ledger(self):
        fx = Fixture()
        (fx.repo / ".gitignore").write_text(".task-pipeline/\n", encoding="utf-8")
        fx.commit_all("ignore")
        (fx.repo / ".task-pipeline").mkdir()
        (fx.repo / ".task-pipeline/run.md").write_text("# run\n\n" + "".join(
            f"- stage: step {i} of the plan done\n" for i in range(400)), encoding="utf-8")
        fx.change()
        fx.filler(24)
        fx.bash("echo '- note: started' >> .task-pipeline/run.md")
        took = fx.timed_stop(limit=60)
        self.assertLess(took, self.LIMIT, f"the Stop took {took:.1f} s")
        self.assertTrue(fx.ledger("SELECT 1 FROM ledger WHERE kind='session'"), "the record exists")
        self.assertEqual(len(fx.journal()), 1, "no stage line was this session's: asked")

    def test_a_long_single_line_command_dense_with_sed(self):
        # Round 5: a one-line printf runbook mentioning `sed` thousands of times made
        # the old `sed -i` pattern quadratic — 30 s for 231 KB. Now linear, and a
        # command longer than COMMAND_SCAN_BYTES is not scanned at all.
        fx = Fixture()
        (fx.repo / ".gitignore").write_text(".task-pipeline/\n", encoding="utf-8")
        fx.commit_all("ignore")
        (fx.repo / ".task-pipeline").mkdir()
        (fx.repo / ".task-pipeline/run.md").write_text("# run\n- stage: intake done here\n",
                                                       encoding="utf-8")
        para = "Step: run sed with -E to rewrite the header, check sed output, then commit.\\n"
        fx.bash("printf '# Runbook\\n\\n" + para * 3200 + "' > docs/runbook.md")
        fx.bash("echo " + "sed " * 60000)
        fx.change()
        took = fx.timed_stop(limit=60)
        self.assertLess(took, self.LIMIT, f"the Stop took {took:.1f} s")
        self.assertTrue(fx.ledger("SELECT 1 FROM ledger WHERE kind='session'"))

    def test_shell_targets_is_linear_and_bounded(self):
        import record_turn, time as _t
        t0 = _t.monotonic()
        self.assertEqual(record_turn._shell_targets("echo " + "sed -i " * 9000 + "x"), [])
        self.assertLess(_t.monotonic() - t0, 1.0)
        self.assertEqual(record_turn.COMMAND_SCAN_BYTES, 65536, "the documented limit")
        self.assertIsNone(record_turn._shell_targets("x" * 65537))
        self.assertEqual(record_turn._shell_targets("sed -i 's/a/b/' docs/DECISIONS.md"),
                         ["docs/DECISIONS.md"])
        self.assertEqual(record_turn._shell_targets("grep -i x docs/DECISIONS.md"), [])

    def test_the_transcript_pass_itself_stops_at_the_deadline(self):
        import record_turn
        fx = Fixture()
        fx.filler(1)
        fx.wrote(REGISTER, tool="Write")
        with self.assertRaises(record_turn._OutOfTime):
            record_turn.session_writes(str(fx.transcript("sess-1")), fx.repo, deadline=0.0)
        self.assertTrue(record_turn.session_writes(str(fx.transcript("sess-1")), fx.repo,
                                                   deadline=float("inf")))

    def test_a_transcript_with_no_tool_calls_still_stops_at_the_deadline(self):
        # The line loop has its own deadline check: a large transcript of ordinary
        # messages, with no tool call to reach the per-call check, must stop too.
        import record_turn
        fx = Fixture()
        fx.filler(2)
        with self.assertRaises(record_turn._OutOfTime):
            record_turn.session_writes(str(fx.transcript("sess-1")), fx.repo, deadline=0.0)

    def test_the_diff_gets_only_the_time_left(self):
        import record_turn
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.commit_all("register")
        with (fx.repo / "docs/DECISIONS.md").open("a", encoding="utf-8") as fh:
            fh.write("## DEC-0002 x\n")
        facts = record_turn.diff_facts(fx.repo)
        with self.assertRaises(record_turn._OutOfTime):
            record_turn._added_lines(fx.repo, "docs/DECISIONS.md", facts, deadline=record_turn.time.monotonic() - 1)
        import subprocess as sp
        with mock.patch.object(record_turn.safe_git, "run",
                               side_effect=sp.TimeoutExpired(["git"], 0.1)):
            with self.assertRaises(record_turn._OutOfTime):
                record_turn._added_lines(fx.repo, "docs/DECISIONS.md", facts,
                                         deadline=record_turn.time.monotonic() + 5)

    def test_a_heading_with_a_long_whitespace_run(self):
        # Round 8: `'# a' + ' ' * 2600 + 'x'` ran the old heading regex past 20 s.
        import record_turn, time as _t
        line = "# a" + " " * 5000 + "x"
        t0 = _t.monotonic()
        for tail in ("", "#", "\t", " #"):
            record_turn._headings([line + tail] * 20)
        self.assertLess(_t.monotonic() - t0, 0.5)
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        body = "## DEC-0001 real\n\n" + line + "\n"
        (fx.repo / "docs/DECISIONS.md").write_text(body, encoding="utf-8")
        fx.wrote(body, tool="Write")
        took = fx.timed_stop(limit=60)
        self.assertLess(took, 2.0 if os.environ.get("CI") is None else self.LIMIT,
                        f"the Stop took {took:.1f} s")
        self.assertTrue(fx.ledger("SELECT 1 FROM ledger WHERE kind='session'"))

    def test_the_descriptor_is_checked_not_only_the_path(self):
        # The path check passed, then the file was swapped for a FIFO: the read
        # checks the opened descriptor and refuses, without blocking.
        import record_turn
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        os.mkfifo(fx.repo / "docs/swapped.md")
        with mock.patch.object(record_turn, "_register_path",
                               side_effect=lambda top, rel: top / rel):
            self.assertIsNone(record_turn._read_register(fx.repo, "docs/swapped.md", 1024))

    def test_the_read_gate_on_a_fifo_and_a_hard_link(self):
        import record_turn
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        os.mkfifo(fx.repo / "docs/fifo.md")
        self.assertIsNone(record_turn._read_register(fx.repo, "docs/fifo.md", 1024))
        (fx.repo / "docs/a.md").write_text("x\n", encoding="utf-8")
        self.assertEqual(record_turn._read_register(fx.repo, "docs/a.md", 1024), b"x\n")
        self.assertIsNone(record_turn._read_register(fx.repo, "docs/a.md", 1), "over the limit")
        os.link(fx.repo / "docs/a.md", fx.repo / "docs/b.md")
        self.assertIsNone(record_turn._read_register(fx.repo, "docs/a.md", 1024))

    def test_the_scanner_keeps_a_hash_inside_the_text(self):
        import record_turn
        self.assertEqual(record_turn._atx("## Use C#"), (2, "Use C#"))
        self.assertEqual(record_turn._atx("## Closed ##"), (2, "Closed"))
        self.assertEqual(record_turn._atx("#nospace"), None)
        self.assertEqual(record_turn._atx("\t## tabbed"), None)
        self.assertEqual(record_turn._atx("   ### three spaces"), (3, "three spaces"))

    def test_a_register_too_large_to_scan_in_time_asks(self):
        # DETERMINISTIC: the scan's own clock is injected, so the verdict does not
        # depend on how fast this machine is. It used to write 4M lines and assume
        # they could not be scanned in 2 s; a fast machine could. Here every budget
        # check costs 50 ms of a virtual clock, starting at the real one, and only
        # `_check` reads it (git's own timeouts keep the real clock).
        import record_turn
        import time as _t
        head = "# Decisions\n\n## DEC-0001 big\n\n"

        def attribute(lines: int) -> tuple[dict | None, int]:
            fx = Fixture()
            (fx.repo / "docs").mkdir()
            (fx.repo / "docs/DECISIONS.md").write_text(head + "a\n" * lines, encoding="utf-8")
            fx.wrote(head, tool="Write")
            facts = record_turn.diff_facts(fx.repo)
            start = _t.monotonic()
            checks = [0]

            def virtual_check(deadline: float) -> None:
                checks[0] += 1
                if start + 0.05 * checks[0] > deadline:
                    raise record_turn._OutOfTime()

            with mock.patch.object(record_turn, "_check", virtual_check):
                got = record_turn.rationale_of(
                    fx.repo, facts, str(fx.transcript("sess-1")),
                    deadline=start + record_turn.ATTRIBUTION_SECONDS)
            return got, checks[0]

        small, _ = attribute(10)
        self.assertIsNotNone(small, "a small register is linked under the same clock")
        self.assertEqual(small["anchor"], "DEC-0001 big")
        big, checks = attribute(400_000)
        self.assertIsNone(big, "past the budget the turn is asked about, never linked late")
        self.assertGreater(checks * 0.05, record_turn.ATTRIBUTION_SECONDS)

    def test_the_heading_scan_honours_the_deadline(self):
        import record_turn
        with self.assertRaises(record_turn._OutOfTime):
            record_turn._headings(["## x"] * 10, deadline=0.0)
        with self.assertRaises(record_turn._OutOfTime):
            record_turn._closed_at_end(["x"] * 10, deadline=0.0)

    def test_the_diff_timeout_never_exceeds_the_time_left(self):
        import record_turn
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.commit_all("register")
        with (fx.repo / "docs/DECISIONS.md").open("a", encoding="utf-8") as fh:
            fh.write("## DEC-0002 x\n")
        facts = record_turn.diff_facts(fx.repo)
        seen = []
        real = record_turn.safe_git.run

        def spy(args, **kw):
            seen.append(kw.get("timeout"))
            return real(args, **kw)
        deadline = record_turn.time.monotonic() + 1.5
        with mock.patch.object(record_turn.safe_git, "run", side_effect=spy):
            record_turn._added_lines(fx.repo, "docs/DECISIONS.md", facts, deadline)
        self.assertTrue(seen and seen[0] is not None and 0 < seen[0] <= 1.5, seen)

    def test_a_line_that_raises_recursion_error_is_skipped(self):
        import record_turn
        fx = Fixture()
        fx.wrote(REGISTER, tool="Write")
        real = record_turn.json.loads
        state = {"n": 0}

        def flaky(raw, *a, **k):
            state["n"] += 1
            if state["n"] == 1:
                raise RecursionError("too deep")
            return real(raw, *a, **k)
        with mock.patch.object(record_turn.json, "loads", side_effect=flaky):
            calls = record_turn.session_writes(str(fx.transcript("sess-1")), fx.repo, float("inf"))
        self.assertEqual(state["n"] >= 2, True)
        self.assertEqual(len(calls), 0, "the raising line (the call) is skipped, the pass goes on")

    def test_a_deeply_nested_line_does_not_abort_the_pass(self):
        import record_turn
        fx = Fixture()
        with fx.transcript("sess-1").open("a", encoding="utf-8") as fh:
            fh.write('{"type": "assistant", "x": "tool_use", "y": ' + "[" * 100000 + "]" * 100000 + "}\n")
        fx.wrote(REGISTER, tool="Write")
        calls = record_turn.session_writes(str(fx.transcript("sess-1")), fx.repo, deadline=float("inf"))
        self.assertEqual(len(calls), 1, "the nested line is skipped; the real call is kept")

    def test_the_deadline_also_stops_the_per_call_work(self):
        # A deadline that passes AFTER the line scan (a short transcript, one check at
        # line 0) and during the per-call extraction must still stop the pass.
        import record_turn
        fx = Fixture()
        for i in range(3):
            fx.bash(f"echo step {i} > out{i}.txt")
        clock = iter([0.0] + [100.0] * 50)
        with mock.patch.object(record_turn.time, "monotonic", lambda: next(clock)):
            with self.assertRaises(record_turn._OutOfTime):
                record_turn.session_writes(str(fx.transcript("sess-1")), fx.repo, deadline=50.0)

    def test_out_of_time_asks_and_the_record_stands(self):
        import record_turn
        fx = Fixture()
        (fx.repo / "docs").mkdir()
        (fx.repo / "docs/DECISIONS.md").write_text(REGISTER, encoding="utf-8")
        fx.wrote(REGISTER, tool="Write")
        facts = record_turn.diff_facts(fx.repo)
        got = record_turn.rationale_of(fx.repo, facts, str(fx.transcript("sess-1")), deadline=0.0)
        self.assertIsNone(got, "past the deadline: no rationale, so the turn is asked")
        self.assertIsNotNone(record_turn.rationale_of(fx.repo, facts, str(fx.transcript("sess-1"))))


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
        out = json.loads(p.stdout or "{}")
        self.assertIn("observatory_record(", out.get("systemMessage", ""), "never lost")
        self.assertIn("this turn", out["systemMessage"])
        self.assertNotIn("decision", out)

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

    def test_a_traversal_session_id_cannot_reach_a_file_outside(self):
        fx = Fixture()
        fx.pending_dir.mkdir(parents=True)
        outside = fx.pending_dir.parent / "escaped.json"
        outside.write_text(json.dumps({"asks": [{"at": "2099-01-01T00:00:00Z",
                                                  "memoryId": "mem:x", "context": "PLANTED"}]}),
                           encoding="utf-8")
        for sid in ("../escaped", "..", "a/../../escaped", "/etc/passwd"):
            p = fx.prompt(raw=json.dumps({"session_id": sid}))
            self.assertEqual((p.returncode, p.stdout.strip()), (0, ""), sid)
        self.assertTrue(outside.exists(), "the file outside the directory was not taken")
        p = subprocess.run([PY, str(ASK)], input=json.dumps(
            {"recorded": True, "hasWhy": False, "ask": True, "memoryId": "mem:y",
             "revision": 1, "session": "../escaped"}),
            text=True, capture_output=True, env=fx.env, timeout=60)
        self.assertNotIn("mem:y", outside.read_text(encoding="utf-8"),
                         "nor can the Stop hook write there")

    def test_a_fault_is_still_spoken_at_stop(self):
        p = subprocess.run([PY, str(ASK)], input=json.dumps(
            {"recorded": False, "fault": True, "reason": "IllegalTransition: x"}),
            text=True, capture_output=True, timeout=60)
        self.assertIn("could NOT record", json.loads(p.stdout)["systemMessage"])

    def test_a_real_fault_reaches_the_stop_hook(self):
        # Through the whole hook, not a hand-made payload: the recorder printed its
        # payload WITHOUT `fault` (only the receipt had it), so the branch above was
        # unreachable. A store that cannot be opened is a fault.
        fx = Fixture()
        (fx.work / "not-a-db").mkdir()
        fx.env["OBSERVATORY_DB"] = str(fx.work / "not-a-db")
        p = fx.stop()
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        self.assertIs(fx.receipt().get("fault"), True, fx.receipt())
        self.assertIn("could NOT record", json.loads(p.stdout)["systemMessage"])

    def test_a_quiet_turn_is_not_a_fault_on_the_wire(self):
        fx = Fixture()
        fx.stop()
        p = fx.stop()                                   # nothing moved since
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))
        self.assertIs(fx.receipt().get("fault"), False, fx.receipt())

    def test_an_old_recorder_without_the_ask_field_gets_the_old_stop_notice(self):
        # An engine that decides nothing about asking would inject into the model's
        # context every turn; it keeps the old Stop notice instead.
        fx = Fixture()
        p = subprocess.run([PY, str(ASK)], input=json.dumps(
            {"recorded": True, "hasWhy": False, "memoryId": "mem:old", "revision": 3,
             "project": "project:watched", "files": 1, "session": "sess-old"}),
            text=True, capture_output=True, env=fx.env, timeout=60)
        msg = json.loads(p.stdout)["systemMessage"]
        self.assertIn("mem:old", msg)
        self.assertNotIn("asked about once", msg)
        self.assertEqual(fx.context(fx.prompt("sess-old")), "", "nothing injected")


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
        self.assertEqual(sorted(doc["asks"][0]), ["at", "context", "journal", "memoryId", "revision"])
        self.assertEqual(pathlib.Path(doc["asks"][0]["journal"]).name, "why-asks.jsonl")

    def test_an_abandoned_ask_is_pruned_by_either_hook(self):
        fx = Fixture()
        fx.pending_dir.mkdir(parents=True)
        old = fx.pending_dir / "gone-session.json"
        old.write_text(json.dumps({"asks": []}), encoding="utf-8")
        os.utime(old, (1, 1))
        fx.stop()
        self.assertFalse(old.exists(), "pruned when the Stop hook leaves an ask")
        old.write_text(json.dumps({"asks": []}), encoding="utf-8")
        os.utime(old, (1, 1))
        fx.prompt()
        self.assertFalse(old.exists(), "and when the prompt hook delivers one")


class Pins(unittest.TestCase):
    """Guards round 4 found unpinned: each case fails when its guard is removed."""

    def test_a_symlinked_journal_is_never_written_through(self):
        fx = Fixture()
        target = fx.work / "elsewhere.txt"
        target.write_text("", encoding="utf-8")
        link_dir = fx.work / "linkdir"
        link_dir.mkdir()
        (link_dir / "why-asks.jsonl").symlink_to(target)
        fx.pending_dir.mkdir(parents=True)
        (fx.pending_dir / "sess-1.json").write_text(json.dumps({"asks": [
            {"at": "2099-01-01T00:00:00Z", "memoryId": "mem:s", "revision": 1,
             "context": "ask mem:s", "journal": str(link_dir / "why-asks.jsonl")}]}), encoding="utf-8")
        self.assertIn("mem:s", fx.context(fx.prompt()))
        self.assertEqual(target.read_text(encoding="utf-8"), "", "nothing written through the link")

    def test_rotation_rechecks_the_size_under_the_lock(self):
        # Another session rotated between our size check and our lock: the journal
        # is small again, and rotating it now would drop the previous generation.
        import why_asks, oslocks
        d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-why-recheck-"))
        journal, previous = d / "why-asks.jsonl", d / "why-asks.jsonl.1"
        journal.write_text(json.dumps({"at": "2026-10-14T00:00:00Z", "memoryId": "old"}) + "\n" * 3000,
                           encoding="utf-8")
        real = oslocks.flock

        def other_session_rotates_first(fd, op):
            if op & oslocks.LOCK_EX and journal.stat().st_size > 2000:
                os.replace(journal, previous)
                journal.write_text(json.dumps({"at": "2026-10-14T00:00:00Z", "memoryId": "new"}) + "\n",
                                   encoding="utf-8")
            return real(fd, op)
        with mock.patch.object(why_asks, "JOURNAL", journal), \
                mock.patch.object(why_asks, "PREVIOUS", previous), \
                mock.patch.object(why_asks, "ROTATE_BYTES", 2000), \
                mock.patch.object(oslocks, "flock", other_session_rotates_first):
            why_asks._rotate()
        self.assertIn('"old"', previous.read_text(encoding="utf-8"), "the previous generation kept")
        self.assertIn('"new"', journal.read_text(encoding="utf-8"))

    def test_every_journal_file_is_a_session_receipt_and_a_foreign_write(self):
        import paths, why_asks, workspace_upgrade, observatory as obs
        for f in (why_asks.JOURNAL, why_asks.PREVIOUS, why_asks.JOURNAL.with_name(why_asks.JOURNAL.name + ".lock")):
            rel = "store/raw/" + f.relative_to(paths.SCRATCH).as_posix()
            self.assertIn(rel, workspace_upgrade.SESSION_RECEIPTS)
            self.assertIn(rel, obs.FOREIGN_WRITES_IGNORED)

    def test_the_floor_is_twenty_delivered_not_twenty_asked(self):
        import build_findings
        got = build_findings.why_unanswered_findings(
            {"asked": 35, "delivered": 5, "pending": 30, "answered": 0, "rate": 0.0,
             "window": {"days": 7}})
        self.assertEqual(got, [])

    def test_asks_past_the_cap_are_not_marked_delivered_and_wait(self):
        fx = Fixture()
        fx.pending_dir.mkdir(parents=True)
        journal = fx.scratch / "why-asks.jsonl"
        asks = [{"at": "2099-01-01T00:00:00Z", "memoryId": f"mem:c{i}", "revision": 1,
                 "context": f"ask mem:c{i} " + "x" * 1800, "journal": str(journal)} for i in range(3)]
        (fx.pending_dir / "sess-1.json").write_text(json.dumps({"asks": asks}), encoding="utf-8")
        first = fx.context(fx.prompt())
        self.assertLessEqual(len(first), 4000)
        self.assertIn("mem:c0", first)
        self.assertNotIn("mem:c2", first)
        delivered = {r["memoryId"] for r in fx.journal(event="delivered")}
        self.assertNotIn("mem:c2", delivered, "an ask cut off by the cap is not delivered")
        second = fx.context(fx.prompt())
        self.assertIn("mem:c2", second, "it is handed over with the next prompt")
        self.assertIn("mem:c2", {r["memoryId"] for r in fx.journal(event="delivered")})


class Measured(unittest.TestCase):
    def test_a_headless_session_adds_nothing_to_the_denominator(self):
        # One prompt, one Stop, no further prompt: the ask never reached the model.
        fx = Fixture()
        fx.stop()
        w = fx.receipt().get("whyAsks") or {}
        self.assertEqual((w.get("asked"), w.get("delivered"), w.get("pending")), (1, 0, 1), w)
        self.assertIsNone(w.get("rate"), "nothing delivered is no rate, not a zero rate")

    def test_a_delivered_unanswered_ask_counts(self):
        fx = Fixture()
        fx.stop()
        fx.prompt()
        rows = fx.journal(event="delivered")
        self.assertEqual(len(rows), 1, fx.journal(event=None))
        (fx.repo / "again.txt").write_text("x\n", encoding="utf-8")
        fx.stop()
        w = fx.receipt()["whyAsks"]
        self.assertEqual((w["asked"], w["delivered"], w["pending"], w["answered"], w["rate"]),
                         (1, 1, 0, 0, 0.0), w)
        self.assertEqual(w["window"]["days"], 7)

    def test_the_measurement_counts_delivered_records_and_their_answers(self):
        import why_asks
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE ledger (memory_id TEXT, revision INT, why TEXT)")
        con.executemany("INSERT INTO ledger VALUES (?,?,?)",
                        [("m1", 1, None), ("m1", 2, "because"), ("m2", 1, None),
                         ("m3", 1, "  "), ("m4", 1, "answered though never delivered")])
        at = "2026-10-14T10:00:00Z"
        rows = [{"at": at, "memoryId": m} for m in ("m1", "m2", "m3", "m4", "m1")]
        rows += [{"at": at, "memoryId": m, "event": "delivered"} for m in ("m1", "m2", "m3")]
        rows.append({"at": "2020-01-01T00:00:00Z", "memoryId": "m-old"})
        rows.append({"at": "2099-01-01T00:00:00Z", "memoryId": "m-future"})
        got = why_asks.measure(con, rows=rows, now="2026-10-14T12:00:00Z")
        self.assertEqual((got["asked"], got["delivered"], got["pending"], got["answered"],
                          got["rate"]), (4, 3, 1, 1, 0.333), got)
        self.assertIsNone(why_asks.measure(con, rows=[], now="2026-10-14T12:00:00Z")["rate"])

    def test_the_journal_rotates_and_an_old_record_is_still_seen(self):
        import why_asks
        d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-why-rotate-"))
        journal = d / "why-asks.jsonl"
        with mock.patch.object(why_asks, "JOURNAL", journal), \
                mock.patch.object(why_asks, "PREVIOUS", d / "why-asks.jsonl.1"), \
                mock.patch.object(why_asks, "ROTATE_BYTES", 2000):
            self.assertTrue(why_asks.ask_once("mem:first")["ask"])
            with journal.open("a", encoding="utf-8") as fh:
                for i in range(40):
                    fh.write(json.dumps({"at": "2026-10-14T00:00:00Z", "memoryId": f"m{i}"}) + "\n")
            self.assertTrue(why_asks.ask_once("mem:second")["ask"])
            self.assertTrue((d / "why-asks.jsonl.1").is_file(), "rotated")
            self.assertLess(journal.stat().st_size if journal.exists() else 0, 2000)
            self.assertFalse(why_asks.ask_once("mem:first")["ask"], "still asked once")
            with journal.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": "2026-10-14T00:00:00Z", "memoryId": "pad"}) + "\n")
            self.assertTrue(why_asks.ask_once("mem:third")["ask"])
            self.assertTrue((d / "why-asks.jsonl.1").is_file(), "a small journal is not rotated")


class TheFinding(unittest.TestCase):
    def findings(self, delivered, answered, pending=0):
        import build_findings
        rate = None if not delivered else round(answered / delivered, 3)
        return build_findings.why_unanswered_findings(
            {"asked": delivered + pending, "delivered": delivered, "pending": pending,
             "answered": answered, "rate": rate,
             "window": {"days": 7, "since": "2026-10-07T00:00:00Z"}})

    def test_it_fires_below_ten_percent_with_twenty_delivered(self):
        out = self.findings(25, 1, pending=4)
        self.assertEqual([f["type"] for f in out], ["observatory.why_unanswered"])
        f = out[0]
        self.assertEqual(f["title_args"]["n"], 24)
        self.assertIn("1 of 25", f["detail"])
        self.assertIn("4 further ask(s) never reached the model", f["detail"])
        self.assertTrue(f["action"] and f["evidence"])

    def test_it_is_silent_otherwise(self):
        self.assertEqual(self.findings(19, 0), [], "too few delivered to judge")
        self.assertEqual(self.findings(30, 3), [], "10 % is the floor, not below it")
        self.assertEqual(self.findings(0, 0, pending=60), [],
                         "undelivered asks — headless sessions — are never unanswered")
        import build_findings
        self.assertEqual(build_findings.why_unanswered_findings(None), [])

    def test_the_type_is_labelled_and_translated(self):
        import finding_types
        self.assertIn("observatory.why_unanswered", finding_types.LABELS)
        ru = json.loads((ROOT / "dashboard/locales/ru.json").read_text(encoding="utf-8"))
        self.assertIn(finding_types.LABELS["observatory.why_unanswered"], ru)
        self.assertIn(self.findings(25, 1)[0]["title_id"], ru)

    def test_the_board_raises_it_from_the_store_and_the_journal(self):
        # The production path: `collect()` reads the journal and the workspace's own
        # store. 22 delivered asks, none answered; 30 more that were never delivered.
        import paths
        import build_findings
        from store import db as store_db
        store_db.connect().close()                       # the workspace store exists
        journal = paths.SCRATCH / "why-asks.jsonl"
        journal.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with journal.open("w", encoding="utf-8") as fh:
                for i in range(52):
                    fh.write(json.dumps({"at": now, "memoryId": f"mem:board{i}", "event": "asked"}) + "\n")
                for i in range(22):
                    fh.write(json.dumps({"at": now, "memoryId": f"mem:board{i}", "event": "delivered"}) + "\n")
            got = [f for f in build_findings.collect() if f["type"] == "observatory.why_unanswered"]
            self.assertEqual(len(got), 1, "the board carries the finding")
            self.assertEqual(got[0]["title_args"]["n"], 22)
            self.assertIn("30 further ask(s)", got[0]["detail"])
        finally:
            journal.unlink()


#: What a checkout that has been worked in for a while carries: files and a folder
#: nobody in the session touched (measured 2026-10-20: "4 files changed, +0/-0",
#: asked 35 times in one session).
STANDING = ("2026-10-07-pasted.txt", "2026-10-13-continued.txt", "notes.md", ".worktrees/wt/a.txt")


class OnlyThisTurnsChanges(unittest.TestCase):
    """A turn is recorded and asked about for what IT changed, not for what the tree
    already held: standing untracked files never ask, a tracked edit asks once, and
    every later turn revises the one record."""

    def clean_with_standing(self, *, baseline: bool) -> Fixture:
        fx = Fixture()
        fx.commit_all("the planted change, committed")          # nothing tracked is dirty
        for rel in STANDING:
            (fx.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (fx.repo / rel).write_text("standing\n", encoding="utf-8")
        if baseline:
            p = fx.session_start()
            self.assertEqual(p.returncode, 0, p.stderr[-300:])
        return fx

    def quiet(self, fx: Fixture, turns: int = 3) -> None:
        for _ in range(turns):
            p = fx.stop()
            self.assertEqual((p.returncode, p.stdout.strip()), (0, ""), p.stderr[-300:])
            r = fx.receipt()
            self.assertFalse(r.get("recorded"), r)
            self.assertFalse(r.get("fault"), r)

    def test_pre_existing_untracked_files_never_ask(self):
        for baseline in (True, False):
            with self.subTest(baseline=baseline):
                fx = self.clean_with_standing(baseline=baseline)
                self.quiet(fx)
                self.assertEqual(fx.journal(), [], "nothing asked")
                self.assertEqual(fx.context(fx.prompt()), "", "nothing pending")
                db = fx.work / "observatory.db"
                self.assertEqual(fx.ledger("SELECT memory_id FROM ledger WHERE kind='session'")
                                 if db.exists() else [], [],
                                 "nothing recorded either: the session changed nothing")
                self.assertIn("nothing changed", fx.receipt().get("reason", ""))

    def test_a_new_untracked_file_is_recorded_but_not_asked_about(self):
        fx = self.clean_with_standing(baseline=True)
        fx.stop()
        (fx.repo / "scratch-output.log").write_text("made this turn\n", encoding="utf-8")
        p = fx.stop()
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""), p.stderr[-300:])
        r = fx.receipt()
        self.assertTrue(r.get("recorded"), r)
        self.assertIs(r.get("ask"), False, r)
        self.assertIs(r.get("turnTracked"), False, r)
        self.assertEqual(r.get("files"), 1, f"the session's one new file, not the standing ones: {r}")
        self.assertEqual(fx.journal(), [], "an untracked file is not tracked content")

    def test_a_real_tracked_edit_asks_exactly_once(self):
        for baseline in (True, False):
            with self.subTest(baseline=baseline):
                fx = self.clean_with_standing(baseline=baseline)
                self.quiet(fx, 1)
                with (fx.repo / "README.md").open("a", encoding="utf-8") as fh:
                    fh.write("one more line\n")
                p = fx.stop()
                self.assertEqual((p.returncode, p.stdout.strip()), (0, ""), p.stderr[-300:])
                r = fx.receipt()
                self.assertTrue(r.get("recorded"), r)
                self.assertIs(r.get("ask"), True, r)
                self.assertEqual((r.get("files"), r.get("insertions"), r.get("deletions")),
                                 (1, 1, 0), f"README.md alone, standing files excluded: {r}")
                statement = fx.ledger("SELECT statement FROM ledger WHERE kind='session'")[0][0]
                self.assertIn("README.md", statement)
                for rel in STANDING:
                    self.assertNotIn(rel.split("/")[0], statement)
                self.assertEqual(len(fx.journal()), 1, "exactly one ask")
                delivered = fx.context(fx.prompt())
                self.assertIn("1 file changed, +1/-0", delivered)
                self.quiet(fx, 2)                               # no change: no more asks
                self.assertEqual(len(fx.journal()), 1)

    def test_repeated_turns_supersede_one_record(self):
        fx = self.clean_with_standing(baseline=True)
        for i in range(4):
            with (fx.repo / "README.md").open("a", encoding="utf-8") as fh:
                fh.write(f"turn {i}\n")
            p = fx.stop()
            self.assertEqual((p.returncode, p.stdout.strip()), (0, ""), p.stderr[-300:])
            fx.prompt()
            self.quiet(fx, 1)                                   # a turn that changed nothing
        rows = fx.ledger("SELECT memory_id, revision FROM ledger WHERE kind='session'"
                         " ORDER BY revision")
        self.assertEqual(len({r["memory_id"] for r in rows}), 1, "one record for the session")
        self.assertEqual([r["revision"] for r in rows], [1, 2, 3, 4], "superseded turn by turn")
        self.assertEqual(len(fx.journal()), 1, "asked once")

    def test_a_commit_made_in_the_turn_is_tracked_content_and_old_ones_are_not(self):
        fx = Fixture()
        fx.commit_all("the planted change, committed")
        bare = fx.work / "origin.git"
        _git(fx.work, "init", "-q", "--bare", str(bare))
        _git(fx.repo, "remote", "add", "upstream-fixture", str(bare))
        _git(fx.repo, "push", "-q", "-u", "upstream-fixture", "main")
        (fx.repo / "README.md").write_text("an unpushed commit from before the session\n",
                                           encoding="utf-8")
        _git(fx.repo, "commit", "-qam", "before the session")
        fx.session_start()
        self.quiet(fx, 2)                         # an unpushed commit the session did not make
        (fx.repo / "README.md").write_text("this turn's commit\n", encoding="utf-8")
        _git(fx.repo, "commit", "-qam", "this turn")
        p = fx.stop()
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""), p.stderr[-300:])
        r = fx.receipt()
        self.assertTrue(r.get("recorded"), r)
        self.assertIs(r.get("turnTracked"), True, r)
        self.assertIs(r.get("ask"), True, r)
        self.assertEqual(len(fx.journal()), 1)
        self.quiet(fx, 1)

    def test_the_state_names_no_path(self):
        fx = self.clean_with_standing(baseline=True)
        fx.change("secret-name-of-a-module.py")
        fx.stop()
        files = list(fx.turn_state().glob("*.json"))
        self.assertEqual(len(files), 1, files)
        text = files[0].read_text(encoding="utf-8")
        for needle in ("secret-name-of-a-module", "notes.md", str(fx.repo), "sess-1"):
            self.assertNotIn(needle, text)
        mode = stat.S_IMODE(files[0].stat().st_mode)
        self.assertEqual(mode, 0o600, oct(mode))
        self.assertEqual(stat.S_IMODE(fx.turn_state().stat().st_mode), 0o700)

    def test_a_git_call_past_the_budget_answers_empty_and_is_not_a_fault(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import record_turn
        record_turn._git_deadline = 0.0                         # long past
        record_turn._git_timed_out = False
        try:
            self.assertEqual(record_turn.git(ROOT, "rev-parse", "--git-dir"), "")
            self.assertTrue(record_turn._git_timed_out)
        finally:
            record_turn._git_deadline = None
            record_turn._git_timed_out = False
        self.assertTrue(any(k in "git timed out after 8 s" for k in record_turn.NOT_A_FAULT))


if __name__ == "__main__":
    unittest.main(verbosity=2)
