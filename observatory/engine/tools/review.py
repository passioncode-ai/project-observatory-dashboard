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

THE STANDING POLICY (OBS-227, docs/design/REVIEW-POLICY.md). A person may delegate a closed set
of safe decisions once; the tick applies them, never as the operator:

    review.py policy show [--json]               the rules and what each would decide now; writes nothing
    review.py policy enable [--only RULE …]      at a terminal: enable exactly the rules shown
    review.py policy disable                     at a terminal
    review.py policy run [--dry-run]             what the tick runs: applies an ENABLED policy only;
                                                 every decision is `policy:<rule>@<hash>`, journaled
    review.py undo-policy <decision-id …> | --since TIME    at a terminal: take decisions back
    review.py batch prepare [--out FILE] [--include-routine]
                                                 what waits for a person, with recommendations an
                                                 agent may edit; writes only FILE
    review.py batch apply FILE                   at a terminal: shows the totals, asks ONCE, applies
                                                 every accept and reject as the operator
"""
from __future__ import annotations
import argparse, json, os, pathlib, sqlite3, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths                                                                      
import atomic                                                                     
import proposals                                                                  
from store import db as store_db                                                  
from store import ledger as L                                                     
from store import retention as _retention                              
import review_policy as RP

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
        kept = _kept_after_lost_lease(r)
        if kept:
            # A STEP KEPT AFTER A LOST LEASE is not a conclusion to promote: it
            # is a session's work on a workflow another executor had taken. It
            # says which workflow, so the decision is whether that work still
            # matters there — `workflow show` answers that.
            print(f"    kept after a lost lease — workflow {kept}: "
                  f"`project-observatory full workflow show {kept}`")
    return 0


def _kept_after_lost_lease(row) -> str | None:
    """The workflow a step was written for, when it was kept because the session
    no longer held the workflow's lease; otherwise None."""
    keys = row.keys()
    if row["kind"] != "step_result" or "workflow_id" not in keys or not row["workflow_id"]:
        return None
    try:
        prov = json.loads(row["provenance_json"] or "[]")
    except ValueError:
        return None
    if any(isinstance(x, dict) and x.get("source") == "lease-lost" for x in prov):
        return row["workflow_id"]
    return None


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
    conn = _sq.connect(paths.sqlite_uri(paths.DB), uri=True)
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
          # BY KIND, from the one reader of the rule (`retention.horizon_for`): a lesson
          # waits 365 days for a second project, and saying 90 would be a false deadline.
          + "".join(f", a {k} after {_retention.horizon_for('proposed', k)} days"
                    if _retention.horizon_for("proposed", k) is not None
                    else f", a {k} never by age"
                    for k, kc in sorted(_retention.kind_horizons().items())
                    if "proposed_days" in kc)
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
            # AND BY KIND: a lesson waits 365 days for a second project, not 90.
            left = _retention.days_left(r["owner"], "proposed", r["created_at"], kind=r["kind"])
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


class Refused(Exception):
    """A decision this queue cannot apply, with the reason a person reads."""


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
    try:
        # THE REVIEW LOCK, shared with the standing policy's run (OBS-227): both read and rewrite
        # the same curation files, and two read-modify-writes in flight lose one of them.
        with RP.lock(wait=True):
            # An unfinished policy run is settled first, so the person decides the proposal as it
            # stands after it, not over files a crashed run left behind.
            _recover(conn)
            row = _proposal(conn, args.id)
            for line in accept_proposal(conn, row, args.why):
                print(line)
    except Refused as exc:
        sys.exit(f"review: {exc}")
    return 0


