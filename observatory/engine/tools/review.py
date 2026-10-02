#!/usr/bin/env python3
"""The operator's queue: read what is proposed, and decide it.

WRITES REQUIRE A TERMINAL, AND THERE IS NO `--yes`. Everything this tool writes
carries `owner="operator"`, which is the highest authority in the ledger:
operator rows are exempt from retention and no automated writer may supersede
them. A tool that mints that authority without a person present lets any agent
with shell access forge it, and every structural guard in `ledger.py` is
downstream of who the owner is.

Requiring a TTY does not stop a determined process — it can allocate one — and
that is not the claim. The claim is that approval is an ACT, and an act that can
happen as a side effect of a script is not one. An escape hatch flag would undo
exactly this, so none exists.

Reading is unrestricted: `list` and `show` change nothing.

    review.py list [--kind session|observation] [--state proposed]
    review.py show <memory-id>
    review.py promote <memory-id> [--why …]      proposed -> observed -> supported
    review.py reject <memory-id> --why …         proposed -> rejected
    review.py tombstone <memory-id> --why …      erasure, and it leaves a trace

    review.py accept-proposal <id> --why …       a REGISTRY proposal: the patch
                                                 lands in the curation file the
                                                 emitter reads, never in
                                                 registry/*.json, which is
                                                 rewritten whole every tick
    review.py reject-proposal <id> --why …       the row stays with the reason
"""
from __future__ import annotations
import argparse, json, pathlib, sqlite3, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths                                                                      
import atomic                                                                     
import proposals                                                                  
from store import db as store_db                                                  
from store import ledger as L                                                     
from store import retention as _retention                              

NEXT = {"proposed": "observed", "observed": "supported"}


