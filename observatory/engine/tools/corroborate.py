#!/usr/bin/env python3
"""The second witness ARCHITECTURE.md promised: check a claim against the world.

    "A `proposed` row is promoted when the operator approves it, or when a
     second, independent scan corroborates it. Nothing else promotes anything."
                                                    — docs/ARCHITECTURE.md:77

Only the first half was even reachable, and nothing called it: forty-three rows
sat proposed until retention would erase them at ninety days. Erasure by timeout
is not review.

WHAT CAN HONESTLY BE CORROBORATED, and what cannot:

* **`session` rows can.** The companion plugin records a repository, a branch and
  the HEAD sha it saw. Asking git, later and independently, whether that sha is
  still an ancestor of that branch is a real second look: it was written from
  `git status`, and this reads the object graph. If the sha is GONE — force-push,
  branch deleted, clone rebuilt — the row is NOT promoted. That is a finding, not
  a corroboration, and pretending otherwise would promote a claim about work that
  no longer exists.

* **`observation` rows cannot, and this refuses rather than pretending.** Their
  evidence is a delta that has already been consumed: `dirty-changed 60 -> 62`.
  Re-reading it proves nothing — the uncommitted-file count moves every hour, and
  a commit count only grows, so "still true" is either meaningless or trivially
  true. Those rows need a person, and the queue says so instead of quietly
  shrinking.

    corroborate.py [--dry-run]
"""
from __future__ import annotations
import json, pathlib, subprocess, sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths                                                                      
import safe_git                                                                   
from store import db as store_db                                                  
from store import ledger as L                                                     
import atomic                                                                      

BY = "agent:corroborator"


def clone_path(nwo: str) -> pathlib.Path | None:
    reg = paths.REGISTRY / "repositories.json"
    if not reg.is_file():
        return None
    for r in json.loads(reg.read_text(encoding="utf-8"))["repositories"]:
        if r["id"] == f"repository:{nwo}" and (r.get("local") or {}).get("path"):
            return pathlib.Path(r["local"]["path"])
    return None


def git(path: pathlib.Path, *args: str) -> tuple[int, str]:
    try:
        r = safe_git.run(args, repo=path, timeout=20)
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def refs_containing(path, sha: str) -> list[str]:
    """Every ref this clone knows that contains the commit, local and remote.

    `branch -a --contains` is the question "did the work survive?" asked
    directly. The named branch is a stronger claim and is tried first; this is
    the fallback that separates "the work is gone" from "the branch it was done
    on has been tidied up", which are not the same fact about the estate.
    """
    code, out = git(path, "branch", "-a", "--contains", sha)
    if code != 0:
        return []
    names = []
    for line in out.splitlines():
        name = line.strip().lstrip("* ").strip()
        # A detached HEAD prints as `(HEAD detached at …)`, which is not a ref.
        if name and not name.startswith("("):
            names.append(name)
    return names


def branch_forms(path, branch: str) -> list[str]:
    """The names one branch can go by in a clone, strongest first.

    A row records `feat/attention-rail`; after the branch is pushed and the local
    copy deleted, the clone still holds `remotes/origin/feat/attention-rail`,
    which is the same branch as far as the question "did this work survive?" is
    concerned. Resolving only the bare name is what reported four pushed commits
    as work git could no longer place.
    """
    forms = [branch]
    if "/" not in branch.split("/", 1)[0] or True:
        code, out = git(path, "remote")
        for remote in (out.split() if code == 0 else ["origin"]):
            cand = f"{remote}/{branch}"
            if cand not in forms:
                forms.append(cand)
    return forms