def accept_proposal(conn, row: dict, why: str) -> list[str]:
    """Apply an accepted registry proposal as the OPERATOR; the lines that say what was written.

    Called only behind `require_terminal`: from `accept-proposal` and from `batch apply`. Raises
    Refused when it cannot be applied as it stands, and first of all when the proposal is no longer
    pending: the status is read again here, not trusted from the caller's row, because a batch file
    is written by an agent and a decided proposal must not be decided a second time."""
    status = _status(conn, row["id"])
    if status != "proposed":
        raise Refused(f"{row['id']} is not pending (it is {status or 'gone'}); nothing was written")
    patch = json.loads(row["patch_json"] or "{}")
    # UNWRAPPED. `proposals_add` stores `{"owner": …, "evidence": [...]}` — the
    # owner is provenance the ledger adds — and the curation file wants the
    # caller's references. The owner is not lost: it travels in `proposed_by`
    # beside the proposal's id and date.
    stored = json.loads(row["evidence_json"] or "[]")
    evidence = (stored.get("evidence") if isinstance(stored, dict) else stored) or []
    refused = proposals.refusal(row["target_id"], patch, evidence)
    if refused:
        # Re-checked at the decision, not trusted from the wire. A proposal
        # queued before a curation file changed shape may no longer be
        # appliable, and applying it anyway would write a field the emitter
        # ignores — a decision with no effect, which is worse than a refusal.
        raise Refused(f"this proposal cannot be applied — {refused}")
    prefix = next(p for p in proposals.LANDS_IN if row["target_id"].startswith(p))
    provenance = {"why": why, "proposed_by": f"{row['id']} ({row['created_at']})", "evidence": evidence}
    written = []
    if "aliases" in patch:
        # OTHER NAMES land in the table of names, not in the target's own row:
        # `identifiers.py` reads that table, and an alias in an override row would
        # be a field the emitter ignores. A name that already names something else
        # is refused here — accepting it would make it ambiguous for every reader.
        import identifiers
        fi = paths.config_file(proposals.ALIASES_LAND_IN)
        try:
            current = identifiers.load_table(fi)
            table = identifiers.merge_accepted(current, patch["aliases"], target=row["target_id"],
                                               on=L._now()[:10], provenance=provenance)
        except identifiers.AliasConflict as exc:
            raise Refused(f"this proposal cannot be applied — {exc}") from None
        except ValueError as exc:
            raise Refused(f"{fi} could not be read: {exc}") from None
        atomic.write_json(fi, table)
        written.append(f"{fi} ({len(patch['aliases'])} alias(es))")
        patch = {k: v for k, v in patch.items() if k != "aliases"}
    if not patch:
        _mark_accepted(conn, row, why)
        return [f"accepted {row['id']}: aliases -> {', '.join(written)}", EMIT_NOTE]
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
    merged["why"] = why
    merged["proposed_by"] = provenance["proposed_by"]
    merged["evidence"] = evidence
    doc[key][row["target_id"]] = merged
    atomic.write_json(f, doc)
    _mark_accepted(conn, row, why)
    # The file actually written, not a checkout-relative name: curation lives in
    # the selected workspace, and a receipt naming another file sends the
    # operator to diff the wrong one.
    return [f"accepted {row['id']}: {', '.join(sorted(patch))} -> "
            f"{f}#{row['target_id']}" + (f"; aliases -> {', '.join(written)}" if written else ""), EMIT_NOTE]


EMIT_NOTE = ("The registry changes on the next emit, not now — `project-observatory full "
             "emit` applies it, and the diff is what you commit.")


def _status(conn, pid: str) -> str | None:
    found = conn.execute("SELECT status FROM proposals WHERE id = ?", (pid,)).fetchone()
    return found[0] if found else None


def _recover(conn) -> None:
    """`RP.recover` for a person's command, under the lock the caller holds, saying what it did."""
    rec = RP.recover(conn)
    if rec["reverted"] or rec["left"]:
        print(f"review: settled a policy run that did not finish: {rec['reverted']} decision(s) reverted, "
              f"{rec['left']} left as a person decided them")
    for line in rec["degraded"]:
        print(f"review: degraded: {line}")


def _mark_accepted(conn, row: dict, why: str) -> None:
    # CONDITIONAL, like the rejection: a status that moved under this write is reported, never
    # overwritten.
    with conn:
        cur = conn.execute("UPDATE proposals SET status='accepted', decided_by=?,"
                           " decided_note=?, decided_at=? WHERE id=? AND status='proposed'",
                           (L.OPERATOR, why, L._now(), row["id"]))
    if cur.rowcount != 1:
        raise Refused(f"{row['id']} stopped being pending while it was accepted "
                      f"(it is {_status(conn, row['id']) or 'gone'}); its status was not changed")


