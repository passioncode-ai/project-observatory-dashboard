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

* **`lesson` rows can, by a second PROJECT.** A lesson (`observatory_lesson`) is a
  class one project learned. When a second, DISTINCT project records the same
  `classKey`, that is an independent witness to the class — and each lesson's evidence
  commit is asked of git in its own project's clone, found by the project's
  pseudonymous key (the first 12 hex digits of the clone's root commit). The rule is the
  operator's (O2): two distinct projects record the class AND BOTH COMMITS RESOLVE, and
  then no person is needed; `supported` stays the operator's. "Resolve" means here, in
  git: the project's key must belong to a clone the registry knows on this machine, and
  the commit must be an object in it. A key no registered clone carries cannot be asked
  — the lesson stays `proposed`, listed as "commit not resolvable here", and witnesses
  nothing, so one caller inventing two `p-` keys buys nothing. A commit git cannot find
  in a clone that IS here contradicts the lesson. The same project twice is one witness:
  a repeat revises that project's record and never counts again.

    corroborate.py [--dry-run]
"""
from __future__ import annotations
import json, pathlib, subprocess, sys, time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths                                                                      
import safe_git                                                                   
from store import db as store_db                                                  
from store import ledger as L                                                     
import atomic                                                                      

BY = "agent:corroborator"
#: The witness for lessons: a second identity, so a lesson an `agent:corroborator` wrote
#: (none does today) could still be corroborated, and the provenance says which rule ran.
LESSON_BY = "agent:lesson-corroborator"
#: Distinct projects that must hold one classKey before its lessons are promoted.
LESSON_PROJECTS = 2


def repository_paths() -> list[pathlib.Path]:
    """Every clone the registry knows on this machine."""
    reg = paths.REGISTRY / "repositories.json"
    if not reg.is_file():
        return []
    try:
        doc = json.loads(reg.read_text(encoding="utf-8"))
        repos = doc.get("repositories", []) if isinstance(doc, dict) else []
        if not isinstance(repos, list):
            return []
    except (OSError, ValueError):
        return []
    out = []
    for r in repos:
        local = r.get("local") if isinstance(r, dict) else None
        p = local.get("path") if isinstance(local, dict) else None
        if isinstance(p, str) and p and pathlib.Path(p).is_dir():
            out.append(pathlib.Path(p))
    return out


def project_keys(*, deadline: float | None = None) -> dict[str, list[pathlib.Path]]:
    """Pseudonymous project key -> the clones here that carry it.

    The key is `p-` and the first 12 hex digits of the repository's root commit (the
    lowest, when a history has several roots) — the same derivation as task-pipeline's
    `lessons.py project_key`, so a lesson's origin finds its clone without the store ever
    holding the project's name."""
    keys: dict[str, list[pathlib.Path]] = {}
    for path in repository_paths():
        if deadline is not None and time.monotonic() >= deadline:
            break
        code, out = git(path, "rev-list", "--max-parents=0", "HEAD", deadline=deadline)
        if code != 0 or not out.split():
            continue
        keys.setdefault("p-" + sorted(out.split())[0][:12], []).append(path)
    return keys


def resolve_origin(origin: dict, keys: dict[str, list[pathlib.Path]], *,
                   deadline: float | None = None) -> tuple[bool | None, str]:
    """Does this lesson's evidence commit resolve? Three answers, as for sessions.

    True: a clone of the project is here and git finds the commit. False: a clone is here
    and git does not — the lesson cites work this project does not hold. None: no clone
    the registry knows carries this key, so nothing could look; the lesson can neither be
    promoted nor witness for another (O2: both commits must resolve)."""
    project, commit = origin.get("projectId"), str(origin.get("commit") or "")
    sha = commit.split("commit:", 1)[-1]
    if not (isinstance(project, str) and project.startswith("p-")) or not sha \
            or not all(c in "0123456789abcdef" for c in sha) or not 7 <= len(sha) <= 40:
        return False, "the origin carries no pseudonymous project key and commit form"
    clones = keys.get(project, [])
    if not clones:
        return None, (f"commit not resolvable here: no clone in the registry carries "
                      f"{project}, so commit:{sha} cannot be asked of git")
    unavailable = False
    for path in clones:
        code = git(path, "cat-file", "-e", f"{sha}^{{commit}}", deadline=deadline)[0]
        if code == 0:
            return True, f"commit:{sha} resolves in {project}'s clone"
        unavailable = unavailable or code == 127
    if unavailable:
        return None, "commit verification unavailable or its time budget expired"
    return False, f"commit:{sha} is not an object in any clone of {project} here"