def check_session(row) -> tuple[bool | None, str]:
    """Did the work this row describes survive? Ask git, not the row.

    **Three outcomes, not two.** `True` a witness confirms, `False` a witness
    CONTRADICTS, `None` nothing could be asked. A pushed branch whose LOCAL copy
    was deleted afterwards still exists as its remote-tracking ref, with every
    commit, date, author and message intact; but `merge-base --is-ancestor`
    fails to resolve the bare name and exits non-zero, and reading any non-zero
    exit as "the branch moved away" reports real, pushed work as lost.
    Two outcomes collapsed into one, and it cost data: the row stays `proposed`
    and retention erases it at ninety days, so four records of real, pushed work
    were queued for deletion because a branch was tidied up. The same conflation
    sat one line above — a missing clone is a question nobody can ask, and it
    was reported as the work being unplaceable.
    """
    ev = json.loads(row["evidence_json"] or "[]")
    repo = next((e for e in ev if str(e.get("uri", "")).startswith("repo:repository:")), None)
    if not repo or not repo.get("head"):
        # A CONTRADICTION, not an unaskable: the row was written to be
        # verifiable and does not carry what it needs. Nothing about the estate
        # is unmeasurable here — the record itself is short.
        return False, "the row records no repository head to verify"
    nwo = repo["uri"].split("repo:repository:", 1)[1]
    path = clone_path(nwo)
    if path is None or not path.is_dir():
        return None, (f"{nwo} has no local clone here, so nothing could look: this "
                      f"is not evidence that the work is gone")
    sha, branch = repo["head"], repo.get("branch") or "HEAD"
    if git(path, "cat-file", "-e", f"{sha}^{{commit}}")[0] != 0:
        return False, (f"{sha} is no longer an object in {nwo} — the work was "
                       f"rewritten or the clone was rebuilt")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # THE BRANCH IN ANY OF ITS FORMS. `feat/attention-rail` and
    # `origin/feat/attention-rail` are the same branch — the second is what a
    # clone keeps after the local copy is deleted, which is what pushing and
    # tidying up leaves behind. Resolving only the bare name is what turned four
    # records of pushed work into "the branch moved away from it".
    resolved = None
    for cand in branch_forms(path, branch):
        if git(path, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}")[0] == 0:
            resolved = cand
            break

    if resolved:
        if git(path, "merge-base", "--is-ancestor", sha, resolved)[0] == 0:
            where = (f"{resolved}" if resolved == branch
                     else f"{resolved} — the local {branch} is gone, which is what "
                          f"pushing and tidying up leaves behind")
            return True, (f"git says {sha} is still an ancestor of {where} in "
                          f"{nwo}, checked {now}")
        # A WITNESS LOOKED AT WHAT THE ROW CLAIMED AND DISAGREED. That stays a
        # refusal: the row records the branch the work was done on, and a branch
        # that exists and no longer holds the commit is the fact the original
        # check was written for. Where another ref reaches it, the message says
        # so — the person deciding needs that, and it is not the same claim.
        held = [h for h in refs_containing(path, sha) if h != resolved]
        return False, (f"{sha} exists in {nwo} but is not an ancestor of {resolved}; "
                       f"the branch moved away from it"
                       + (f" — git still reaches it from {', '.join(held[:3])}, so the "
                          f"work survives somewhere other than where the row says"
                          if held else
                          " and no other ref reaches it either, so a garbage "
                          "collection will take it"))

    # THE NAME RESOLVES NOWHERE, local or remote. The commit may still be one
    # ref away, and if it is, the work plainly survived.
    held = refs_containing(path, sha)
    if held:
        return True, (f"no branch named {branch} exists in {nwo} any more, but git "
                      f"reaches {sha} from {', '.join(held[:3])}, so the work "
                      f"survives, checked {now}")
    return None, (f"no branch named {branch} exists in {nwo} and no ref reaches "
                  f"{sha}, so this clone cannot say whether the work survived "
                  f"elsewhere — that is not evidence that it did not")


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    conn = store_db.connect()
    conn.row_factory = __import__("sqlite3").Row
    # The tombstone join is what every other reader does — `ledger.live()`,
    # `survey.search`, `review.py`, `build_findings.py`, `ledger_candidates` —
    # and this query was the one that did not. An erased record would have come
    # back here on every run: refused by the ledger's own guard, counted as a
    # refusal, and reported as a finding for ever. Excluded here instead, so an
    # erasure means the same thing on every read path.
    rows = conn.execute(
        "SELECT l.* FROM ledger l JOIN (SELECT memory_id, MAX(revision) rev FROM ledger "
        "GROUP BY memory_id) m ON l.memory_id = m.memory_id AND l.revision = m.rev "
        "LEFT JOIN tombstones t ON t.memory_id = l.memory_id "
        "WHERE l.state = 'proposed' AND t.memory_id IS NULL").fetchall()

    # FOUR BUCKETS, because `check_session` now has three answers and one of
    # them is "nobody could look". Folding that into `refused` is what put four
    # records of pushed work on the board as unplaceable.
    promoted, refused, unaskable, needs_person = [], [], [], []
    for row in rows:
        # ONE rule, and it is positive: `session` is the only kind with evidence
        # a machine can re-check. There were two — a `NO_MECHANICAL_CHECK`
        # denylist above this line, holding `observation` — and it was
        # unreachable, since anything in it is also `!= "session"` and both
        # branches did the same thing. A denylist sitting above the real gate is
        # worse than redundant: a reader takes it for the gate and adds a new
        # kind expecting it to be refused by default, when the positive rule is
        # what actually refuses it.
        if row["kind"] != "session":
            needs_person.append((row["memory_id"], row["kind"]))
            continue
        if row["owner"] == BY:
            continue                                                           
        ok, why = check_session(row)
        if ok is None:
            unaskable.append((row["memory_id"], why, row["created_at"],
                              row["owner"], row["state"]))
            continue
        if not ok:
            refused.append((row["memory_id"], why, row["created_at"],
                            row["owner"], row["state"]))
            continue
        if dry:
            promoted.append((row["memory_id"], why))
            continue
        try:
            L.corroborate(conn, row["memory_id"], by=BY, check={"how": why},
                          expected_revision=row["revision"])
            promoted.append((row["memory_id"], why))
        except L.LedgerError as e:
            refused.append((row["memory_id"], f"{type(e).__name__}: {e}"))
    conn.close()

    # A REFUSAL IS A FINDING — this file's own docstring says so: "If the sha is
    # GONE — force-push, branch deleted, clone rebuilt — the row is NOT promoted.
    # That is a finding, not a corroboration." Until now it was a line on stdout,
    # which the tick swallows into a log: the estate recorded work at a sha git
    # can no longer place, and nothing said so anywhere a person looks. The row
    # then sits `proposed` until retention erases it at ninety days, taking the
    # only trace of the vanished work with it.
    if not dry:
        atomic.write_json(paths.SCRATCH / "corroboration.json", {
            "checked_on": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "counts": {"proposed": len(rows), "promoted": len(promoted),
                       "refused": len(refused), "unaskable": len(unaskable),
                       "needs_person": len(needs_person)},
            "refused": [{"memory_id": mid, "why": why, "created_at": at,
                         "owner": owner, "state": state}
                        for mid, why, at, owner, state in refused],
            # LISTED, not counted. A count with no list is a number an operator
            # cannot act on, and this is the bucket whose rows retention will
            # erase while nothing is known to be wrong with them.
            # AND WHEN IT WAS WRITTEN. The row a reader is asked to decide on
            # is erased by retention at ninety days, and the receipt named it
            # without saying how long it had — sending every reader back to the
            # store for the one number the decision turns on.
            "unaskable": [{"memory_id": mid, "why": why, "created_at": at,
                           "owner": owner, "state": state}
                          for mid, why, at, owner, state in unaskable],
        })

    tag = "[dry-run] " if dry else ""
    print(f"{tag}corroborate: {len(rows)} proposed — "
          f"{len(promoted)} promoted, {len(refused)} refused, "
          f"{len(unaskable)} unaskable, {len(needs_person)} need a person")
    for mid, why, *_ in unaskable[:6]:
        print(f"  unaskable {mid}: {why}")
    for mid, why in promoted[:6]:
        print(f"  + {mid}  {why[:96]}")
    for mid, why, *_ in refused[:6]:
        print(f"  - {mid}  {why[:96]}")
    if needs_person:
        kinds = sorted({k for _, k in needs_person})
        print(f"  ? {len(needs_person)} row(s) of kind {kinds} carry no evidence a "
              f"machine can re-check; they wait for review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