def cmd_proposal_reject(conn, args) -> int:
    require_terminal("reject a registry proposal")
    try:
        # THE REVIEW LOCK and recovery, as for an acceptance: rejecting a proposal a crashed run
        # wrote into a curation file must not leave that write in force with no decision naming it.
        with RP.lock(wait=True):
            _recover(conn)
            row = _proposal(conn, args.id)
            reject_proposal(conn, row, args.why)
    except Refused as exc:
        sys.exit(f"review: {exc}")
    except RP.Busy as exc:
        sys.exit(f"review: {exc}")
    print(f"rejected {row['id']} against {row['target_id']}. The row stays with "
          f"the reason, so the same patch arriving again can be recognised.")
    return 0


def reject_proposal(conn, row: dict, why: str) -> None:
    """Reject a registry proposal as the OPERATOR; behind `require_terminal` only. Refused when the
    proposal is not pending: the update changed nothing, and saying "rejected" would be false."""
    with conn:
        cur = conn.execute("UPDATE proposals SET status='rejected', decided_by=?,"
                           " decided_note=?, decided_at=? WHERE id=? AND status='proposed'",
                           (L.OPERATOR, why, L._now(), row["id"]))
    if cur.rowcount != 1:
        raise Refused(f"{row['id']} is not pending (it is {_status(conn, row['id']) or 'gone'}); "
                      f"nothing was written")


# region the standing policy (OBS-227)

def _confirm(question: str) -> bool:
    """One question at the terminal; anything but yes is no."""
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _login() -> str:
    import getpass
    try:
        return getpass.getuser()
    except Exception:                                                              # noqa: BLE001
        return "unknown"


def _examples(items, n: int = 3) -> list[str]:
    return [f"      {getattr(it, 'item')}  {getattr(it, 'reason')[:110]}" for it in items[:n]]


def _describe_policy(conn, policy, heading: str = "") -> list[str]:
    """The rules and what each would decide on the queue as it stands. Reads only."""
    plan = RP.evaluate(conn, policy.rules_on(), limit=10**9)
    lines = [heading or (f"review policy: {policy.state}" + (f": {policy.problem}" if policy.problem else "")),
             f"  hash {policy.hash or '-'}" + (f", enabled {policy.enabled['at']} by {policy.enabled.get('by')}"
                                              if policy.enabled else "")]
    lines += [f"  note: {n}" for n in policy.notes]
    lines.append("")
    for rid, spec in RP.RULES.items():
        rule = policy.rules.get(rid) or {}
        decided = [d for d in plan.decisions if d.rule == rid]
        undecided = [w for w in plan.undecided if w.rule == rid]
        mark = "on " if rule.get("on") else "off"
        lines.append(f"  [{mark}] {rid} v{spec['version']}: {spec['decides']}")
        if decided:
            lines.append(f"      would decide {len(decided)} now:")
            lines += _examples(decided)
        elif undecided:
            lines.append(f"      would decide {len(undecided)} if it were on:")
            lines += _examples(undecided)
        else:
            lines.append("      nothing to decide now")
    person = RP.for_person(plan, policy)
    w = RP.waiting_summary(person)
    lines += ["", f"waiting for a person: {w['total']} ({w['registry']} registry proposal(s), "
                  f"{w['ledger']} ledger conclusion(s)" + (f", the oldest since {w['oldest'][:10]}" if w["oldest"] else "")
                  + ")",
              f"  left to retention (routine conclusions): {plan.retention}; to corroboration (lessons): "
              f"{plan.corroboration}; already folded: {plan.folded}"]
    lines += [f"  degraded: {d}" for d in plan.degraded]
    if w["total"]:
        lines += _examples(person)
        lines.append("  resolve them in one launch: `project-observatory full review batch prepare`, then, "
                     "at a terminal, `project-observatory full review batch apply FILE`")
    return lines


