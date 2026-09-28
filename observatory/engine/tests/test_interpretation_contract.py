#!/usr/bin/env python3
"""What the model must return, and the two rules the schema could not enforce.

`agent/observe.py` sends a `strict: true` JSON schema, so an answer of the wrong
SHAPE is the provider's 400 rather than a parsing bug here. Two rules the schema
STATES in its own field descriptions cannot be expressed in it, and until now
they were enforced nowhere:

* *"Empty string when worth_recording is false"* — and its converse. A model
  answering `worth_recording: true` with an empty interpretation would have
  appended a BLANK conclusion: measured 2026-09-07, `L.append` accepted an empty
  statement and stored `''`. The indexer then skips it as "tombstoned or empty"
  and the review queue shows a blank line for an operator to adjudicate.
* *why_not is required "so a decline has to say something"* — a comment citing
  the first live run, which declined eight projects with an empty reason. But
  `required` lists KEYS; it does not forbid `""`. The fix that comment describes
  never fixed the defect it describes.

Neither can move into the schema: OpenAI's `strict` structured outputs accept a
subset of JSON Schema with no `if`/`then`, so a cross-field rule has no home
there. The runtime loop is the only place, which is where they are now.

**The two are handled differently, on purpose.** A self-contradicting answer is a
model FAILURE: it is counted `malformed` and its deltas stay UNCONSUMED, so a
later run sees the change again. A decline with no reason is a usable judgement —
re-asking would likely produce the same answer while the queue stalled — so the
delta IS consumed and the gap is counted `unreasoned`. Both reach
`store/raw/agent.json` and `tools/build_findings.py`; a console line is not a
fact.

**One suspicion measured and found sound**, recorded so nobody re-checks it: the
confidence cap IS applied. `max(0.01, min(AGENT_MAX_CONFIDENCE, …))` sits on the
append, so a model returning 1.0 cannot make a `proposed` row read as settled.
"""
from __future__ import annotations
import importlib.util, json, os, pathlib, sqlite3, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# ─────────── the ledger refuses an empty record ────────────────────────

