#!/usr/bin/env python3
"""Record what a turn changed in a watched repository. Facts only.

Called by the observatory-log plugin's Stop hook. Standard library only — a hook
is a bad place to discover a missing dependency, so nothing here imports outside
the interpreter.

One record per session, corrected as the work grows: the second turn supersedes
the first by compare-and-swap rather than appending a near-duplicate. `why` is
never written here. The hook cannot know it, and inventing it would be the exact
failure the whole design refuses.

Prints a JSON object on stdout for the hook to act on:
    {"recorded": true, "memoryId": …, "revision": …, "hasWhy": false,
     "project": "project:…", "files": 3, "insertions": 40, "deletions": 5}
Any refusal is `{"recorded": false, "reason": …}` — never a traceback, because
the caller is a hook that must degrade silently.
"""
from __future__ import annotations
import argparse, json, re, sqlite3, subprocess, sys, pathlib
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic                                                                    
import paths                                                                     
import safe_git                                                                  

OWNER = "agent:claude-code"
MAX_SUBJECTS = 8


#: Reasons that are ANSWERS about the work rather than failures of the recorder.
#: Everything else in `reason` is a fault, and a fault must not be silent: the
#: same distinction drawn for collectors, applied to the one component that
#: runs in every session of every project.
NOT_A_FAULT = (
    "not a git repository",
    "not in the registry",
    "nothing changed",
    "unchanged since the last turn",
    "no session id",
    # THE RULE WORKING, not the recorder failing. `estate.py` exists to answer
    # "what counts as this estate's OWN work" in one place, and declining a turn
    # inside a third-party clone is that answer: "somebody else's history".
    # Missing from this list, it made every such turn a fault: a session inside
    # a checked-out external project raised `companion.not_recording`, and a
    # durable line was appended for every one of those turns as well.
    "project ownership is",
)


#: Set by `main` before anything is written, so `out` can stamp them on
#: every path including the outer exception handler. `_cwd` is here for the same
#: reason the session id is: a lost turn's first question is WHICH PROJECT, and
#: the outer handler has no arguments left to read it from.
_session_id = ""
_cwd = ""