def latest(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT l.* FROM ledger l JOIN (SELECT memory_id, MAX(revision) rev FROM ledger "
        "GROUP BY memory_id) m ON l.memory_id = m.memory_id AND l.revision = m.rev "
        "ORDER BY l.created_at DESC").fetchall()


def require_terminal(action: str) -> None:
    if not sys.stdin.isatty():
        sys.exit(
            f"review: refusing to {action} without a terminal.\n"
            f"  Everything written here is owned by `operator` — exempt from retention, "
            f"and unsupersedable by any agent.\n"
            f"  Minting that authority from a script would let anything with shell "
            f"access forge it. Run this yourself; there is no --yes.")


def shown(value, width: int = 0) -> str:
    """A value, or a legible mark for its ABSENCE — never the word `None`.

    `confidence` is nullable and NULL means "this is not a judgement": a session
    record restates a `git status`, and `tools/record_turn.py` hardcoded 0.5 on
    77 of them, in the one column a reviewer reads as a measurement. `why` is
    nullable for the same kind of reason — most records have never needed a
    revision note.

    Two of the three consumers already did this, which is what says the design
    intended NULL: the list view spelled it inline and the dashboard writes
    `n.conf != null ? … : ""`. The digest and the single-record view printed the
    raw value and would say `None`.
    """
    if value is None:
        mark = "—"
    elif isinstance(value, float):
        mark = f"{value:.2f}"
    else:
        mark = str(value)
    return mark.rjust(width) if width else mark


def cmd_list(conn, args) -> int:
    rows = [r for r in latest(conn)
            if (not args.state or r["state"] == args.state)
            and (not args.kind or r["kind"] == args.kind)]
    if not rows:
        print("nothing matches")
        return 0
    by_state: dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    print("  ".join(f"{k}={v}" for k, v in sorted(by_state.items())), "\n")
    for r in rows:
        conf = f"{shown(r['confidence'], width=4)}"
        print(f"{r['memory_id']}  r{r['revision']:<3} {r['state']:<9} {conf}  "
              f"{r['owner']:<20} {r['kind']:<11} {(r['project_id'] or '')[:28]:<28} "
              f"{(r['statement'] or '')[:60]}")
    return 0


def report_proposals(conn) -> int:
    """REGISTRY proposals, which had no reader anywhere.

    `observatory_propose` is declared surface — it is in the manifest and a probe
    exercises it — and it writes into `proposals`, where nothing in the tick, the
    dashboard or any CLI ever looked. A proposal nobody can see is worse than no
    proposal: the caller was told it landed.

    A function rather than a block at the end of the digest, because the digest
    returns EARLY when no ledger conclusion is waiting — and a reader after an
    early return is invisible again for exactly the case that matters most: a
    proposal arriving on a quiet queue. Caught by its own test on 2026-09-07,
    which planted one proposal and no memories.
    """
    try:
        pending = conn.execute(
            "SELECT id, target_id, status, created_at, substr(patch_json, 1, 90) AS patch"
            " FROM proposals WHERE status = 'proposed' ORDER BY created_at").fetchall()
    except sqlite3.Error as exc:
        print(f"\nproposals unreadable: {exc}")
        return 0
    if not pending:
        print("\nno registry proposals are pending")
        return 0
    plural = "" if len(pending) == 1 else "s"
    print(f"\n{len(pending)} registry proposal{plural} awaiting a decision:")
    for r in pending:
        print(f"  {r['created_at'][:10]}  {r['target_id']}  {r['patch']}")
        print(f"       {r['id']}")
    return len(pending)


def cmd_digest(conn, args) -> int:
    """The queue as something a person can face, and it WRITES NOTHING.

    Measured 2026-09-07: 106 conclusions waiting, all of kind `observation`,
    growing about fifty a day, and the only way out is this tool at a terminal.
    That is a volume problem rather than an adjudication one — nobody reads fifty
    interpretations a day, so every one of them would leave by retention instead,
    which `tools/corroborate.py` calls "not review" in its own docstring.

    What is deliberately NOT here is a `--yes`. `require_terminal` exists because
    everything written by this tool is owned by `operator` — exempt from
    retention and unsupersedable by any agent — and minting that from a script
    would let anything with shell access forge it. A digest needs no authority:
    it groups, counts and dates, so ONE human decision can cover many rows
    instead of one.
    """
    import json as _json
    import sqlite3 as _sq
    # READ-ONLY. `store_db.connect()` opens read-write and applies pending
    # migrations, so a command that only reads was a writer into the store it
    # reports on, and its own test caught it by watching the mtime.
    conn = _sq.connect(f"file:{paths.DB}?mode=ro", uri=True)
    conn.row_factory = _sq.Row
    horizon = 90
    try:
        cfg = _json.loads(paths.config_file("retention.json").read_text(encoding="utf-8"))
        horizon = int(cfg["ledger"]["proposed_days"])
    except Exception:
        pass
    rows = conn.execute(
        "SELECT l.memory_id, l.project_id, l.kind, l.owner, l.created_at,"
        "       l.confidence, l.revision, l.evidence_json,"
        "       substr(l.statement, 1, 150) AS gist"
        " FROM ledger l JOIN (SELECT memory_id, MAX(revision) rev FROM ledger"
        "   GROUP BY memory_id) m ON l.memory_id = m.memory_id AND l.revision = m.rev"
        " LEFT JOIN tombstones t ON t.memory_id = l.memory_id"
        " WHERE t.memory_id IS NULL AND l.state = 'proposed'"
        " ORDER BY l.project_id, l.created_at DESC").fetchall()
    if not rows:
        print("no ledger conclusion is waiting for a decision")
        report_proposals(conn)
        return 0

    from datetime import datetime, timezone, timedelta
    from collections import Counter as _Counter

    def _ord(stamp) -> float:
        """A comparable moment, or the beginning of time. An unparseable stamp
        must not sort as the newest thing in the queue."""
        try:
            return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).timestamp()
        except (TypeError, ValueError):
            return 0.0
    _cls: dict[str, str] = {}
    now = datetime.now(timezone.utc)
    by_project: dict[str, list] = {}
    for r in rows:
        by_project.setdefault(r["project_id"] or "(no project)", []).append(r)

    soon = []
    # WHAT THE QUEUE RESTS ON, not only how big it is. Measured 2026-09-07: of
    # 130 waiting records, 107 rested on nothing but the working tree's own
    # rhythm — commits, dirty, clean — and this list was ordered by how many
    # each project had produced, so one project's eighteen notes about its own
    # commit rhythm sat above "the assistant-preview project has been deleted or
    # absorbed". `estate.conclusion_class` reads the delta kinds each record
    # cites, which is a fact about what it was built from rather than a
    # judgement about what it says.
    import estate
    for pid, items in by_project.items():
        for r in items:
            _cls[r["memory_id"]] = estate.conclusion_class(r["evidence_json"])
    shape = _Counter(_cls.values())
    print(f"{len(rows)} conclusion(s) waiting, across {len(by_project)} project(s): "
          f"{shape.get('structural', 0)} rest on a structural change, "
          f"{shape.get('routine', 0)} on the working tree's own rhythm"
          + (f", {shape['unclassified']} on evidence this cannot read"
             if shape.get("unclassified") else "")
          + f". Retention tombstones a `proposed` row after {horizon} days"
          + (f", except rows owned by {', '.join(_exempt)}, which it never "
             f"erases — those are marked `kept` and still wait for a decision."
             if (_exempt := _retention.exempt_owners()) else "."))
    # AND HOW MUCH OF IT A CORRECTED POLICY WOULD NEVER HAVE MADE. Measured
    # 2026-09-07: 43 of 123 observer records, a third of the queue, exist only
    # because every tick appended one before `agent/observe.py` began correcting
    # one per project per day on that same day. They are not 43 decisions
    #.
    _fold = estate.fold_groups([dict(r) for r in rows])
    _extra = sum(len(g["fold"]) for g in _fold)
    if _extra:
        print(f"{_extra} of them are superseded readings of a day the one-per-day "
              f"policy now corrects in place, in {len(_fold)} group(s). "
              f"`review.py reject-group <project> <day>` folds one group, from a "
              f"terminal — one decision instead of {_extra}.")
    print()

    def _project_key(kv):
        """Structure first, then soonest to expire. A project is ranked by its
        BEST class, so one structural note lifts the project it belongs to — the
        operator is being pointed at a decision, not at a tidy grouping."""
        pid_, its = kv
        best = min((estate.conclusion_rank(_cls[r["memory_id"]]) for r in its),
                   default=99)
        oldest = min((r["created_at"] or "9999" for r in its), default="9999")
        return (best, oldest, pid_)

    for pid, items in sorted(by_project.items(), key=_project_key):
        ages, kept = [], 0
        for r in items:
            try:
                made = datetime.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            # THROUGH `retention.days_left`, which is the only reader that
            # applied `owner_exempt`. This computed `horizon - age` for every
            # row and printed a countdown for rows retention will never touch —
            # a deadline on the operator's attention that does not exist
            #.
            left = _retention.days_left(r["owner"], "proposed", r["created_at"])
            if left is None:
                kept += 1
                continue
            ages.append(left)
            if left <= 14:
                soon.append((left, r["memory_id"], pid))
        # BOTH, when a group holds both. The first version printed the age
        # window and dropped the `kept` count whenever any row still had a
        # deadline — so a group of one deadlined row and two irreplaceable ones
        # read exactly like a group of one.
        window = (f"{min(ages)}–{max(ages)}d left" if ages
                  else "no deadline" if kept else "age unknown")
        if kept:
            window += f", {kept} kept"
        klasses = _Counter(_cls[r["memory_id"]] for r in items)
        label = ", ".join(f"{n} {k}" for k, n in sorted(
            klasses.items(), key=lambda kv: estate.conclusion_rank(kv[0])))
        print(f"  {len(items):3}  {pid}   ({window}; {label})")
        # THE RECORD THAT LIFTED THE PROJECT, not merely the newest. Ranking a
        # project by its best class and then printing a routine gist under a
        # `structural` label hides the reason it is at the top: a project lifted
        # by one structural note would otherwise show a routine commit count.
        newest = min(items, key=lambda r: (estate.conclusion_rank(_cls[r["memory_id"]]),
                                           -_ord(r["created_at"])))
        print(f"       {_cls[newest['memory_id']]}: {newest['gist']}")
        # NOT the raw value. A mechanical record carries NO confidence — a
        # `git status` is not fifty per cent likely — and printing `None` in a
        # column of real judgements reads as a broken field rather than an
        # absent one.
        print(f"       {newest['memory_id']}  "
              f"confidence {shown(newest['confidence'])}")
    if soon:
        print(f"\n{len(soon)} row(s) will be ERASED UNREVIEWED within 14 days:")
        for left, mid, pid in sorted(soon)[:10]:
            print(f"  {left:3}d  {mid}  {pid}")
    report_proposals(conn)

    print("\nPromote or reject one with `review.py promote <memory_id>` — from a "
          "terminal, because operator authority is not mintable from a script.")
    return 0


