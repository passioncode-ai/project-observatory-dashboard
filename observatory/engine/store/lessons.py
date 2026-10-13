#!/usr/bin/env python3
"""A lesson as its own record kind: `kind = "lesson"`, one per (classKey, project).

A lesson is a retro entry's CLASS, stated so an agent in another project can use it
before it repeats the mistake. Its writer is task-pipeline's `scripts/lessons.py`, which
has two routes: `emit-observatory --tool observatory_lesson` where the engine serves this
tool, and the record bridge (`observatory_record`, a note whose statement starts
`lesson:`) otherwise; `tools/migrate_lessons.py` turns bridge notes into lesson records.
This module accepts the first route's arguments field for field; `tests/test_lesson.py`
holds that emitter output's shape and checks the published schema against it, so the
two cannot drift apart silently.

What a lesson may carry, and why each rule exists:

* **The project is a pseudonymous key, `p-` and 12 hex digits** of the repository's root
  commit, never a `project:<slug>` id. A lesson is read by every other project; a
  repository's name can be a customer's or a product's, and it would travel with it.
  The key is stable across clones and names nothing.
* **The evidence commit is kept as `commit:<12 hex>`**, the form the memory redactor
  keeps whole (`memory_redact.KEEP`); a full 40-character id is a long hex run and the
  shape filter would replace it. Twelve digits still resolve with `git cat-file`.
* **No path, no URL, no e-mail address, one line per field.** These are refused, not
  rewritten: a path names a machine and a person, and a lesson that needs one is an
  incident, not a class. Credential shapes and the workspace's known values are
  REDACTED by `memory_redact` like every agent write, and the answer says where.
* **One record per (classKey, project).** A repeat from the same project revises its
  record; it never mints a second one, because two records of one project would read
  as two witnesses to the corroborator (`tools/corroborate.py`).

Lessons are agents' work memory and stay in this store (the operator's memory
boundary). A lesson that turns out to be project knowledge has a future export path to
Fabric project memory; nothing here moves one (docs/design/AGENT-MEMORY.md, "Lessons").
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

KIND = "lesson"
FUNCTION = "experiential"
#: Not a project's: a lesson is for every project of the estate, so its `project_id`
#: column is empty and its origin lives in the body.
SCOPE = "global"
CONFIDENCE = 0.5

CLASS_KEY = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
PROJECT_KEY = re.compile(r"^p-[0-9a-f]{12}$")
#: What the emitter sends (7-40 hex) and the stored form, both accepted.
COMMIT = re.compile(r"^(?:commit:)?([0-9a-f]{7,40})$")
#: lesson/1's bounds (task-pipeline templates/lesson.schema.json).
TEXT = {"title": (8, 160), "trigger": (3, 200), "check": (3, 200)}
NOUNS = (3, 8)
NOUN = (2, 32)
CLASS_KEY_MAX = 64
#: What a refusal tells the writer to do, naming both routes of the emitter.
HINT = ("a lesson is a class, not an incident: one line per field, no path, URL or e-mail "
        "address, the project as its pseudonymous key. task-pipeline's `lessons.py "
        "emit-observatory` builds the arguments: `--tool observatory_lesson` where the engine "
        "has this tool, `observatory_record` (a `lesson:` note) otherwise")
#: The arguments `observatory_lesson` takes, and nothing else.
FIELDS = ("classKey", "title", "trigger", "check", "nouns", "origin")
ORIGIN_FIELDS = ("projectId", "commit")

#: What a lesson may not carry, each with the words its refusal uses. The same list the
#: emitter applies before it sends (lessons.py FORBIDDEN, path and contact rows), so an
#: agent that skips the emitter meets the same rule here.
FORBIDDEN = (
    (re.compile(r"(?:^|[\s\"'(`=:])(?:/[A-Za-z0-9._-]+){2,}"), "an absolute path"),
    (re.compile(r"(?:^|[\s\"'(`=:])~[A-Za-z0-9._-]*/"), "a home path"),
    (re.compile(r"(?:^|[\s\"'(`])[A-Za-z]:\\"), "a drive path"),
    (re.compile(r"[a-z][a-z0-9+.-]*://", re.I), "a URL"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "an e-mail address"),
)


def commit_form(value: str) -> str:
    """`commit:<at most 12 hex>`: the stored, redaction-safe, still-resolvable form."""
    m = COMMIT.match(value)
    if not m:
        raise ValueError(value)
    return "commit:" + m.group(1)[:12]


def _text(name: str, value: Any, lo: int, hi: int) -> list[str]:
    if not isinstance(value, str):
        return [f"{name} is not a string"]
    errs = []
    if not lo <= len(value) <= hi:
        errs.append(f"{name} is {len(value)} characters; a lesson allows {lo}-{hi}")
    if "\n" in value or "\r" in value:
        errs.append(f"{name} carries a line break; one line each")
    errs += [f"{name} carries {what}" for rx, what in FORBIDDEN if rx.search(value)]
    return errs


def validate(lesson: Any) -> list[str]:
    """Every reason this lesson is refused, in one list (empty: accepted)."""
    if not isinstance(lesson, dict):
        return ["a lesson is an object"]
    errs = [f"unknown field {k!r}; a lesson carries {', '.join(FIELDS)}"
            for k in lesson if k not in FIELDS]
    errs += [f"missing field {k!r}" for k in FIELDS if lesson.get(k) is None]
    if errs:
        return errs
    ck = lesson["classKey"]
    if not isinstance(ck, str) or len(ck) > CLASS_KEY_MAX or not CLASS_KEY.match(ck):
        errs.append("classKey is not a slug (a-z, 0-9, single hyphens, at most 64)")
    for name, (lo, hi) in TEXT.items():
        errs += _text(name, lesson[name], lo, hi)
    nouns = lesson["nouns"]
    if (not isinstance(nouns, list) or not NOUNS[0] <= len(nouns) <= NOUNS[1]
            or len(set(map(str, nouns))) != len(nouns)):
        errs.append("nouns is not 3-8 distinct strings")
    else:
        for n in nouns:
            errs += _text("a noun", n, *NOUN)
    o = lesson["origin"]
    if not isinstance(o, dict) or set(o) != set(ORIGIN_FIELDS):
        errs.append("origin is not {projectId, commit}")
    else:
        if not isinstance(o["projectId"], str) or not PROJECT_KEY.match(o["projectId"]):
            errs.append("origin.projectId is not a pseudonymous project key (p- and 12 hex "
                        "of the repository's root commit); a project:<slug> id is refused")
        if not isinstance(o["commit"], str) or not COMMIT.match(o["commit"]):
            errs.append("origin.commit is not a commit id (7-40 hex, or commit:<hex>)")
    return errs


def body_of(lesson: dict) -> dict:
    """The record's body: lesson/1's fields, the commit in its stored form."""
    o = lesson["origin"]
    return {"schema": "lesson/1", "classKey": lesson["classKey"], "title": lesson["title"],
            "trigger": lesson["trigger"], "check": lesson["check"],
            "nouns": list(lesson["nouns"]),
            "origin": {"projectId": o["projectId"], "commit": commit_form(o["commit"])}}


def statement_of(body: dict) -> str:
    return f"lesson: {body['classKey']} — {body['title']}"


def why_of(body: dict) -> str:
    return f"{body['trigger']}; check: {body['check']}"


def evidence_of(body: dict) -> list[dict]:
    return [{"kind": "commit", "ref": body["origin"]["commit"],
             "project": body["origin"]["projectId"]}]


def existing(conn: sqlite3.Connection, class_key: str, project_key: str) -> sqlite3.Row | None:
    """The current, unerased lesson of this class from this project, if one exists.

    An erased one does not count: a tombstoned record takes no further revisions, and
    a new lesson after an erasure is a new record (`store/ledger.py`)."""
    return conn.execute(
        "SELECT l.* FROM ledger l JOIN (SELECT memory_id, MAX(revision) r FROM ledger"
        " GROUP BY memory_id) m ON m.memory_id = l.memory_id AND m.r = l.revision"
        " LEFT JOIN tombstones t ON t.memory_id = l.memory_id"
        " WHERE t.memory_id IS NULL AND l.kind = ?"
        # A closed record is history, not this project's record of the class: a lesson
        # archived because its commit changed has a successor (see `write`).
        " AND l.state NOT IN ('rejected', 'archived', 'superseded')"
        " AND json_extract(l.body_json, '$.classKey') = ?"
        " AND json_extract(l.body_json, '$.origin.projectId') = ?"
        # THE OLDEST RECORD FIRST, decided by when each record was MINTED and then by its
        # first row's insertion order — never by the random id. Two records of one class and
        # project can be minted in one second (an archive and its replacement), and a tie
        # broken by `memory_id` picked either. One open record per (classKey, project) is
        # the invariant; this order only makes a breach of it answer the same way each time.
        " ORDER BY (SELECT MIN(x.created_at) FROM ledger x WHERE x.memory_id = l.memory_id),"
        " (SELECT MIN(x.rowid) FROM ledger x WHERE x.memory_id = l.memory_id), l.memory_id"
        " LIMIT 1",
        (KIND, class_key, project_key)).fetchone()


def current_lessons(conn: sqlite3.Connection,
                    states: tuple[str, ...] = ("proposed", "observed", "supported")) -> list[dict]:
    """Every current, unerased lesson in these states, with its body parsed."""
    rows = conn.execute(
        "SELECT l.* FROM ledger l JOIN (SELECT memory_id, MAX(revision) r FROM ledger"
        " GROUP BY memory_id) m ON m.memory_id = l.memory_id AND m.r = l.revision"
        " LEFT JOIN tombstones t ON t.memory_id = l.memory_id"
        f" WHERE t.memory_id IS NULL AND l.kind = ? AND l.state IN ({','.join('?' * len(states))})"
        " ORDER BY l.created_at, l.memory_id", (KIND, *states)).fetchall()
    out = []
    for r in rows:
        try:
            body = json.loads(r["body_json"] or "{}")
        except ValueError:
            body = {}
        out.append({**dict(r), "body": body if isinstance(body, dict) else {}})
    return out


def has_support(conn: sqlite3.Connection, body: dict) -> bool:
    """Two known, distinct roots with resolving commits; a shared five-second Git budget.

    This is current evidence, not authentication of a local caller's declared identity.
    """
    from tools import corroborate as C
    deadline = time.monotonic() + 5.0
    keys = C.project_keys(deadline=deadline)
    if C.resolve_origin(body.get("origin") or {}, keys, deadline=deadline)[0] is not True:
        return False
    mine = body["origin"]["projectId"]
    projects = {mine}
    for row in current_lessons(conn):
        other = row["body"]
        if other.get("classKey") != body.get("classKey"):
            continue
        origin = other.get("origin") or {}
        if origin.get("projectId") in projects:
            continue
        if C.resolve_origin(origin, keys, deadline=deadline)[0] is True:
            projects.add(origin["projectId"])
            if len(projects) >= C.LESSON_PROJECTS:
                return True
    return False


def revalidate(conn: sqlite3.Connection, ledger: Any, *, memory_id: str,
               expected_revision: int) -> dict:
    """Recheck the current observation without replaying a stale scan's body."""
    with ledger.atomic_revisions(conn):
        prior = ledger.current(conn, memory_id)
        if prior is None or prior["revision"] != expected_revision:
            raise ledger.RevisionConflict(memory_id, expected_revision,
                                          prior["revision"] if prior else -1)
        if prior["state"] != "observed" or prior["owner"] == ledger.OPERATOR:
            raise ledger.LedgerError("only an agent-owned observation is mechanically revalidated")
        return _write_locked(conn, ledger, owner=prior["owner"],
                             body=json.loads(prior["body_json"]),
                             provenance=[{"source": "lesson-evidence-revalidation"}])