def corroborate_lessons(conn, dry: bool) -> dict:
    """Promote `proposed` lessons whose class a second, distinct project also holds.

    A lesson is promoted when its own commit resolves in its project's clone here and at
    least one OTHER project's lesson of the same classKey — proposed, observed or
    supported — resolves in its clone too. The witness is named in the check, with both
    resolutions; the ledger's own guards still apply (`L.corroborate`: never the owner,
    never the operator, only from `proposed`). Four outcomes: promoted, waiting (resolved,
    no second resolved project yet), unresolvable (no registered clone carries the key),
    contradicted (the clone does not hold the commit)."""
    from store import lessons as LS
    rows = LS.current_lessons(conn)
    # The clones are asked only when some class could be promoted: a proposed lesson whose
    # class names two or more projects. Otherwise no root commit needs reading, and a tick
    # over an estate of many clones spends nothing on lessons that wait.
    claimed: dict[str, set] = {}
    for r in rows:
        claimed.setdefault(r["body"].get("classKey"), set()).add(
            (r["body"].get("origin") or {}).get("projectId"))
    askable = any(len(claimed.get(r["body"].get("classKey"), ())) >= LESSON_PROJECTS
                  for r in rows if r["state"] in ("proposed", "observed"))
    keys = project_keys() if askable else {}
    resolution = {r["memory_id"]: resolve_origin((r["body"].get("origin") or {}), keys)
                  for r in rows}
    by_class: dict[str, list[dict]] = {}
    for r in rows:
        ck = r["body"].get("classKey")
        if isinstance(ck, str) and ck:
            by_class.setdefault(ck, []).append(r)
    promoted, waiting, unresolvable, contradicted, invalidated = [], [], [], [], []
    for ck, members in sorted(by_class.items()):
        # Only a RESOLVED origin witnesses: a key no registered clone carries is not a
        # project this machine can vouch for.
        standing = [m for m in members if resolution[m["memory_id"]][0] is True]
        projects = {m["body"]["origin"]["projectId"] for m in standing}
        for m in members:
            if m["state"] == "observed" and m["owner"] != L.OPERATOR:
                if resolution[m["memory_id"]][0] is not True or len(projects) < LESSON_PROJECTS:
                    if dry:
                        invalidated.append({"memory_id": m["memory_id"], "dry_run": True})
                    else:
                        try:
                            out = LS.revalidate(conn, L, memory_id=m["memory_id"],
                                                expected_revision=m["revision"])
                            if out["outcome"] == "replaced":
                                invalidated.append({"memory_id": m["memory_id"], **out})
                        except L.LedgerError as exc:
                            waiting.append({"memory_id": m["memory_id"], "classKey": ck,
                                            "reason": type(exc).__name__})
                continue
            if m["state"] != "proposed":
                continue
            if len(claimed.get(ck, ())) < LESSON_PROJECTS:
                # Nothing to ask git yet: no other project has recorded the class.
                waiting.append({"memory_id": m["memory_id"], "classKey": ck,
                                "projects": len(claimed.get(ck, ()))})
                continue
            ok, why = resolution[m["memory_id"]]
            if ok is False:
                contradicted.append({"memory_id": m["memory_id"], "classKey": ck,
                                     "why": why, "created_at": m["created_at"]})
                continue
            if ok is None:
                unresolvable.append({"memory_id": m["memory_id"], "classKey": ck,
                                     "why": why, "created_at": m["created_at"]})
                continue
            mine = m["body"]["origin"]["projectId"]
            witnesses = [w for w in standing if w["body"]["origin"]["projectId"] != mine]
            if len(projects) < LESSON_PROJECTS or not witnesses:
                waiting.append({"memory_id": m["memory_id"], "classKey": ck,
                                "projects": len(projects)})
                continue
            w = witnesses[0]
            how = (f"lesson {ck} is held by {len(projects)} distinct projects; this one "
                   f"({mine}): {why}; witness {w['memory_id']}@{w['revision']} "
                   f"({w['body']['origin']['projectId']}): {resolution[w['memory_id']][1]}")
            if dry:
                promoted.append({"memory_id": m["memory_id"], "classKey": ck, "how": how})
                continue
            try:
                L.corroborate(conn, m["memory_id"], by=LESSON_BY,
                              check={"how": how, "rule": "lesson-two-projects",
                                     "witness": f"{w['memory_id']}@{w['revision']}"},
                              expected_revision=m["revision"])
                promoted.append({"memory_id": m["memory_id"], "classKey": ck, "how": how})
            except L.LedgerError as exc:
                contradicted.append({"memory_id": m["memory_id"], "classKey": ck,
                                     "why": f"{type(exc).__name__}: {exc}",
                                     "created_at": m["created_at"]})
    return {"classes": len(by_class), "promoted": promoted, "waiting": waiting,
            "unresolvable": unresolvable, "contradicted": contradicted, "invalidated": invalidated}


