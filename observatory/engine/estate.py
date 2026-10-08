#!/usr/bin/env python3
"""What counts as this estate's OWN work, in one place.

Two components answer that question and until 2026-09-06 only one of them asked
it. `tools/record_turn.py` — the companion plugin's recorder — refused to write a
session record for anything but an `owned` or `work-bitbucket` project, on the
grounds that "a third-party clone is somebody else's history, and a `why` written
about it is noise the dashboard carries forever". `collectors/scan_events.py`
applied no such rule and recorded every commit in every checkout.

Measured before the rule was shared, a large share of the commits inside the
retention window came from a handful of `external` projects — third-party
clones kept for reference. A statistic built on that answers "where was work
done" with the name of somebody else's project.

One rule, two readers. The same shape as the retention horizon, which
`store/retention.json` holds for both the collector and the pruner after they
spent a live tick deleting and re-inserting each other's rows (trap T25).
"""
from __future__ import annotations

#: Project ownership values whose history is the operator's own work.
#: A project outside this set is still watched, still in the registry, and still
#: shows its activity date — it just contributes no EVENTS, because its commits
#: are not this estate's activity.
#:
#: `local-only` belongs here although it was once missing: the set was drawn
#: against a stranger's history, and an unpublished folder is the opposite of
#: that — the operator's own work, existing on no remote at all. Excluding it
#: meant that local projects with real commits contributed nothing to the store
#: even once the loop could reach them. A `local-only` folder that is not a git
#: repository has no history to read, so the rule changes nothing for it.
RECORDED_OWNERSHIP = frozenset({"owned", "work-bitbucket", "local-only"})


def records_events(ownership: str | None) -> bool:
    """True when a project's commits belong in the event store.

    `None` means the repository is attached to no project. Those are recorded:
    an unattributed checkout is a gap in the registry, and dropping its history
    would hide the gap instead of showing it."""
    return ownership is None or ownership in RECORDED_OWNERSHIP


def why_excluded(ownership: str) -> str:
    return (f"ownership {ownership!r}; only {sorted(RECORDED_OWNERSHIP)} are recorded — "
            "a third-party clone's commits are somebody else's history")


def undeclared_owner_reason(owners: list[str], repos: int, checked_out: int) -> str:
    """What an organisation the listing returned and `OWNED_ORGS` omits COSTS.

    **Here because the sentence is about ownership, and because it has to be
    testable.** `collectors/merge.py` runs at module level — importing it
    performs a live collection — so a function defined there can only be driven
    by running the collector, which is how its first version came to state a
    consequence nobody had checked.

    That consequence is real for the class and **not for every instance**: an
    undeclared owner whose repositories are cloned here is losing recorded work
    every turn, because `records_events` refuses them and so does the
    companion's recorder. One with no checkout (`local_folders: []`) has no
    session to lose, and the effect is a classification the operator may not
    care about. Saying the alarming half either way spends the operator's
    attention on the case that costs nothing and teaches them to skip the case
    that costs work.

    Never auto-declared: which organisations are the operator's is the
    operator's fact, and a script that minted it would make "owned" mean "seen
    by a token".
    """
    who = ", ".join(owners)
    head = (f"the GitHub listing returned {repos} repositor"
            f"{'y' if repos == 1 else 'ies'} under {who}, which OWNED_ORGS "
            f"(`organizations` in the workspace's config/ownership.json) does not "
            f"declare, so their projects are reported as `external`")
    if checked_out:
        cost = (f". {checked_out} of them {'is' if checked_out == 1 else 'are'} "
                f"cloned on this machine, so work in "
                f"{'it' if checked_out == 1 else 'them'} is observed and then "
                f"dropped: `estate.records_events` refuses the commits and the "
                f"companion's recorder declines the session")
    else:
        cost = (". None of them is cloned here, so no session is being lost and "
                "the effect is classification only")
    ask = ("Add the organisation to OWNED_ORGS if it is yours"
           if len(owners) == 1 else
           "Add them to OWNED_ORGS if they are yours")
    return (head + cost + f". {ask} — that is an operator's decision, not "
            "a collector's")


# ─────────────────── what a waiting conclusion RESTS ON ──────────────────────
#
#: The three delta kinds an ordinary working day produces. A developer commits,
#: the tree goes dirty, the tree goes clean, and `last_activity_on` moves with
#: them — that oscillation is the working rhythm, not news about the estate.
#:
#: Measured 2026-09-07 over the 130 records genuinely waiting for a decision:
#: **107 rest on nothing but these three**, and one project alone had eighteen
#: notes about its own commit rhythm sitting at the top of the operator's queue,
#: because `tools/review.py digest` sorted projects by how many each had
#: produced. The seventeen that rest on something else — a project appearing or
#: vanishing, an owner changing, a repository or a stack arriving — are the ones
#: a person is actually needed for.
#:
#: `last_activity_on-changed` belongs HERE rather than among the structural
#: kinds, and that was checked by reading rather than assumed: the nine records
#: carrying all three read "a single commit was made while the working tree
#: became dirty". It accompanies ordinary work. It appears beside the anomalies
#: too ("the commit count increased tenfold in three days"), which is why the
#: anomalies are not separated out here — no mechanical signal distinguishes
#: them, and inventing one would be a judgement wearing a rule's clothes.
ROUTINE_DELTA_KINDS = frozenset({
    "commits-changed", "dirty-changed", "last_activity_on-changed",
})

#: Sort order for the three classes. `structural` first because it is what a
#: person is needed for; `unclassified` SECOND rather than last, because a
#: record whose evidence cannot be read might be either and demoting it to the
#: cheap class is how a queue hides the thing that mattered.
_RANK = {"structural": 0, "unclassified": 1, "routine": 2}