def write(conn: sqlite3.Connection, ledger: Any, *, owner: str, body: dict,
          provenance: list[dict]) -> dict:
    with ledger.atomic_revisions(conn):
        return _write_locked(conn, ledger, owner=owner, body=body, provenance=provenance)


def _write_locked(conn: sqlite3.Connection, ledger: Any, *, owner: str, body: dict,
          provenance: list[dict]) -> dict:
    """Mint the lesson, or revise this project's record of the class.

    An unchanged repeat writes nothing and answers with the current revision. A change of
    wording keeps an observation only while two distinct known projects still have
    resolving evidence. Missing evidence also invalidates an unchanged repeat.

    **A NEW COMMIT IS A NEW CLAIM.** A corroboration vouched for the old commit, so a
    promoted lesson that now cites another one cannot stay `observed`. The lifecycle has
    no edge back to `proposed`, and none is added: the old record is ARCHIVED by its
    author (`observed -> archived`, its history kept) and a new `proposed` record of the
    class replaces it (`outcome: replaced`, `replaces: <id>@<rev>`). The corroborator
    checks the new commit on its next pass and promotes it only once it resolves. A
    proposed lesson whose commit changes is simply revised: nothing vouched for it yet.

    `ledger` is `store.ledger`, passed in so a caller that reloaded it in a test uses one
    module."""
    prior = existing(conn, body["classKey"], body["origin"]["projectId"])
    if prior is not None:
        same = (json.loads(prior["body_json"] or "{}") == body
                and prior["statement"] == statement_of(body) and prior["why"] == why_of(body))
        unsupported = prior["state"] == "observed" and not has_support(conn, body)
        if same and not unsupported:
            return {"memoryId": prior["memory_id"], "revision": prior["revision"],
                    "state": prior["state"], "owner": prior["owner"], "supersedes": [],
                    "createdAt": prior["created_at"], "outcome": "unchanged"}
        prior_body = json.loads(prior["body_json"] or "{}")
        new_commit = (prior_body.get("origin") or {}).get("commit") != body["origin"]["commit"]
        if (new_commit and prior["state"] != "proposed") or unsupported:
            closed = ledger.append(
                conn, owner=owner, kind=KIND, function=FUNCTION, scope=SCOPE,
                memory_id=prior["memory_id"], expected_revision=prior["revision"],
                statement=prior["statement"], why=prior["why"], state="archived",
                confidence=prior["confidence"], evidence=json.loads(prior["evidence_json"] or "[]"),
                body=prior_body,
                provenance=json.loads(prior["provenance_json"] or "[]") + [
                    {"source": "observatory_lesson",
                     "archived": "current evidence no longer supports the observed claim"}])
            replaces = f"{prior['memory_id']}@{closed['revision']}"
            out = ledger.append(conn, owner=owner, kind=KIND, function=FUNCTION, scope=SCOPE,
                                statement=statement_of(body), why=why_of(body),
                                state="proposed", confidence=CONFIDENCE,
                                evidence=evidence_of(body), body=body,
                                provenance=provenance + [{"source": "observatory_lesson",
                                                          "replaces": replaces}])
            return {**out, "outcome": "replaced", "replaces": replaces}
        out = ledger.append(conn, owner=owner, kind=KIND, function=FUNCTION, scope=SCOPE,
                            memory_id=prior["memory_id"], expected_revision=prior["revision"],
                            statement=statement_of(body), why=why_of(body),
                            state=prior["state"], confidence=prior["confidence"],
                            evidence=evidence_of(body), body=body,
                            provenance=json.loads(prior["provenance_json"] or "[]"))
        return {**out, "outcome": "revised"}
    out = ledger.append(conn, owner=owner, kind=KIND, function=FUNCTION, scope=SCOPE,
                        statement=statement_of(body), why=why_of(body), state="proposed",
                        confidence=CONFIDENCE, evidence=evidence_of(body), body=body,
                        provenance=provenance)
    return {**out, "outcome": "created"}