def cmd_policy(conn, args) -> int:
    policy = RP.load()
    if args.action == "show":
        if args.json:
            print(json.dumps({"policy": {"state": policy.state, "hash": policy.hash or None,
                                         "problem": policy.problem, "notes": policy.notes,
                                         "rules": policy.rules, "enabled": policy.enabled},
                              "summary": RP.summary(conn)}, indent=1, ensure_ascii=False))
            return 0
        print("\n".join(_describe_policy(conn, policy)))
        return 0
    if args.action == "run":
        receipt = RP.run(conn, apply=not args.dry_run)
        if receipt.get("skipped"):
            print(f"review-policy: skipped: {receipt['skipped']}")
            return 0
        done = receipt["applied"] or {}
        print(f"review-policy: {receipt['state']}; "
              + (f"decided {sum(done.values())} ({', '.join(f'{k} {v}' for k, v in done.items())})" if done else
                 f"would decide {sum(receipt['would'].values())}" if receipt["would"] else "nothing to decide")
              + f"; waiting for a person {receipt['waiting']['total']}"
              + (f"; deferred to the next run {receipt['deferred']}" if receipt["deferred"] else ""))
        for d in receipt["degraded"]:
            print(f"review-policy: degraded: {d}")
        return 0
    require_terminal(f"{args.action} the review policy")
    try:
        doc = RP.read_document()
    except RP.PolicyError as exc:
        sys.exit(f"review: {exc}. Repair or remove {RP.policy_path()} first.")
    at, user = L._now(), _login()
    if args.action == "disable":
        atomic.write_json(RP.policy_path(), RP.disable_document(doc, at=at, user=user))
        print(f"review policy disabled ({RP.policy_path()}); nothing is decided automatically. Its decisions "
              f"stand: `review.py undo-policy --since TIME` takes them back.")
        return 0
    only = set(args.only) if args.only else None
    unknown = sorted((only or set()) - set(RP.RULES))
    if unknown:
        sys.exit(f"review: unknown rule(s) {', '.join(unknown)}; the rules are {', '.join(RP.RULES)}")
    new = RP.enable_document(doc, only=only, at=at, user=user)
    preview = RP.parse({**new, "enabled": None})
    print("\n".join(_describe_policy(conn, preview, heading="the review policy you are about to enable:")))
    print(f"\nEnabling makes the tick apply the rules marked on, every run, as "
          f"`policy:<rule>@{new['enabled']['hash'].split(':', 1)[-1][:12]}`, never as you. "
          f"`review.py undo-policy` takes any decision back.")
    if not _confirm("Enable this policy?"):
        sys.exit("review: not enabled; nothing was written")
    atomic.write_json(RP.policy_path(), new)
    print(f"review policy enabled: {new['enabled']['hash']} ({RP.policy_path()})")
    return 0


def cmd_undo_policy(conn, args) -> int:
    require_terminal("take back the review policy's decisions")
    try:
        with RP.lock(wait=True):
            # A run that died between its steps is settled first: its decisions were never in force,
            # and what it wrote is reverted before anything is chosen.
            _recover(conn)
            entries = RP.active_decisions()
            chosen = RP.select(entries, ids=set(args.ids) if args.ids else None, since=args.since)
            missing = sorted(set(args.ids or ()) - {d["id"] for d in chosen})
            for m in missing:
                print(f"review: {m} is not a decision in force (unknown, or undone already)")
            if not chosen:
                print("review: no policy decision matches; nothing else changed")
                return 1 if missing else 0
            results = RP.undo(conn, chosen)
    except RP.Busy as exc:
        sys.exit(f"review: {exc}")
    for r in results:
        print(f"{r['id']}  {r['rule']}  {r['item']}: {r['result']}")
    print(f"{sum(r['result'].startswith('reverted') for r in results)} of {len(results)} decision(s) taken back. "
          f"While the policy stays enabled, the next run decides the same items again: "
          f"`review.py policy disable`, or turn the rule off and enable again, to keep them for a person.")
    return 0


def cmd_batch(conn, args) -> int:
    if args.action == "prepare":
        doc = RP.prepare_batch(conn, include_routine=args.include_routine)
        out = pathlib.Path(args.out) if args.out else paths.STATE / "review" / "batch.json"
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic.write_json(out, doc)
        os.chmod(out, 0o600)
        c = doc["counts"]
        print(f"{len(doc['items'])} item(s) wait for a person: recommended accept {c['accept']}, "
              f"reject {c['reject']}, keep {c['keep']} -> {out}")
        if doc["not_included"].get("retention"):
            print(f"  not included: {doc['not_included']['retention']} routine conclusion(s) retention retires "
                  f"(--include-routine lists them)")
        print("Read or edit `decision` and `reason`, then at a terminal: "
              f"`project-observatory full review batch apply {out}`")
        return 0
    return _batch_apply(conn, pathlib.Path(args.file))