def clone_path(nwo: str) -> pathlib.Path | None:
    reg = paths.REGISTRY / "repositories.json"
    if not reg.is_file():
        return None
    for r in json.loads(reg.read_text(encoding="utf-8"))["repositories"]:
        if r["id"] == f"repository:{nwo}" and (r.get("local") or {}).get("path"):
            return pathlib.Path(r["local"]["path"])
    return None


def git(path: pathlib.Path, *args: str, deadline: float | None = None) -> tuple[int, str]:
    try:
        timeout = 20 if deadline is None else min(20, deadline - time.monotonic())
        if timeout <= 0:
            return 127, ""
        r = safe_git.run(args, repo=path, timeout=timeout)
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


def repository_of(row) -> str | None:
    """OWNER/NAME of the repository a session row's evidence names, if any."""
    try:
        ev = json.loads(row["evidence_json"] or "[]")
    except (ValueError, TypeError):
        return None
    repo = next((e for e in ev if isinstance(e, dict)
                 and str(e.get("uri", "")).startswith("repo:repository:")), None)
    return repo["uri"].split("repo:repository:", 1)[1] if repo else None


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
    # Lessons leave here: they have their own rule and their own counts
    # (`corroborate_lessons`), so `counts.proposed` is the rows the rules below read.
    rows = [r for r in rows if r["kind"] != "lesson"]

    # FOUR BUCKETS, because `check_session` now has three answers and one of
    # them is "nobody could look". Folding that into `refused` is what put four
    # records of pushed work on the board as unplaceable.
    promoted, refused, unaskable, needs_person = [], [], [], []
    # LESSONS HAVE THEIR OWN RULE, across projects, and their own bucket in the receipt:
    # a lesson waiting for a second project is not a refusal, and the findings builder
    # reads `refused` as work git can no longer place.
    lessons = corroborate_lessons(conn, dry)
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
                              row["owner"], row["state"], repository_of(row)))
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
                       "needs_person": len(needs_person),
                       "lessons_promoted": len(lessons["promoted"]),
                       "lessons_waiting": len(lessons["waiting"]),
                       "lessons_unresolvable": len(lessons["unresolvable"]),
                       "lessons_contradicted": len(lessons["contradicted"]),
                       "lessons_invalidated": len(lessons["invalidated"])},
            # Lessons apart from `refused`: see `corroborate_lessons`.
            "lessons": lessons,
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
            # AND WHICH REPOSITORY: five rows shared one title on the board, told
            # apart only by opening each (audit A30).
            "unaskable": [{"memory_id": mid, "why": why, "created_at": at,
                           "owner": owner, "state": state, "repository": repo}
                          for mid, why, at, owner, state, repo in unaskable],
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
    print(f"  lessons: {lessons['classes']} class(es) — {len(lessons['promoted'])} promoted, "
          f"{len(lessons['waiting'])} waiting for a second project, "
          f"{len(lessons['unresolvable'])} not resolvable here, "
          f"{len(lessons['contradicted'])} contradicted by git, "
          f"{len(lessons['invalidated'])} observations invalidated")
    for c in (lessons["unresolvable"] + lessons["contradicted"])[:6]:
        print(f"  - {c['memory_id']}  {c['why'][:96]}")
    if needs_person:
        kinds = sorted({k for _, k in needs_person})
        print(f"  ? {len(needs_person)} row(s) of kind {kinds} carry no evidence a "
              f"machine can re-check; they wait for review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