def conclusion_class(evidence_json: str | None) -> str:
    """`structural`, `routine` or `unclassified`, from what the record cites.

    A FACT rather than a judgement: `evidence_json` records the delta kinds a
    conclusion was built from, so this reads what it rests on and never what it
    says. Confidence was measured as the alternative and rejected — among the
    restatements 0.95, 0.9, 0.9, 0.85, 0.7, 0.6, and among the lifecycle and
    anomaly conclusions 0.9, 0.9, 0.85, 0.7, 0.6. Orthogonal, so sorting by it
    would have looked principled and changed nothing.

    THREE OUTCOMES. Unreadable, absent, or placeholder evidence is
    `unclassified`, never `routine`.

    One rule, two readers: `tools/review.py digest` orders by it and
    `ledger.review_backlog` reports the split, the same shape as
    `RECORDED_OWNERSHIP` above.
    """
    import json as _json
    try:
        items = _json.loads(evidence_json or "[]")
    except (ValueError, TypeError):
        return "unclassified"
    if not isinstance(items, list) or not items:
        return "unclassified"
    kinds = set()
    for it in items:
        if isinstance(it, dict):
            k = str(it.get("kind") or "").strip()
            if k and k != "?":
                kinds.add(k)
    if not kinds:
        return "unclassified"
    return "routine" if kinds <= ROUTINE_DELTA_KINDS else "structural"


def conclusion_rank(klass: str) -> int:
    """Sort key for a class. An unknown name sorts last rather than first."""
    return _RANK.get(klass, max(_RANK.values()) + 1)


# ──────────── records a corrected policy would never have created ────────────
#
# `agent/observe.py` keeps ONE record per project per day and CORRECTS it — a
# new revision, not a new record — since 2026-09-07. Before that day every tick
# appended. Measured the same day: 23 (project, day) groups still held more than
# one waiting record and 43 records existed that the policy would never have
# made, a third of the operator's whole queue, all of them from 09-04, 05 and
# 06 and none from 09-07. The rule holds; this is what the fix left behind.
#
# The key below is the policy's OWN, read from its query rather than inferred:
#
#     project_id = ? AND kind = 'observation' AND owner = ?
#       AND substr(created_at, 1, 10) = ?
#       AND memory_id NOT IN (SELECT memory_id FROM tombstones)
#
# Four parts. A fold on three of them would merge what the policy keeps apart.


#: (kind, owner) pairs whose writer keeps ONE record per project per DAY. Only
#: these may be folded, and the default is therefore "not folded".
#:
#: The first version of `fold_groups` had no such scope and folded every waiting
#: record. Measured immediately: it grouped 2 `session` records written by
#: `agent:claude-code` and 1 `estate-history` record — and the session policy is
#: one record per SESSION, not per day, so two sessions in one day are
#: two legitimate records and folding them would destroy exactly what the
#: four-part key was written to protect. `estate-history`'s policy was not read
#: at all, which is its own reason not to touch it.
#:
#: A writer added here must have had its policy READ first. The safe default is
#: the empty case, and this list carries its reason like every other exemption
#: list in this repository.
ONE_PER_DAY_WRITERS = frozenset({
    ("observation", "agent:observer"),
})


def residue_key(record: dict) -> tuple:
    """The policy's grouping key for one record.

    A record with no readable date gets a key of its own — its `memory_id` —
    rather than joining the group of some other record's day. A row cannot be
    folded into a day it cannot be shown to belong to.
    """
    stamp = record.get("created_at")
    day = str(stamp)[:10] if isinstance(stamp, str) and len(str(stamp)) >= 10 else None
    if day is None:
        return ("__undated__", record.get("memory_id"))
    return (record.get("project_id"), record.get("kind"), record.get("owner"), day)


def fold_groups(records: list[dict]) -> list[dict]:
    """Groups holding more than one record, with which survives and which fold.

    Returns `[{"key": …, "keep": memory_id, "fold": [memory_id, …]}]`, only for
    groups with two or more. The survivor is the LATEST reading: each record of a
    day restates the project's state at its moment, and the policy that replaced
    this behaviour corrects one record so that only the latest survives.

    **What folding loses, stated rather than hidden.** The earlier records of a
    day are different moments — the tree went dirty, the work was committed, it
    went dirty again — and keeping only the last discards those readings. That is
    precisely what the current policy does every day. Reproducing its outcome is
    the point.

    This function DECIDES NOTHING. `proposed` can only become `observed` or
    `rejected` (`store/ledger.py`'s transition table — `superseded` is reachable
    from `supported` and `contested` alone), and both are owned by `operator`
    behind `require_terminal`. So the fold is a proposal for one decision per
    group, never a sweep.
    """
    buckets: dict[tuple, list[dict]] = {}
    for r in records:
        buckets.setdefault(residue_key(r), []).append(r)
    out: list[dict] = []
    for key, group in buckets.items():
        if len(group) < 2 or key[0] == "__undated__":
            continue
        # THE WRITER'S OWN POLICY, or nothing. See ONE_PER_DAY_WRITERS.
        if (key[1], key[2]) not in ONE_PER_DAY_WRITERS:
            continue
        ordered = sorted(group, key=lambda r: (str(r.get("created_at") or ""),
                                               int(r.get("revision") or 0),
                                               str(r.get("memory_id") or "")))
        keep = ordered[-1]
        out.append({"key": key, "keep": keep.get("memory_id"),
                    "fold": [r.get("memory_id") for r in ordered[:-1]]})
    return sorted(out, key=lambda g: (str(g["key"][0]), str(g["key"][-1])))
