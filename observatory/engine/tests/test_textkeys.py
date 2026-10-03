#!/usr/bin/env python3
"""Search keys, the coverage floor and the checkpoint body in the lexical index.

`textkeys` turns text into the keys the lexical index stores and a question asks
for: a Snowball stem, cut to six characters for Russian, so a question in one word
form finds a record in another. A checkpoint's statement carries only its goal and
next actions, so its body's prose — decisions, constraints, results, questions,
notes — is keyed too; its identifiers (paths, commits, credential names) are not.
The search then needs a share of a question's keys in a hit, and when nothing
clears that floor it says `abstain` instead of answering from the best of the bad.
"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402
import textkeys                                                     # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def test_word_forms_share_a_key() -> None:
    check("the stemmer is installed with the full extras", textkeys.STEMMER)
    k = textkeys.keys
    check("Russian: a noun and its verb share a key",
          k("переключение")[0] == k("переключает")[0] == k("переключить")[0],
          str([k("переключение"), k("переключает"), k("переключить")]))
    check("Russian: ё reads as е", k("отстаёт") == k("отстает"), str(k("отстаёт")))
    check("Russian keys are at most six characters",
          all(len(x) <= textkeys.RU_KEY for x in k("контрольных суммах регистрацию")))
    check("English: inflections share a key", k("exports")[0] == k("exported")[0],
          str([k("exports"), k("exported")]))
    check("case does not matter", k("Ledger") == k("ledger"))


def test_a_question_asks_for_its_subject_words_only() -> None:
    q = textkeys.query_keys("Why are the cents stored as integers?")
    check("stopwords leave a question", not ({"why", "are", "the", "as"} & set(q)), str(q))
    check("each key once, in order",
          q == list(dict.fromkeys(q)) and q[0] == textkeys.keys("cents")[0], str(q))
    check("a question of stopwords asks for nothing", textkeys.query_keys("что это и как") == [])
    check("one-letter words are dropped", textkeys.query_keys("a b c x") == [])


def test_the_body_prose_is_keyed_and_identifiers_are_not() -> None:
    body = {
        "goal": "Move invoices",
        "plan": [{"step_id": "S1", "title": "backfill the archive"}],
        "done": [{"step_id": "S1", "result": "batches imported", "evidence": ["abc1234"]}],
        "open": [{"step_id": "S2", "next_action": "switch readers"}],
        "decisions": [{"id": "D1", "choice": "integers for cents", "why": "rounding drift"}],
        "constraints": ["finance signs off first"],
        "questions": ["who owns refunds"],
        "notes": "replica lags under load",
        "artifacts": [{"kind": "git", "path": "/srv/zebrapath", "head": "deadbee",
                       "note": "migration branch"}],
        "credentials": [{"project": "alpha-web", "env": "prod", "name": "QUOKKA_TOKEN"}],
    }
    prose = textkeys.body_prose(body)
    for phrase in ("backfill the archive", "batches imported", "switch readers",
                   "integers for cents", "rounding drift", "finance signs off first",
                   "who owns refunds", "replica lags under load", "migration branch"):
        check(f"prose is keyed: {phrase!r}", phrase in prose)
    for ident in ("zebrapath", "deadbee", "abc1234", "QUOKKA_TOKEN", "alpha-web", "D1"):
        check(f"an identifier is not: {ident!r}", ident not in prose)
    check("the stored JSON reads the same as the dict",
          textkeys.body_prose(json.dumps(body)) == prose)
    check("a body that is not JSON keys nothing", textkeys.body_prose("{not json") == "")
    check("no body keys nothing", textkeys.body_prose(None) == "" and textkeys.body_prose([]) == "")
    check("malformed entries are skipped, not raised",
          textkeys.body_prose({"decisions": ["text", {"choice": 3}], "notes": 5,
                               "constraints": [None, "kept"]}) == "kept")
    stems = textkeys.stems_of("checkpoint wf S1", None, body).split()
    check("the stems column carries the body", textkeys.keys("rounding")[0] in stems
          and textkeys.keys("checkpoint")[0] in stems, str(stems))


def test_the_floor_needs_a_share_of_the_question() -> None:
    import survey
    check("one subject word needs itself", survey.coverage_floor(1) == 1.0)
    check("two need one of them", survey.coverage_floor(2) == 0.5)
    check("none cannot divide by zero", survey.coverage_floor(0) == 1.0)
    check("three or more need the measured share",
          survey.coverage_floor(3) == survey.coverage_floor(9) == survey.COVERAGE)


def test_a_checkpoint_is_found_by_its_body_and_nothing_else_abstains() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-textkeys-"))
    before = os.environ.get("OBSERVATORY_DB")
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    try:
        import paths
        importlib.reload(paths)
        from store import db as sdb
        importlib.reload(sdb)
        from store import workflow as W
        import memory_redact
        import survey
        importlib.reload(survey)
        conn = sdb.connect()
        quiet = memory_redact.Redactor(known_loader=lambda: {})
        wf = W.checkpoint_write(
            conn, owner="agent:fixture", idempotency_key="textkeys-0001", step_id="S1",
            status="in_progress", project_id="project:alpha-web", redactor=quiet,
            body={"goal": "Move invoices to the new ledger",
                  "decisions": [{"choice": "Keep cents as integers", "why": "rounding drift"}],
                  "notes": "Реплика отстаёт на минуту под нагрузкой"})
        conn.close()
        mid = f"ckpt:{wf['workflowId']}"
        # Written a moment ago and not indexed by a pass: found in the same transaction.
        for q in ("Why are cents kept as integers?", "Насколько отстают реплики под нагрузкой?"):
            r = survey.search(q, limit=5)
            check(f"found by its body: {q!r}", mid in [h["memoryId"] for h in r["results"]]
                  and not r["abstain"], json.dumps(r, ensure_ascii=False)[:400])
        r = survey.search("Which payment provider charges the lowest fee?", limit=5)
        check("a question nothing answers abstains, with its reason and floor",
              r["abstain"] and r["results"] == [] and r.get("abstainReason")
              and r["floor"]["coverage"] == survey.COVERAGE, json.dumps(r)[:400])
    finally:
        if before is None:
            os.environ.pop("OBSERVATORY_DB", None)
        else:
            os.environ["OBSERVATORY_DB"] = before
        import paths
        importlib.reload(paths)


if __name__ == "__main__":
    print("search keys, the floor and the checkpoint body\n")
    for fn in (test_word_forms_share_a_key,
               test_a_question_asks_for_its_subject_words_only,
               test_the_body_prose_is_keyed_and_identifiers_are_not,
               test_the_floor_needs_a_share_of_the_question,
               test_a_checkpoint_is_found_by_its_body_and_nothing_else_abstains):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mevery check passed\033[0m")