def out(payload: dict) -> int:
    """Print the result for the hook, and leave a receipt for the findings.

    **The silence this closes.** `ask-why.py` says nothing when `recorded` is
    false, the hook discards stderr, and the hook is *designed* to be quiet
    about work that is not its business — so a genuine failure was
    indistinguishable from a quiet turn. Measured 2026-09-07: roughly seventy-two
    Stop hooks in one session each raised `IllegalTransition` and reported
    nothing, and the only sign was a ledger row that had stopped moving fifteen
    hours earlier.
    """
    print(json.dumps(payload, ensure_ascii=False))
    reason = payload.get("reason") or ""
    # THE SESSION, because this file is SHARED. Every session of every watched
    # project on the machine writes it — 49 Claude processes were running when
    # that was measured — so the last writer wins and a reader must be able to
    # tell whose turn the reason describes. Without it `companion.not_recording`
    # says "the companion could not record the last turn it tried" and cannot
    # say which one.
    payload = {**payload,
               "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "session": _session_id or "",
               "fault": bool(reason) and not any(k in reason for k in NOT_A_FAULT)}
    try:
        atomic.write_json(paths.SCRATCH / "record-turn.json", payload)
    except Exception as exc:                                                      
        # NAMED ON STDERR, not swallowed. A hook must not fail because its own
        # receipt could not be written — the facts are already in the ledger —
        # but a receipt that disappears silently is the very defect this
        # function was added to close, one layer in. The hook discards stderr,
        # so this is for a person running the tool by hand.
        print(f"the record-turn receipt could not be written: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)

    # AND THE DURABLE HALF, because the receipt above is a SLOT. It holds one
    # turn, every session on this machine writes it, and the board is rebuilt
    # every 1800 seconds — so a fault was reported only if it happened to be the
    # newest turn on the whole machine when the tick ran. The incident that
    # produced `companion.not_recording` survived because it was permanent;
    # a transient fault, which is the class this machine actually has, was
    # erased by the next quiet turn of any project.
    if payload["fault"]:
        try:
            import companion_faults                                                 
            kept = companion_faults.append(
                {"at": payload["at"], "session": payload["session"],
                 "cwd": _cwd, "reason": reason})
            where = companion_faults.LOG
        except Exception as exc:                                                   
            kept, where = None, f"the fault log ({type(exc).__name__}: {exc})"
        if kept is None:
            # Named on stderr and NOT swallowed, for the same reason as above:
            # the hook discards stderr, so this is for a person running the tool
            # by hand — and the finding rule reports the orphan separately,
            # because a count with a silently missing line is worse than none.
            print(f"the lost turn could not be added to {where}: the receipt is "
                  f"now its only record", file=sys.stderr)
    return 0


def _fault(exc: BaseException) -> int:
    """The one place an exception becomes a receipt, for both handlers.

    A `sqlite3` error ALSO goes to `store_faults`, which is where free space,
    WAL size and the holder count at the moment of failure are captured. This
    recorder is the machine's most frequent store-toucher — every turn of every
    session opens the database — and it was the one caller that module was never
    wired into. A domain refusal such as `IllegalTransition` is deliberately not
    filed there: every reader of that log reports that the STORE failed, and an
    `lsof` per turn per session would be telemetry about nothing.
    """
    if isinstance(exc, sqlite3.Error):
        try:
            import store_faults                                                     
            store_faults.record("companion.record_turn", exc)                 
        except Exception:                                                          
            pass                                                                   
    return out({"recorded": False, "reason": f"{type(exc).__name__}: {exc}"})


def git(cwd: pathlib.Path, *args: str) -> str:
    """stdout, or "" — through `safe_git`. This runs from the Stop hook on every
    agent turn, and bare git there ran the operator's gpg (signature display),
    fsmonitor, clean filters and index hooks each time."""
    try:
        p = safe_git.run(args, repo=cwd, timeout=20)
    except Exception:
        return ""
    return p.stdout if p.returncode == 0 else ""


def repo_id_for(cwd: pathlib.Path) -> tuple[str | None, str | None]:
    """Map a checkout to a registry repository id, by remote first then by path."""
    try:
        repos = json.loads((paths.REGISTRY / "repositories.json").read_text(encoding="utf-8"))
    except Exception:
        return None, None
    remote = git(cwd, "remote", "get-url", "origin").strip()
    nwo = None
    m = re.search(r"(?:github\.com|bitbucket\.org)[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?$", remote)
    if m:
        nwo = f"{m.group(1)}/{m.group(2)}"
    top = git(cwd, "rev-parse", "--show-toplevel").strip()
    for r in repos["repositories"]:
        if nwo and r["name_with_owner"] == nwo:
            return r["id"], top
        local = r.get("local") or {}
        if top and local.get("path") == top:
            return r["id"], top
    return None, top


#: Only the operator's own work is recorded — the rule itself lives in
#: `estate.py`, which exists BECAUSE this file and `collectors/scan_events.py`
#: once disagreed about what the estate is.
  
#: This module kept a COPY of that set, and the test named
#: `test_one_rule_two_readers` could not detect a divergence: it compared
#: `estate.RECORDED_OWNERSHIP` against a literal spelled out inside the test,
#: then checked only that the STRING "RECORDED_OWNERSHIP" appeared here. Driven
#: 2026-09-07 against a planted copy reading `{"external"}` — this recorder
#: inverted to write about nothing BUT somebody else's history — and both
#: assertions passed. `sys.path` already reaches the root for
#: `atomic` and `paths`, so the copy was never necessary.
import estate                                                                    
records_events = estate.records_events


def _knowledge_age(top: pathlib.Path) -> dict:
    """Days since the code graph was built and the wiki notes were touched.

    The graph's age is measured from the checkout itself; the wiki's from the
    vault scan's receipt, because the recorder must not walk the vault on every
    turn. Both degrade to None — absent is not stale.
    """
    import datetime as _dt
    out: dict = {"graphAgeDays": None, "wikiAgeDays": None}
    gj = top / "graphify-out" / "graph.json"
    if gj.is_file():
        try:
            out["graphAgeDays"] = max(0, int(
                (_dt.datetime.now(_dt.timezone.utc)
                 - _dt.datetime.fromtimestamp(gj.stat().st_mtime, _dt.timezone.utc)
                 ).total_seconds() // 86400))
        except OSError:
            pass
    vf = paths.SCRATCH / "vault.json"
    if vf.is_file():
        try:
            rows = json.loads(vf.read_text(encoding="utf-8"))
            row = next((r for r in rows if r.get("folder") == top.name), None)
            stamp = (row or {}).get("notes_updated_on")
            if stamp:
                out["wikiAgeDays"] = max(0, (
                    _dt.datetime.now(_dt.timezone.utc).date()
                    - _dt.date.fromisoformat(stamp)).days)
        except (OSError, ValueError, TypeError):
            pass
    return out


def project_for(repo_id: str) -> tuple[str | None, str | None]:
    """Return (project_id, ownership) for a repository, or (None, None)."""
    import paths
    try:
        rels = json.loads((paths.REGISTRY / "relations.json").read_text(encoding="utf-8"))
        projects = {p["id"]: p for p in
                    json.loads((paths.REGISTRY / "projects.json").read_text(encoding="utf-8"))["projects"]}
    except Exception:
        return None, None
    for rel in rels["relations"]:
        if rel["type"] == "implemented_by" and rel["to"] == repo_id:
            pid = rel["from"]
            return pid, (projects.get(pid) or {}).get("ownership")
    return None, None


def diff_facts(cwd: pathlib.Path) -> dict:
    """What changed, from git rather than from anything the agent says."""
    porcelain = [l for l in git(cwd, "status", "--porcelain").splitlines() if l.strip()]
    numstat = [l for l in git(cwd, "diff", "HEAD", "--numstat").splitlines() if l.strip()]
    ins = dele = 0
    for line in numstat:
        parts = line.split("\t")
        if len(parts) >= 2:
            ins += int(parts[0]) if parts[0].isdigit() else 0
            dele += int(parts[1]) if parts[1].isdigit() else 0
    files = sorted({l[3:].split(" -> ")[-1].strip() for l in porcelain})
    head = git(cwd, "rev-parse", "--short", "HEAD").strip()
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD").strip()
    unpushed = [l for l in git(cwd, "log", "--oneline", "@{u}..HEAD").splitlines() if l.strip()] \
        if git(cwd, "rev-parse", "--abbrev-ref", "@{u}").strip() else []
    return {"files": files, "insertions": ins, "deletions": dele, "head": head,
            "branch": branch, "unpushed": [u[:120] for u in unpushed[:MAX_SUBJECTS]]}


def statement_of(facts: dict, repo_id: str) -> str:
    n = len(facts["files"])
    bits = [f"{repo_id.split(':', 1)[1]} on {facts['branch'] or '?'}: "
            f"{n} file{'s' if n != 1 else ''} changed, "
            f"+{facts['insertions']}/-{facts['deletions']}"]
    if facts["unpushed"]:
        bits.append(f"{len(facts['unpushed'])} unpushed commit(s)")
    shown = facts["files"][:MAX_SUBJECTS]
    bits.append("touched: " + ", ".join(shown) + ("…" if n > len(shown) else ""))
    return " | ".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--session-id", default="")
    args = ap.parse_args()
    # BEFORE THE FIRST GUARD, not beside the one that checks it. The assignment
    # sat after the git-repository check, so the earliest returns — the common
    # case on an unwatched directory — wrote a receipt with an empty session.
    global _session_id, _cwd
    _session_id = args.session_id or ""
    _cwd = args.cwd or ""

    cwd = pathlib.Path(args.cwd)
    if not cwd.is_dir():
        return out({"recorded": False, "reason": "cwd does not exist"})
    if not git(cwd, "rev-parse", "--git-dir").strip():
        return out({"recorded": False, "reason": "not a git repository"})

    repo_id, top = repo_id_for(cwd)
    if repo_id is None:
        return out({"recorded": False,
                    "reason": "this repository is not in the registry; run "
                              "project-observatory full scan merge emit to add it"})
    facts = diff_facts(pathlib.Path(top or cwd))
    if not facts["files"] and not facts["unpushed"]:
        return out({"recorded": False, "reason": "nothing changed"})

    from store import db as store_db
    from store import ledger as L
    project_id, ownership = project_for(repo_id)
    if not estate.records_events(ownership):
        return out({"recorded": False,
                    "reason": f"project ownership is {ownership!r}; only "
                              f"{sorted(estate.RECORDED_OWNERSHIP)} are recorded"})
    project_id = project_id or repo_id
    statement = statement_of(facts, repo_id)
    evidence = [{"uri": f"repo:{repo_id}", "head": facts["head"], "branch": facts["branch"]},
                {"uri": "git:status --porcelain", "files": facts["files"][:40]}]

    # A row with no session id can never be REVISED: the prior-row lookup below
    # is keyed on the session, so every turn would mint a fresh `proposed`
    # memory and the "unchanged since the last turn" guard could never fire.
    # One long session already holds twenty revisions of one memory; without an
    # id that would have been twenty memories in the review queue, each needing
    # a decision. Refusing is the honest answer, and the hook now resolves its
    # interpreter before reading the payload so an empty id means the CALLER
    # omitted it rather than the parse having failed silently.
    if not args.session_id:
        return out({"recorded": False,
                    "reason": "no session id: a row keyed to no session cannot be "
                              "revised, so every turn would add one to the review "
                              "queue instead of correcting the last",
                    "project": project_id})

    conn = store_db.connect()
    try:
        prior = None
        if args.session_id:
            # `NOT IN tombstones`, because a record an erasure has closed takes
            # no further revisions and this hook runs on every turn
            # of every session. Without it the append would raise and the hook
            # would report `recorded: false` for the rest of the session; with
            # it the turn starts a fresh record, which is what an erasure means.
            row = conn.execute(
                "SELECT l.memory_id, l.revision, l.why, l.statement, l.state"
                " FROM ledger l JOIN (SELECT memory_id, MAX(revision) r FROM ledger"
                "   WHERE session_id = ? AND kind = 'session' GROUP BY memory_id) m"
                "  ON m.memory_id = l.memory_id AND m.r = l.revision"
                " WHERE l.memory_id NOT IN (SELECT memory_id FROM tombstones)"
                " ORDER BY l.revision DESC LIMIT 1", (args.session_id,)).fetchone()
            prior = row if row and row["memory_id"] else None
        # A PROMOTED RECORD IS FINISHED, as far as this writer is concerned.
        #
        # **The defect this closes was silent and total.** `tools/corroborate.py`
        # promotes a `proposed` session row to `observed` once it can re-check
        # the commit sha — which is exactly its job — and the next turn's append
        # then asked for `state="proposed"` on an `observed` record. The
        # lifecycle refuses that edge, correctly, so `L.append` raised
        # `IllegalTransition: observed -> proposed`, the handler below turned it
        # into `recorded: false`, `ask-why.py` says nothing on a false, and the
        # hook's stderr is discarded. Measured 2026-09-07: **once a session was
        # corroborated the companion stopped recording it for ever** — this
        # session's own record sat at revision 2 from 20:12 the previous evening
        # while roughly seventy-two Stop hooks ran and wrote nothing.
        #
        # Revising it in place at its CURRENT state would be worse than the bug:
        # the new statement describes work the corroborator never checked, so
        # keeping `observed` would claim a corroboration the text never got. A
        # fresh `proposed` record is what an automated writer is allowed to make,
        # and the link to what came before travels in `provenance` — not in
        # `supersedes`, which would say the earlier record is no longer true when
        # it is still true about earlier work.
        continues = None
        if prior is not None and (prior["state"] or "") != "proposed":
            continues = prior["memory_id"]
            prior = None
        if prior is not None and prior["statement"] == statement:
            # Nothing moved since the last turn. Appending here would add a
            # revision that says exactly what the previous one said, and
            # append-only is a reason to be careful about what gets appended.
            return out({"recorded": False, "reason": "unchanged since the last turn",
                        "memoryId": prior["memory_id"], "revision": prior["revision"],
                        "hasWhy": bool(prior["why"]), "project": project_id})
        result = L.append(
            conn, owner=OWNER, kind="session", statement=statement,
            why=prior["why"] if prior else None,
            memory_id=prior["memory_id"] if prior else None,
            expected_revision=prior["revision"] if prior else None,
            project_id=project_id, session_id=args.session_id or None,
            function="episodic", scope="project", state="proposed",
            # NO CONFIDENCE, deliberately. This record restates a `git status`
            # — "166 files changed, +14051/-809" — and a fact anyone can re-run
            # in a second is not fifty per cent likely. `confidence=0.5` was
            # hardcoded on 77 rows and `./observatory.py digest` prints that
            # number beside real judgements from 0.01 to 0.95, so a reviewer
            # read a fabricated measurement as a writer's own doubt. NULL is the
            # honest third answer: THIS IS NOT A JUDGEMENT.
            confidence=None, evidence=evidence,
            provenance=[{"source": "observatory-log/Stop", "measured": True,
                         **({"continues": continues} if continues else {})}])
        has_why = bool(prior and prior["why"])
        return out({"recorded": True, "memoryId": result["memoryId"],
                    "revision": result["revision"], "hasWhy": has_why,
                    "continues": continues,
                    "project": project_id, "files": len(facts["files"]),
                    "insertions": facts["insertions"], "deletions": facts["deletions"],
                    "unpushed": len(facts["unpushed"]),
                    # Knowledge freshness AT THE MOMENT OF WORK — the one moment
                    # an agent can act on it for free, being already inside the
                    # project. The board's aggregated rows (`project.graph_stale`,
                    # `project.wiki_stale`) tell the operator later; this tells
                    # the agent NOW, and the ask-why hook turns it into words.
                    # None means "does not apply": a project with no graph has
                    # not adopted graphify, and that is a choice, not staleness
                    #.
                    **_knowledge_age(pathlib.Path(top or cwd))})
    except Exception as exc:
        return _fault(exc)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:                                                                
        # THROUGH `out`, so the receipt exists for the one path that most needs
        # it. A bare print here left the outer failure — the class that swallowed
        # `IllegalTransition` for fifteen hours — as the only outcome with no
        # record at all.
        _fault(exc)
        raise SystemExit(0)