def cmd_show(conn, args) -> int:
    hist = L.history(conn, args.memory_id)
    if not hist:
        sys.exit(f"review: {args.memory_id} does not exist")
    cur = hist[-1]
    print(f"{cur['memory_id']}  revision {cur['revision']}  state {cur['state']}")
    print(f"  owner       {cur['owner']}   "
          f"confidence {shown(cur['confidence'])}")
    print(f"  project     {cur['project_id']}   kind {cur['kind']}   "
          f"function {cur['function']}   scope {cur['scope']}")
    print(f"  statement   {cur['statement']}")
    print(f"  why         {shown(cur['why'])}")
    for label, blob in (("provenance", cur["provenance_json"]),
                        ("evidence", cur["evidence_json"])):
        for item in json.loads(blob or "[]"):
            print(f"  {label:<11} {json.dumps(item, ensure_ascii=False)[:140]}")
    print(f"  history     " + " -> ".join(f"r{h['revision']}:{h['state']}" for h in hist))
    nxt = NEXT.get(cur["state"])
    print(f"\n  next        {'promote -> ' + nxt if nxt else 'terminal; nothing to promote'}")
    return 0


def cmd_promote(conn, args) -> int:
    require_terminal("promote")
    cur = L.current(conn, args.memory_id)
    if cur is None:
        sys.exit(f"review: {args.memory_id} does not exist")
    nxt = NEXT.get(cur["state"])
    if not nxt:
        sys.exit(f"review: {args.memory_id} is {cur['state']!r}; there is nothing above it")
    out = L.transition(conn, args.memory_id, to_state=nxt, owner=L.OPERATOR,
                       expected_revision=cur["revision"],
                       why=args.why or cur["why"])
    print(f"promoted {args.memory_id}: {cur['state']} -> {out['state']} "
          f"(revision {out['revision']}, owner {out['owner']})")
    return 0