def _batch_apply(conn, file: pathlib.Path) -> int:
    """Apply a batch file as the operator, after showing the person everything the file departs from.

    The file is written by an agent and every field of it, `recommended` included, can be edited, so
    nothing in it is trusted as the engine's view. The engine prepares the batch again from the queue
    as it stands (`engine_view`), and only an item that is still pending and that this fresh
    preparation lists is applied. Before the one question the person sees, in full, every item whose
    decision differs from the engine's recommendation, every reason that is not the engine's, and
    every item the engine did not prepare or that is no longer pending (those are never applied).
    Under the lock the view is taken again; an item whose engine view moved since it was shown is
    skipped, not applied on a recommendation the person never saw."""
    require_terminal("apply a review batch")
    try:
        raw = file.read_bytes()
        doc = json.loads(raw)
    except (OSError, ValueError) as exc:
        sys.exit(f"review: {file} cannot be read: {type(exc).__name__}")
    problems = RP.check_batch(doc)
    if problems:
        sys.exit("review: the batch is refused whole:\n  " + "\n  ".join(problems[:20]))
    import hashlib
    tag = hashlib.sha256(raw).hexdigest()[:12]

    def engine_view() -> dict:
        fresh = RP.prepare_batch(conn, include_routine=True)
        return {(it["queue"], it["id"]): it for it in fresh["items"]}

    def partition(engine: dict):
        out = {"act": [], "keep": [], "changed": [], "unprepared": [], "not_pending": []}
        for it in doc["items"]:
            row = RP.current_row(conn, it["queue"], it["id"])
            state = None if row is None else row["status" if it["queue"] == "registry" else "state"]
            if state != "proposed":
                out["not_pending"].append((it, state or "gone"))
            elif (it["queue"], it["id"]) not in engine:
                out["unprepared"].append(it)
            elif RP.item_hash(it["queue"], row) != it["hash"]:
                out["changed"].append(it)
            elif it["decision"] == "keep":
                out["keep"].append(it)
            else:
                out["act"].append(it)
        return out

    def departures(items, engine: dict) -> list[str]:
        lines = []
        for it in items:
            mine = engine[(it["queue"], it["id"])]
            if it["decision"] != mine["recommended"]:
                lines.append(f"    {it['id']}: {it['decision']} differs from the engine's recommendation "
                             f"{mine['recommended']} ({mine['reason'][:160]})")
            if it["reason"] != mine["reason"]:
                lines.append(f"    {it['id']}: reason changed from the engine's {mine['reason'][:160]!r} "
                             f"to {it['reason'][:160]!r}")
        return lines

    engine = engine_view()
    parts = partition(engine)
    act = parts["act"]
    totals: dict[str, int] = {}
    for it in act:
        totals[f"{it['decision']} {it['queue']}"] = totals.get(f"{it['decision']} {it['queue']}", 0) + 1
    print(f"batch {file} ({tag}): {len(act)} decision(s) to apply as the operator, {len(parts['keep'])} kept "
          f"for later, {len(parts['changed'])} changed since it was prepared and will be skipped")
    for k, v in sorted(totals.items()):
        print(f"  {k}: {v}")
    for it in act[:8]:
        print(f"    {it['decision']:<6} {it['id']}  {it['reason'][:100]}")
    moved = departures(act, engine)
    if moved:
        print(f"  where this file departs from the engine ({len(moved)}), every one:")
        print("\n".join(moved))
    for it in parts["unprepared"]:
        print(f"  will not be applied: {it['id']} was not prepared by the engine for this queue")
    for it, state in parts["not_pending"]:
        print(f"  will not be applied: {it['id']} is not pending (it is {state})")
    if not act:
        print("nothing to apply")
        return 0
    shown = {(it["queue"], it["id"]): (engine[(it["queue"], it["id"])]["recommended"],
                                      engine[(it["queue"], it["id"])]["reason"], it["decision"])
             for it in act}
    if not _confirm(f"Apply these {len(act)} decision(s) as the operator?"):
        sys.exit("review: not applied; nothing was written")
    applied, failed, skipped = [], [], []
    try:
        with RP.lock(wait=True):
            _recover(conn)
            # Again, under the lock: what was shown may have moved, and only what was shown is applied.
            engine = engine_view()
            parts = partition(engine)
            for it in parts["act"]:
                key = (it["queue"], it["id"])
                if shown.get(key) != (engine[key]["recommended"], engine[key]["reason"], it["decision"]):
                    skipped.append((it, "the queue moved after it was shown"))
                    continue
                why = f"batch {tag}: {it['reason']}"
                try:
                    _apply_item(conn, it, why)
                    applied.append(it)
                except (Refused, L.LedgerError) as exc:
                    failed.append((it, str(exc)))
    except RP.Busy as exc:
        sys.exit(f"review: {exc}")
    print(f"applied {len(applied)} decision(s) as the operator.")
    for it in parts["changed"]:
        print(f"  skipped {it['id']}: changed since the batch was prepared; prepare it again")
    for it in parts["unprepared"]:
        print(f"  not applied {it['id']}: not prepared by the engine for this queue")
    for it, state in parts["not_pending"]:
        print(f"  not applied {it['id']}: not pending (it is {state})")
    for it, why in skipped:
        print(f"  skipped {it['id']}: {why}; prepare it again")
    for it, why in failed:
        print(f"  refused {it['id']}: {why}")
    if any(it["queue"] == "registry" and it["decision"] == "accept" for it in applied):
        print(EMIT_NOTE)
    return 0