def test_an_empty_statement_is_refused_structurally() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-empty-"))
    env = dict(os.environ, OBSERVATORY_DB=str(d / "observatory.db"))
    p = subprocess.run(
        [PY, "-c", "import sys; sys.path.insert(0,'.')\n"
                   "from store import db as sdb\nfrom store import ledger as L\n"
                   "c = sdb.connect()\n"
                   "for s in ('', '   ', '\\n'):\n"
                   "    try:\n"
                   "        L.append(c, owner='agent:t', statement=s)\n"
                   "        print('ACCEPTED', repr(s))\n"
                   "    except L.LedgerError as e:\n"
                   "        print('refused', repr(s), '|', str(e))\n"
                   "c.close()"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    check("an empty statement is refused", "ACCEPTED" not in p.stdout,
          p.stdout[-200:])
    check("and so is whitespace", p.stdout.count("refused") == 3, p.stdout[-200:])
    check("the refusal says why an empty record is useless",
          "cannot be reviewed" in p.stdout, p.stdout[-200:])
    src = (ROOT / "store/ledger.py").read_text(encoding="utf-8")
    check("the guard is in `append`, not at one call site",
          'if not (statement or "").strip():' in src,
          "the wire's min_length protects the wire alone")


# ─────────── the two cross-field rules ─────────────────────────────────

def observe_with(answer: dict, db: pathlib.Path, scratch: pathlib.Path) -> tuple[int, str]:
    """Run the interpretation loop against a stubbed provider.

    The provider is replaced rather than called: the point is the loop's
    handling of an answer, and a real call would spend money to test a branch
    that has nothing to do with the model.
    """
    driver = scratch / "drive.py"
    driver.write_text(
        "import json, sys, pathlib\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"sys.path.insert(0, {str(ROOT / 'agent')!r})\n"
        "import providers\n"
        f"ANSWER = json.loads({json.dumps(json.dumps(answer))})\n"
        # THE WHOLE RETURN SHAPE, read from `providers.complete` rather than
        # guessed: the first stub omitted `stop` and the loop raised KeyError
        # AFTER appending, so the report was never written and the failure
        # looked like a defect in the report.
        "providers.complete = lambda *a, **k: {'parsed': ANSWER, 'model': 'stub/model',\n"
        "                                      'cost': 0.0, 'tokens_in': 0, 'tokens_out': 0,\n"
        "                                      'attempts': 1, 'finish_reason': 'stop',\n"
        "                                      'stop': False, 'selection_level': 'stub'}\n"
        "providers.check_budget = lambda *a, **k: None\n"
        "providers.read_key = lambda *a, **k: ('stub-key', 'stub')\n"
        "import observe\n"
        "raise SystemExit(observe.main())\n", encoding="utf-8")
    p = subprocess.run([PY, str(driver)], cwd=ROOT, capture_output=True, text=True,
                       timeout=600,
                       env=dict(os.environ, OBSERVATORY_DB=str(db),
                                OBSERVATORY_SCRATCH=str(scratch)))
    return p.returncode, p.stdout + p.stderr


def seeded_store() -> tuple[pathlib.Path, pathlib.Path]:
    """A store with one unconsumed delta, so the loop has work."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-interp-"))
    scratch = d / "scratch"
    scratch.mkdir()
    db = d / "observatory.db"
    # WRITTEN FROM THE SCHEMA, not from memory. The first version invented
    # `deltas.project_id` and `detected_at` (the columns are `subject_id`, and
    # there is no detected_at) and omitted `scans.collector_version`, which is
    # NOT NULL — so the seed raised, the loop found no work, and every assertion
    # below failed for a reason that had nothing to do with the code under test.
    # The precondition check in the caller is what localised it.
    seed = subprocess.run(
        [PY, "-c", "import sys; sys.path.insert(0,'.')\n"
                   "from store import db as sdb\n"
                   "c = sdb.connect()\n"
                   "c.execute(\"INSERT INTO scans (id, started_at, finished_at,"
                   " collector_version) VALUES ('s1','2026-09-07T00:00:00Z',"
                   "'2026-09-07T00:00:01Z','test')\")\n"
                   "c.execute(\"INSERT INTO deltas (id, from_scan, to_scan, subject_id,"
                   " kind, before_json, after_json) VALUES"
                   " ('d1','s1','s1','project:alpha-web','commits-changed','1','2')\")\n"
                   "c.commit(); c.close()"],
        cwd=ROOT, env=dict(os.environ, OBSERVATORY_DB=str(db)),
        capture_output=True, text=True, timeout=300)
    if seed.returncode != 0:
        raise AssertionError(f"the fixture could not be seeded: {seed.stderr[-300:]}")
    return db, scratch


def report_of(scratch: pathlib.Path) -> dict:
    f = scratch / "agent.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def unconsumed(db: pathlib.Path) -> int:
    c = sqlite3.connect(str(db))
    try:
        return c.execute("SELECT count(*) FROM deltas WHERE consumed_at IS NULL").fetchone()[0]
    finally:
        c.close()


def test_a_self_contradicting_answer_is_refused_and_the_delta_kept() -> None:
    db, scratch = seeded_store()
    check("the fixture has work to do", unconsumed(db) == 1, str(unconsumed(db)))
    code, out = observe_with({"worth_recording": True, "interpretation": "",
                              "confidence": 0.8, "why_not": ""}, db, scratch)
    check("the run reports it as MALFORMED", "MALFORMED" in out, out[-300:])
    check("nothing was appended",
          sqlite3.connect(str(db)).execute(
              "SELECT count(*) FROM ledger").fetchone()[0] == 0,
          "a blank conclusion would sit in the review queue")
    check("and the delta stays unconsumed for a later run", unconsumed(db) == 1,
          f"{unconsumed(db)} left")
    rep = report_of(scratch)
    check("the report counts it", rep.get("malformed") == 1, str(rep)[:200])


def test_a_decline_with_no_reason_is_counted_but_consumed() -> None:
    db, scratch = seeded_store()
    code, out = observe_with({"worth_recording": False, "interpretation": "",
                              "confidence": 0.2, "why_not": ""}, db, scratch)
    check("the run says no reason was given", "NO REASON GIVEN" in out, out[-200:])
    check("the delta IS consumed, because the judgement is usable",
          unconsumed(db) == 0, f"{unconsumed(db)} left")
    rep = report_of(scratch)
    check("and the gap is a number", rep.get("unreasoned") == 1, str(rep)[:200])
    check("it is not counted as malformed", rep.get("malformed") == 0, str(rep)[:200])


def test_a_decline_WITH_a_reason_counts_nothing() -> None:
    db, scratch = seeded_store()
    code, out = observe_with({"worth_recording": False, "interpretation": "",
                              "confidence": 0.2,
                              "why_not": "a dependency bump, nothing to read"}, db, scratch)
    rep = report_of(scratch)
    check("a reasoned decline is not a gap", rep.get("unreasoned") == 0, str(rep)[:200])
    check("nor malformed", rep.get("malformed") == 0, str(rep)[:200])
    check("and the reason is printed", "dependency bump" in out, out[-200:])


def test_a_good_answer_still_records() -> None:
    db, scratch = seeded_store()
    code, out = observe_with({"worth_recording": True,
                              "interpretation": "the release branch was cut",
                              "confidence": 1.0, "why_not": ""}, db, scratch)
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT statement, confidence, state, owner FROM ledger").fetchone()
    c.close()
    check("the conclusion is recorded", row is not None and
          row["statement"] == "the release branch was cut", str(dict(row) if row else None))
    if row:
        check("in state proposed", row["state"] == "proposed", row["state"])
        # THE SUSPICION THAT WAS SOUND. A model returning 1.0 must not produce a
        # row that reads as settled, and the cap is applied on the append rather
        # than asserted about the source — the earlier test read the code for a
        # `min()` call, which is not the same thing.
        check("and the confidence is capped below 1", row["confidence"] == 0.95,
              str(row["confidence"]))
    check("the report counts it recorded", report_of(scratch).get("recorded") == 1,
          str(report_of(scratch))[:200])


# ─────────── the numbers reach a reader ────────────────────────────────

def findings_for(agent_doc: dict) -> list[dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-interpfind-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "scratch/agent.json").write_text(json.dumps(agent_doc), encoding="utf-8")
    p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT,
                       env=dict(os.environ, OBSERVATORY_REGISTRY=str(d / "registry"),
                                OBSERVATORY_SCRATCH=str(d / "scratch"),
                                OBSERVATORY_DB=str(d / "absent.db")),
                       capture_output=True, text=True, timeout=600)
    try:
        return [f for f in json.loads(p.stdout)["findings"]
                if f["type"].startswith("interpretation.")]
    except (ValueError, KeyError):
        check("findings built", False, (p.stdout + p.stderr)[-300:])
        return []


def test_both_counts_become_findings() -> None:
    got = findings_for({"malformed": 3, "unreasoned": 0})
    check("malformed is a warning",
          [(f["type"], f["severity"]) for f in got] == [("interpretation.malformed",
                                                         "warning")], str(got)[:200])
    if got:
        check("saying the deltas were kept", "unconsumed" in got[0]["detail"],
              got[0]["detail"][-120:])
    got = findings_for({"malformed": 0, "unreasoned": 8})
    check("unreasoned is info, because the judgement is probably right",
          [(f["type"], f["severity"]) for f in got] == [("interpretation.unreasoned",
                                                         "info")], str(got)[:200])
    if got:
        check("saying nothing will revisit it", "revisit" in got[0]["detail"],
              got[0]["detail"][-120:])
    check("a clean run raises neither",
          not findings_for({"malformed": 0, "unreasoned": 0}))


def test_the_schema_comment_no_longer_claims_required_is_enough() -> None:
    src = (ROOT / "agent/observe.py").read_text(encoding="utf-8")
    # The engine's comment was reworded when it was restored; the claim it
    # must carry is unchanged, so the match is on the words, not their case.
    check("the comment says what `required` does and does not do",
          "enforces nothing else" in src.lower(),
          "the previous comment named a defect and a fix that did not fix it")
    check("and names why the rule cannot live in the schema",
          "no `if`/`then`" in src,
          "a reader would otherwise try to move it there")


# ─────────── a record its reader cannot read ───────────────────────────

def test_a_statement_in_another_language_is_recorded_and_counted() -> None:
    """The third shape of a bad answer. Measured 2026-09-07 across
    112 waiting records: one Chinese, two Russian, every other record English —
    and nothing said what language a record should be in, so the estate held
    conclusions its only reader could not read."""
    db, scratch = seeded_store()
    rc, out = observe_with(
        {"worth_recording": True,
         "interpretation": "在一天内提交了245个新提交，表明有大量的代码变更合并或快速迭代。",
         "confidence": 0.8}, db, scratch)
    check("the run succeeds", rc == 0, out[-260:])
    check("the delta is consumed rather than looping on the same model",
          unconsumed(db) == 0, str(unconsumed(db)))
    rep = report_of(scratch)
    check("it was recorded — the content may be right",
          rep.get("recorded") == 1, str(rep)[:200])
    check("and counted as not English", rep.get("not_english") == 1, str(rep)[:200])
    check("it is not counted as malformed or unreasoned",
          (rep.get("malformed"), rep.get("unreasoned")) == (0, 0), str(rep)[:200])
    check("and the run says so where a person would see it",
          "NOT IN ENGLISH" in out, out[-300:])


def test_an_english_sentence_quoting_a_foreign_name_is_not_flagged() -> None:
    """What the threshold is FOR. This estate holds projects with Cyrillic names
    and repositories whose upstream description is Chinese; flagging a sentence
    that quotes one would make the check cry wolf, and a checker that cries wolf
    is the one nobody reads (trap T28)."""
    sys.path.insert(0, str(ROOT / "agent"))
    import observe as O
    importlib.reload(O)
    english = "The repository ссылка-бот gained two commits after a rename."
    check("an English sentence with a Cyrillic name stays under the threshold",
          O.non_latin_share(english) < O.FOREIGN_LETTER_SHARE,
          f"{O.non_latin_share(english):.0%} of letters")
    check("a Chinese sentence is over it",
          O.non_latin_share("在一天内提交了245个新提交") > O.FOREIGN_LETTER_SHARE, "")
    check("a Russian sentence is over it too",
          O.non_latin_share("Получили 11 исправленных изменений и 4 новых коммита") >
          O.FOREIGN_LETTER_SHARE, "")
    check("and pure English is zero", O.non_latin_share("Two commits landed.") == 0.0, "")
    check("an empty statement is zero rather than an error",
          O.non_latin_share("") == 0.0, "")
    # MEASURED, not guessed. The first version of this case was
    # `"在 store/raw/x.json 中 245"` and asserted it was over the threshold — it
    # is 13%, and correctly so: two CJK characters against twelve Latin letters
    # is a mostly-English sentence, which is exactly what the threshold must
    # NOT flag. The property meant here is that digits and a quoted path do not
    # drag a genuinely foreign sentence under the line.
    check("digits and a quoted path do not rescue a foreign sentence",
          O.non_latin_share("在一天内提交了245个新提交，见 store/raw")
          > O.FOREIGN_LETTER_SHARE,
          f"{O.non_latin_share('在一天内提交了245个新提交，见 store/raw'):.0%}")


def test_the_prompt_says_which_language_and_why() -> None:
    sys.path.insert(0, str(ROOT / "agent"))
    import observe as O
    importlib.reload(O)
    # The engine's prompt states the reason without naming one installation's
    # reader: the ledger is shared and reviewed, so it is written in one
    # language — and quoted evidence keeps its own.
    check("the system prompt pins English", "Write in English" in O.SYSTEM,
          "a rule with no reason is a rule a model discounts")
    check("and gives the reason a reader can check",
          "reviewed consistently" in O.SYSTEM, "")
    check("while quoted evidence keeps its original language",
          "original language" in O.SYSTEM, "")


if __name__ == "__main__":
    print("the interpretation contract — the rules the schema could not hold\n")
    for fn in (test_an_empty_statement_is_refused_structurally,
               test_a_self_contradicting_answer_is_refused_and_the_delta_kept,
               test_a_decline_with_no_reason_is_counted_but_consumed,
               test_a_decline_WITH_a_reason_counts_nothing,
               test_a_good_answer_still_records,
               test_both_counts_become_findings,
               test_the_schema_comment_no_longer_claims_required_is_enough,
               test_a_statement_in_another_language_is_recorded_and_counted,
               test_an_english_sentence_quoting_a_foreign_name_is_not_flagged,
               test_the_prompt_says_which_language_and_why):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32man answer that contradicts itself is no longer a blank row in the queue\033[0m")