def cmd_reject(conn, args) -> int:
    require_terminal("reject")
    cur = L.current(conn, args.memory_id)
    if cur is None:
        sys.exit(f"review: {args.memory_id} does not exist")
    out = L.transition(conn, args.memory_id, to_state="rejected", owner=L.OPERATOR,
                       expected_revision=cur["revision"], why=args.why)
    print(f"rejected {args.memory_id} (revision {out['revision']}). "
          f"Retention drops a rejected record after 30 days; the revisions stay.")
    return 0


def cmd_reject_group(conn, args) -> int:
    """Reject every record of one (project, day) group but its latest reading.

    ONE DECISION PER GROUP, not per record. Measured 2026-09-07: 43 of the 123
    waiting observer records — a third of the whole queue — existed only because
    every tick appended a new record before `agent/observe.py` began correcting
    one per project per day on that same day. They are not 43 decisions; they are
    23 decisions presented 43 times.

    `require_terminal` FIRST and unchanged. Everything written here is owned by
    `operator`, and a batch path is exactly where such a guard gets weakened for
    convenience — so it is not. The guard is about WHERE the command runs, and a
    row count buys no exemption from it. There is still no `--yes`.

    `superseded` would be the natural state and is unreachable: `proposed` goes
    only to `observed` or `rejected` (`store/ledger.py`). So this rejects, and
    the reason it writes says which policy made the record unnecessary.
    """
    require_terminal("reject a whole group")
    import estate
    rows = [dict(r) for r in conn.execute(
        "SELECT l.memory_id, l.project_id, l.kind, l.owner, l.created_at, l.revision"
        " FROM ledger l JOIN (SELECT memory_id, MAX(revision) rev FROM ledger"
        "   GROUP BY memory_id) m ON l.memory_id = m.memory_id AND l.revision = m.rev"
        " LEFT JOIN tombstones t ON t.memory_id = l.memory_id"
        " WHERE t.memory_id IS NULL AND l.state = 'proposed'"
        "   AND l.project_id = ? AND substr(l.created_at, 1, 10) = ?",
        (args.project, args.day))]
    groups = [g for g in estate.fold_groups(rows)]
    if not groups:
        print(f"review: nothing to fold for {args.project} on {args.day} — "
              f"{len(rows)} waiting record(s), no (project, kind, owner, day) group "
              f"holds more than one")
        return 0
    done = 0
    for g in groups:
        print(f"keeping {g['keep']} — the latest reading of {args.day}")
        for mid in g["fold"]:
            cur = L.current(conn, mid)
            if cur is None:
                print(f"  {mid}: gone")
                continue
            L.transition(conn, mid, to_state="rejected", owner=L.OPERATOR,
                         expected_revision=cur["revision"],
                         why=args.why or (
                             f"folded into {g['keep']}: one record per project per day "
                             f"is the policy since 2026-09-07, and this record exists "
                             f"only because an earlier version appended one per tick"))
            done += 1
            print(f"  rejected {mid}")
    print(f"\n{done} record(s) rejected, {len(groups)} group(s) folded. Retention "
          f"drops a rejected record after 30 days; the revisions stay.")
    return 0


