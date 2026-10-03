#!/usr/bin/env python3
"""Whether a project has ever shipped anything, and how long ago.

The estate answers what a project IS, how big it is, whose code it depends on,
and when work last happened. **Nothing said the work resulted in anything.**
`last_activity_on` is a commit or a session; a portfolio of about 120 projects
needs the other question — did this go anywhere — and for that a tag is the
cheapest honest signal a checkout can give.

MEASURED BEFORE THIS WAS WRITTEN, because a metric that is a field of zeros is
machinery without a question. On one installation roughly a third of the
checkouts held tags and the rest held none, with a few carrying no measurement
at all — the tagged and untagged counts need not sum to the total, and the gap
is the honest part, because a checkout the plugin could not read is neither
tagged nor untagged. The tagged ones were mostly packages versioned
continuously, several of them tagged within the same week. A split like that is
a signal; a handful out of a hundred would not have been.

TWO METRICS, AND THE SECOND ONE'S ABSENCE CARRIES INFORMATION.

    release.tags             how many tags the checkout holds
    release.days_since_last  days since the NEWEST tag — omitted entirely where
                             there are none, because 0 would mean "released
                             today", the opposite of never (the same rule that
                             omits a value rather than zeroing it elsewhere)

WHY `for-each-ref` AND NOT `git log --tags`. An annotated tag carries its own
date, and that is the release's date: the commit it points at may be far older —
a release cut from a stabilised branch is exactly that shape. `git log
--no-walk --tags` reads the COMMIT's date and would report the work rather than
the release. The lightweight case falls back to the commit date, which is the
only date such a tag has.

A tag is not a release everywhere: some repositories tag nightlies, some tag
nothing and publish from a branch. That is why the metric is named for what it
MEASURES — tags — rather than for what it is used to infer.
"""
from __future__ import annotations
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths                                                                    
import safe_git                                                                 

#: The instant every row of one run shares — the calendar day in UTC, matching
#: the manifest's 24-hour cadence. A daily plugin stamping `now()` would write a
#: different `at` on every run and defeat the runner's own idempotency key
#: `(project_id, metric, at)`.
AT = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")


def git(cwd: pathlib.Path, *args: str) -> tuple[str, str | None]:
    """`(stdout, reason)` — the three-outcome shape every collector here uses.

    A checkout that cannot be read is NOT a checkout with no tags, and reporting
    the second is how a broken clone becomes "never released".
    """
    try:
        p = safe_git.run(args, repo=cwd, timeout=30)
    except FileNotFoundError:
        return "", "git is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return "", "git did not answer within 30s"
    except OSError as exc:
        return "", f"git could not be run: {type(exc).__name__}: {exc}"
    if p.returncode != 0:
        return "", (f"git exited {p.returncode}: "
                    f"{(p.stderr or '').strip()[:120] or 'no message'}")
    return p.stdout, None


def checkouts() -> list[tuple[str, pathlib.Path]]:
    """`(project_id, path)` for every project with a git checkout here.

    Read from the registry, which is the only place that knows which repository
    belongs to which project — and through `implemented_by`, because a
    repository's own row carries no project.
    """
    try:
        projects = json.loads((paths.REGISTRY / "projects.json")
                              .read_text(encoding="utf-8"))["projects"]
        repos = {r["id"]: r for r in
                 json.loads((paths.REGISTRY / "repositories.json")
                            .read_text(encoding="utf-8"))["repositories"]}
        relations = json.loads((paths.REGISTRY / "relations.json")
                               .read_text(encoding="utf-8"))["relations"]
    except (OSError, ValueError, KeyError) as exc:
        # The runner treats a non-zero exit as a plugin failure and reports it,
        # which is the right destination: an unreadable registry is a fault of
        # the run rather than a measurement of the estate.
        print(f"release_cadence: the registry could not be read: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)

    known = {p["id"] for p in projects}
    out: list[tuple[str, pathlib.Path]] = []
    seen: set[str] = set()
    for rel in relations:
        if rel.get("type") != "implemented_by":
            continue
        pid, rid = rel.get("from"), rel.get("to")
        if pid not in known or pid in seen:
            continue
        local = (repos.get(rid) or {}).get("local") or {}
        path = local.get("path")
        if not path or not (pathlib.Path(path) / ".git").exists():
            continue
        # ONE checkout per project, the first by relation order. A project with
        # several repositories would otherwise get one row per repository under
        # the same `(project_id, metric, at)` key, and the last write would win
        # silently — a number that depends on iteration order.
        seen.add(pid)
        out.append((pid, pathlib.Path(path)))
    return out


def tags_of(repo: pathlib.Path) -> tuple[int, str | None, str | None]:
    """`(count, newest tag date or None, reason it could not be read)`."""
    # `creatordate` is the tag's own date for an annotated tag and the commit's
    # for a lightweight one — which is the only date the latter has.
    out, why = git(repo, "for-each-ref", "--sort=-creatordate",
                   "--format=%(creatordate:short)", "refs/tags")
    if why is not None:
        return 0, None, why
    dates = [l.strip() for l in out.splitlines() if l.strip()]
    return len(dates), (dates[0] if dates else None), None


def main() -> int:
    today = datetime.now(timezone.utc).date()
    unreadable = 0
    for pid, repo in checkouts():
        count, newest, why = tags_of(repo)
        if why is not None:
            # NAMED on stderr and counted. The runner collects stderr into its
            # report, so a checkout git could not answer about is visible
            # without becoming a zero in the metric.
            print(f"  {pid}: tags could not be read at {repo} — {why}",
                  file=sys.stderr)
            unreadable += 1
            continue
        print(json.dumps({"project_id": pid, "metric": "release.tags",
                          "at": AT, "value": float(count)}, ensure_ascii=False))
        if newest is None:
            # NO ROW, deliberately. See the module docstring: zero days would
            # read as "released today".
            continue
        try:
            days = (today - datetime.strptime(newest, "%Y-%m-%d").date()).days
        except ValueError:
            print(f"  {pid}: git gave an unparseable tag date {newest!r}",
                  file=sys.stderr)
            continue
        print(json.dumps({"project_id": pid,
                          "metric": "release.days_since_last",
                          "at": AT, "value": float(max(days, 0))},
                         ensure_ascii=False))
    if unreadable:
        print(f"release_cadence: {unreadable} checkout(s) could not be asked",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