def _apply_item(conn, it: dict, why: str) -> None:
    if it["queue"] == "registry":
        row = dict(conn.execute("SELECT id, target_id, patch_json, evidence_json, status, created_at"
                                " FROM proposals WHERE id = ?", (it["id"],)).fetchone())
        if it["decision"] == "accept":
            accept_proposal(conn, row, why)
        else:
            reject_proposal(conn, row, why)
        return
    cur = L.current(conn, it["id"])
    if cur is None or cur["state"] != "proposed":
        raise Refused(f"{it['id']} is not pending (it is {cur['state'] if cur else 'gone'})")
    to = "observed" if it["decision"] == "accept" else "rejected"
    L.transition(conn, it["id"], to_state=to, owner=L.OPERATOR, expected_revision=cur["revision"], why=why)

# endregion


def parser() -> argparse.ArgumentParser:
    """The parser `main` and `observatory.refusal` share, so a handed-over command is checked
    against exactly what runs."""
    ap = argparse.ArgumentParser(prog="project-observatory full review", description=__doc__,
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
    # THE STANDING POLICY (OBS-227). `policy run` is what the tick runs; enable, disable, undo and
    # batch apply need a terminal like every other write here.
    p = sub.add_parser("policy", help="the standing review policy: show, enable, disable, run")
    p.add_argument("action", choices=("show", "enable", "disable", "run"))
    p.add_argument("--only", nargs="+", metavar="RULE", help="enable: only these rules are on")
    p.add_argument("--json", action="store_true", help="show: the policy and the counts as JSON")
    p.add_argument("--dry-run", action="store_true", help="run: count, decide nothing")
    p.set_defaults(fn=cmd_policy)
    p = sub.add_parser("undo-policy", help="take back decisions the standing policy made (a terminal)")
    p.add_argument("ids", nargs="*", metavar="DECISION")
    p.add_argument("--since", help="every decision at or after this UTC time, e.g. 2026-10-24T00:00:00Z")
    p.set_defaults(fn=cmd_undo_policy)
    p = sub.add_parser("batch", help="what waits for a person, in one file; applied in one act")
    bsub = p.add_subparsers(dest="action", required=True)
    bp = bsub.add_parser("prepare", help="write the batch file; decides nothing")
    bp.add_argument("--out", help="the file to write (default store/review/batch.json)")
    bp.add_argument("--include-routine", action="store_true",
                    help="also list routine conclusions retention would retire")
    bp.set_defaults(fn=cmd_batch)
    ba = bsub.add_parser("apply", help="at a terminal: show the totals, ask once, apply as the operator")
    ba.add_argument("file")
    ba.set_defaults(fn=cmd_batch)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    # `full review` with nothing after it lists, as it always did.
    args = ap.parse_args((list(argv) if argv is not None else sys.argv[1:]) or ["list"])
    if args.cmd == "undo-policy" and not args.ids and not args.since:
        ap.error("undo-policy needs decision ids or --since")
    conn = store_db.connect()
    conn.row_factory = sqlite3.Row
    try:
        return args.fn(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