def cmd_tombstone(conn, args) -> int:
    require_terminal("tombstone")
    cur = L.current(conn, args.memory_id)
    if cur is None:
        sys.exit(f"review: {args.memory_id} does not exist")
    L.tombstone(conn, args.memory_id, reason=args.why, approved_by=L.OPERATOR)
    print(f"tombstoned {args.memory_id}. The ledger row stays — an erasure leaves "
          f"a trace, and a missing audit trail is indistinguishable from a bug.")
    return 0


def _proposal(conn, pid: str) -> dict:
    row = conn.execute(
        "SELECT id, target_id, patch_json, evidence_json, status, created_at"
        " FROM proposals WHERE id = ?", (pid,)).fetchone()
    if row is None:
        sys.exit(f"review: proposal {pid} does not exist")
    if row["status"] != "proposed":
        sys.exit(f"review: proposal {pid} is already {row['status']!r}")
    return dict(row)


def cmd_proposal_accept(conn, args) -> int:
    """Land an accepted proposal in the CURATION file, not in the registry.

    `registry/*.json` is rewritten whole from the model on every emit, so a
    patch written there survives until the next tick and no longer. The
    overrides files are what the emitter applies on top of what it measured, and
    they are therefore where a human decision belongs — which is also why
    `proposals.appliable()` derives the appliable field set from them rather
    than restating it.

    Until this existed the queue had a reporter and no DECIDER: measured
    2026-09-07, `review.py` could promote, reject and tombstone a LEDGER row by
    `memory_id` and nothing anywhere could accept a registry proposal. A caller
    was answered "proposed" for a row that could only ever be listed.
    """
    require_terminal("accept a registry proposal")
    row = _proposal(conn, args.id)
    patch = json.loads(row["patch_json"] or "{}")
    # UNWRAPPED. `proposals_add` stores `{"owner": …, "evidence": [...]}` — the
    # owner is provenance the ledger adds — and the curation file wants the
    # caller's references. The owner is not lost: it travels in `proposed_by`
    # beside the proposal's id and date.
    stored = json.loads(row["evidence_json"] or "[]")
    evidence = (stored.get("evidence") if isinstance(stored, dict) else stored) or []
    why = proposals.refusal(row["target_id"], patch, evidence)
    if why:
        # Re-checked at the decision, not trusted from the wire. A proposal
        # queued before a curation file changed shape may no longer be
        # appliable, and applying it anyway would write a field the emitter
        # ignores — a decision with no effect, which is worse than a refusal.
        sys.exit(f"review: this proposal cannot be applied — {why}")
    prefix = next(p for p in proposals.LANDS_IN if row["target_id"].startswith(p))
    name, key = proposals.LANDS_IN[prefix]
    f = paths.config_file(name)
    doc = json.loads(f.read_text(encoding="utf-8"))
    before = doc[key].get(row["target_id"], {})
    merged = {**before, **patch}
    if "resources" in patch:
        # APPEND, never replace: two agents each reporting what they created
        # propose against the same starting list, and a plain overwrite would let
        # the second acceptance silently drop the first one's resource.
        import organizations
        merged["resources"] = organizations.merge_resources(before.get("resources") or [], patch["resources"])
    # PROVENANCE, written by the decision rather than proposed by the caller.
    # The next reader of this file needs to know who suggested it, on what, and
    # when a person agreed — otherwise an override is a value with no argument
    # behind it, which is how a curated file becomes unmaintainable.
    merged["why"] = args.why
    merged["proposed_by"] = f"{row['id']} ({row['created_at']})"
    merged["evidence"] = evidence
    doc[key][row["target_id"]] = merged
    atomic.write_json(f, doc)
    with conn:
        conn.execute("UPDATE proposals SET status='accepted', decided_by=?,"
                     " decided_note=?, decided_at=? WHERE id=?",
                     (L.OPERATOR, args.why, L._now(), row["id"]))
    # The file actually written, not a checkout-relative name: curation lives in
    # the selected workspace, and a receipt naming another file sends the
    # operator to diff the wrong one.
    print(f"accepted {row['id']}: {', '.join(sorted(patch))} -> "
          f"{f}#{row['target_id']}")
    print("The registry changes on the next emit, not now — `project-observatory full "
          "emit` applies it, and the diff is what you commit.")
    return 0


def cmd_proposal_reject(conn, args) -> int:
    require_terminal("reject a registry proposal")
    row = _proposal(conn, args.id)
    with conn:
        conn.execute("UPDATE proposals SET status='rejected', decided_by=?,"
                     " decided_note=?, decided_at=? WHERE id=?",
                     (L.OPERATOR, args.why, L._now(), row["id"]))
    print(f"rejected {row['id']} against {row['target_id']}. The row stays with "
          f"the reason, so the same patch arriving again can be recognised.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--state", default="proposed")
    p.add_argument("--kind"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("digest", help="the queue grouped, with what retention will erase")
    p.set_defaults(fn=cmd_digest)
    p = sub.add_parser("show"); p.add_argument("memory_id"); p.set_defaults(fn=cmd_show)
    p = sub.add_parser("promote"); p.add_argument("memory_id")
    p.add_argument("--why"); p.set_defaults(fn=cmd_promote)
    for name, fn in (("reject", cmd_reject), ("tombstone", cmd_tombstone)):
        p = sub.add_parser(name); p.add_argument("memory_id")
        p.add_argument("--why", required=True,
                       help="not optional: a decision with no reason cannot be "
                            "reviewed by the person who finds it later")
        p.set_defaults(fn=fn)
    p = sub.add_parser("reject-group",
                       help="reject a (project, day) group's superseded readings, "
                            "keeping its latest — one decision instead of many")
    p.add_argument("project"); p.add_argument("day")
    p.add_argument("--why", help="optional here: the default reason names the policy "
                                 "that made these records unnecessary")
    p.set_defaults(fn=cmd_reject_group)
    # THE REGISTRY QUEUE, which had a reporter and no decider. Keyed by a
    # proposal id rather than a `memory_id`: it is a different queue, and
    # sharing the argument name would invite the wrong id.
    for name, fn in (("accept-proposal", cmd_proposal_accept),
                     ("reject-proposal", cmd_proposal_reject)):
        p = sub.add_parser(name, help="decide a registry proposal from "
                                      "`observatory_propose`")
        p.add_argument("id")
        p.add_argument("--why", required=True,
                       help="not optional: an override with no argument behind it "
                            "is a value the next reader cannot maintain")
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    conn = store_db.connect()
    conn.row_factory = sqlite3.Row
    try:
        return args.fn(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
