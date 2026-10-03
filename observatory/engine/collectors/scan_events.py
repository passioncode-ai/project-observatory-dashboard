#!/usr/bin/env python3
"""Read commits out of every local checkout into the event store.

Deterministic and idempotent: an event is keyed by (kind, ref), so running again
over the same history inserts nothing new. That property is trap T5's shape and
is asserted by tests/test_traps.py.
"""
from __future__ import annotations
import json, sqlite3, subprocess, sys, pathlib
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import estate
import paths
import safe_git
from collectors import registry_read
from store import db as store_db, migrate

COLLECTOR_VERSION = "scan_events/2"
# A SAFETY VALVE, not the bound. The bound is the retention window, which is
# what makes the collector and the pruner agree (trap T25). At 200 this was the
# real limit and it was silent: 30,674 of the estate's 39,702 commits inside the
# window were never recorded, and no line anywhere said so.
DEFAULT_DEPTH = 10000


def retention_horizon_days() -> int | None:
    """The same horizon retention prunes by, read from the same file.

    Without this the two fight: the collector inserted every commit within its
    depth regardless of age and retention deleted everything past a year, so a
    live tick reported a large `deleted` count and the next one inserted the
    same events again, run after run.
    """
    # THE WORKSPACE'S CONFIG, which is where store/retention.py reads it
    # (`paths.config_file`). This read `paths.STORE / "retention.json"`, a file
    # that exists in no workspace, so the collector had no window and every tick
    # inserted about 29,000 events that retention deleted again (PB-136).
    try:
        return int(json.loads(
            paths.config_file("retention.json").read_text(encoding="utf-8"))["events_days"])
    except Exception:
        return None


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(repo: pathlib.Path, *args: str) -> tuple[str, str | None]:
    """`(stdout, reason)`: the three-outcome shape, and here it prevents a crash.

    This returned `""` on any non-zero exit, and NEITHER a timeout nor a missing
    binary was caught: `subprocess.run(timeout=60)` raises `TimeoutExpired` and
    an absent `git` raises `FileNotFoundError`, so ONE slow or unreadable
    checkout aborted the whole collector: every repository after it unscanned,
    the `scans` row left with no `finished_at`, and the only record a log nobody
    reads on a schedule. With well over a hundred repositories on a volume that
    can fill up, this is not hypothetical.

    And the reason matters as much as the crash. "No commits in the window" is a
    fact about the SUBJECT; "git could not run" is a fact about the RUN. Both
    arrived as an empty string and were reported as "git log returned nothing",
    which reads as the first. A fault must not be silent, and here it no longer is.

    Through `safe_git`: `git log` under the operator's configuration ran
    gpg.program on a signed commit whenever `log.showSignature` was set.
    """
    try:
        proc = safe_git.run(args, repo=repo, timeout=60)
    except FileNotFoundError:
        return "", "git is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return "", "git did not answer within 60s"
    except OSError as exc:
        return "", f"git could not be run: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return "", (f"git exited {proc.returncode}: "
                    f"{(proc.stderr or '').strip()[:160] or 'no message'}")
    return proc.stdout, None


def finish(conn, scan_id: str, *, degraded: list[dict], **counts) -> None:
    """Close the scan row. One writer, called from the success path AND from the
    `finally` below.

    A scan with no `finished_at` is a broken segment of the spine every delta
    hangs from (`deltas.from_scan` and `to_scan` are foreign keys into it), and
    `scans` is the one table retention never prunes, so it stays broken. Before
    `git()` learned to catch a timeout, one unreadable checkout produced exactly
    that row and nothing else.
    """
    try:
        conn.execute("UPDATE scans SET finished_at = ?, counts_json = ?,"
                     " degraded_json = ? WHERE id = ?",
                     (now(), json.dumps(counts),
                      json.dumps(degraded, ensure_ascii=False), scan_id))
        conn.commit()
    except sqlite3.Error as exc:
        print(f"the scan row could not be closed: {exc}", file=sys.stderr)


def targets(projects: list[dict], repos: dict[str, dict],
            owner_of: dict[str, str]) -> list[dict]:
    """Every checkout with history to read: THE SUBJECT OF THIS LOOP.

    It used to iterate `repositories.json`, and that was the defect rather than a
    shorthand for it: a git folder with no remote has no `owner/name`, so no
    repository row, so its history was never read at all, in a system whose
    central question is where work happened. Such folders can hold active,
    recent work.

    Nothing downstream had to change to hold them, which is what says the subject
    was wrong rather than the schema: `events.repo_id` is nullable and already
    NULL for many rows, `store/rollup.py` groups by `project_id` and never
    mentions `repo_id`, and neither does retention or the MCP server.

    A target carries its own keys so the loop never has to ask which kind it is:
    `repo_id` is None for an unpublished folder, and that None is what reaches the
    insert rather than an invented id.
    """
    out: list[dict] = []
    for rid, repo in repos.items():
        local = repo.get("local")
        if not local or not local.get("path"):
            # A repository in the registry with no checkout here. Skipped
            # silently and always was: there is no history on this disk to read,
            # which is a fact about the listing rather than a fault.
            continue
        out.append({"label": rid, "project_id": owner_of.get(rid), "repo_id": rid,
                    "path": local["path"], "created_on": repo.get("created_on") or "",
                    "name": repo.get("name_with_owner") or rid.split(":", 1)[1]})
        # EVERY CHECKOUT, not only the primary one. A second clone or a worktree
        # on another branch holds commits the primary does not; reading only
        # `local.path` recorded none of them. The label stays the repository id,
        # so a sha seen in two checkouts of ONE repository is not reported as
        # history shared between repositories, and INSERT OR IGNORE keeps one row.
        for extra in local.get("extra_checkouts") or []:
            if extra.get("path") and extra["path"] != local["path"]:
                out.append({"label": rid, "project_id": owner_of.get(rid), "repo_id": rid,
                            "path": extra["path"], "created_on": repo.get("created_on") or "",
                            "name": repo.get("name_with_owner") or rid.split(":", 1)[1],
                            "checkout": extra.get("folder")})
    for p in projects:
        lo = p.get("local_only") or {}
        # `unpublished` is `is_git and no remote` (collectors/merge.py). A git
        # folder whose remote is on a host the merge cannot key (a self-hosted
        # forge, a bare repository on another disk) is a `local-only` project
        # too, with `unpublished: false` — and admitting only `unpublished`
        # dropped its history silently: the panel said "no commits in the
        # window" for a checkout committed to that day. `commits` is what the
        # filesystem scan measured, so a folder with history is a target
        # whatever its remote; one that is not a git repository measured none.
        remote_elsewhere = not lo.get("unpublished") and int(lo.get("commits") or 0) > 0
        if not (lo.get("unpublished") or remote_elsewhere) or not lo.get("path"):
            continue
        out.append({"label": p["id"], "project_id": p["id"], "repo_id": None,
                    "path": lo["path"], "created_on": "",
                    "name": p.get("name") or lo.get("folder") or p["id"],
                    "remote_elsewhere": remote_elsewhere})
    return out


def by_age(t: dict) -> tuple[str, str]:
    """Oldest checkout first; one with no creation date last.

    A commit is recorded once: it is one piece of work, and giving the inherited
    copy its own row would credit the younger repository with the elder's
    history. Which target gets it therefore matters, and it used to be decided
    by `sorted()`: alphabetical luck.

    An unpublished folder has no creation date and sorts last, which is the
    direction the missing-date default already pointed and the right one on its
    own merits: where a published repository and an unpublished folder hold the
    same commit, the published one is the better home for it.
    """
    return (t.get("created_on") or "9999-12-31", t["label"])


def main() -> int:
    depth = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DEPTH
    horizon = retention_horizon_days()
    # NO SCAN ROW YET, deliberately. A refusal here has recorded nothing, and a
    # `scans` row that exists because the registry was unreadable is a segment of
    # the spine with no measurement behind it.
    try:
        projects = registry_read.read("projects.json", "projects")
        repos = {r["id"]: r for r in
                 registry_read.read("repositories.json", "repositories")}
        relations = registry_read.read("relations.json", "relations")
    except registry_read.RegistryUnreadable as exc:
        print(f"NOT scanned — {exc}", file=sys.stderr)
        print("Recording no commits would freeze every activity date and drift the "
              "whole estate toward `inactive`, which is what the dashboard shows.",
              file=sys.stderr)
        return 1
    owner_of = {}
    for rel in relations:
        if rel["type"] == "implemented_by":
            owner_of[rel["to"]] = rel["from"]

    conn = store_db.connect()
    scan_id = store_db.scan_id("events", now())
    conn.execute("INSERT INTO scans (id, started_at, collector_version) VALUES (?,?,?)",
                 (scan_id, now(), COLLECTOR_VERSION))
    conn.commit()

    ownership_of = {p["id"]: p.get("ownership") for p in projects}
    inserted = skipped = checkouts = 0
    excluded = truncated = unreadable = 0
    #: A repository git answered about, with nothing in the window. An ANSWER, so
    #: it belongs beside the counts and not in `degraded` — see `git()`.
    quiet: list[str] = []
    #: reason -> the repositories it applied to. A policy exclusion is a rule,
    #: not an incident, so it is summarised by rule rather than listed 118 times.
    excluded_why: dict[str, list[str]] = {}
    #: FAULTS ONLY. Policy exclusions used to land here too, so `degraded
    #: sources: 118` was mostly the estate's own rule about other people's
    #: repositories and a broken checkout hid inside it. They are counted
    #: separately now and reported as `excluded`, which is what they are.
    degraded: list[dict] = []
    #: sha -> the repository this scan attributed it to. Two repositories can
    #: hold the same commit without either being a fork: creating a repository
    #: from a copy of another's history does it, and GitHub records no `fork`
    #: flag for that. It happens in practice, between repositories created weeks
    #: apart.
    owner_of_sha: dict[str, str] = {}
    shared: list[tuple[str, str, int]] = []
    # ORDERED BY AGE, not by name. A commit is recorded once — it is one piece of
    # work, and giving the inherited copy its own row would credit the younger
    # repository with the elder's history, which is the mistake `estate.py`
    # exists to prevent one level up. Which repository gets it therefore matters,
    # and until now it was decided by `sorted()`: alphabetical luck. The
    # repository that existed FIRST owns shared ancestry, which is a rule rather
    # than an accident — and on the live pair it happens to agree with the
    # alphabet, so the change is visible only in the reporting, which is where a
    # silent ambiguity should have been all along.
    unpublished = remote_elsewhere = 0
    # THE LOOP IS WRAPPED, because the scan row already exists. Anything raising
    # inside it — and it walks 172 checkouts on a volume that has been at 100% —
    # would otherwise leave `finished_at` NULL for ever: `scans` is the table
    # retention never prunes, and every delta hangs off it by foreign key.
    try:
      for t in sorted(targets(projects, repos, owner_of), key=by_age):
          rid = t["label"]
          # One rule, two readers: the companion plugin's recorder has always
          # refused to write about somebody else's history, and this collector did
          # not. Thirty per cent of the commits in the window came from seven
          # `external` projects, which would have made a stranger's tool the most
          # active thing in the estate.
          own = ownership_of.get(t["project_id"])
          if not estate.records_events(own):
              excluded += 1
              excluded_why.setdefault(estate.why_excluded(own), []).append(rid)
              continue
          path = pathlib.Path(t["path"])
          if not (path / ".git").exists():
              degraded.append({"source": rid, "reason": f"no .git at {path}"})
              continue
          checkouts += 1
          if t.get("remote_elsewhere"):
              # Its own count, for the same reason as `unpublished` below: these
              # are published somewhere, just not anywhere this estate inventories.
              remote_elsewhere += 1
          elif t["repo_id"] is None:
              # COUNTED, not folded into `checkouts`. A new source absorbed into
              # an old total reads as "nothing changed", and the number an
              # operator checks after this change is exactly how many checkouts
              # had no repository to be keyed by.
              unpublished += 1
          # depth+1 makes truncation EXACTLY detectable: asking for one more than
          # the cap and receiving it proves history was cut. `-200` returning 200
          # is ambiguous — a repository with exactly 200 commits looks identical to
          # one with twenty thousand, which is how 30,674 commits went missing
          # without a single line saying so.
          args = ["log", f"-{depth + 1}", "--no-merges",
                  "--format=%H%x1f%an%x1f%cI%x1f%s%x1e"]
          if horizon:
              # Ask git for the window retention keeps, rather than inserting rows
              # the next prune will delete.
              args.insert(1, f"--since={horizon} days ago")
          log, reason = git(path, *args)
          if reason is not None:
              # A fact about the run. It is NOT "this repository has no commits",
              # and conflating the two is how an unreadable checkout reads as an
              # idle project — which then reads as a project to archive.
              degraded.append({"source": rid, "reason": reason})
              unreadable += 1
              continue
          if not log.strip():
              # A fact about the subject, and an ANSWER: git ran, and there is
              # nothing in the window. Recorded as `quiet` rather than degraded.
              quiet.append(rid)
              continue
          records = [r for r in log.split("\x1e") if r.strip()]
          if len(records) > depth:
              truncated += 1
              degraded.append({"source": rid, "reason":
                               f"history truncated at the {depth}-commit safety valve; "
                               f"older commits inside the retention window were NOT recorded"})
              records = records[:depth]
          for record in records:
              parts = record.strip().split("\x1f")
              if len(parts) != 4:
                  continue
              sha, author, iso, subject = parts
              # git's %cI carries the COMMITTER'S local offset. Stored verbatim it
              # made three spellings of one instant share a TEXT column that every
              # ordering, window and retention cutoff compares lexicographically —
              # `+` sorts before `-` sorts before `Z`, none of which is time. One
              # instant, one spelling, decided at the boundary where it enters.
              iso = migrate.to_utc_z(iso) or iso
              first = owner_of_sha.setdefault(sha, rid)
              if first != rid:
                  # Recorded, not silently dropped. `INSERT OR IGNORE` below will
                  # skip it and say nothing; without this line the younger
                  # repository's history simply appears shorter than it is, with
                  # no way to tell that from a repository that did less work.
                  if not shared or shared[-1][:2] != (first, rid):
                      shared.append((first, rid, 0))
                  shared[-1] = (first, rid, shared[-1][2] + 1)
              cur = conn.execute(
                  "INSERT OR IGNORE INTO events (id, project_id, repo_id, kind, ref, actor,"
                  " occurred_at, payload_json) VALUES (?,?,?,?,?,?,?,?)",
                  (f"commit:{sha}", t["project_id"], t["repo_id"], "commit", sha, author, iso,
                   json.dumps({"subject": subject[:300], "repo": t["name"]},
                              ensure_ascii=False)))
              inserted += cur.rowcount
              skipped += 1 - cur.rowcount
    except Exception as exc:
        finish(conn, scan_id, checkouts=checkouts, events_inserted=inserted,
               events_already_present=skipped, repos_excluded=excluded,
               repos_truncated=truncated, repos_unreadable=unreadable,
               repos_quiet=len(quiet), checkouts_unpublished=unpublished,
               checkouts_remote_not_inventoried=remote_elsewhere,
               aborted=True,
               commits_shared=sum(n for _, _, n in shared),
               degraded=degraded + [{"source": "the scan itself",
                                     "reason": f"aborted: {type(exc).__name__}: {exc}"}])
        conn.close()
        print(f"SCAN ABORTED after {checkouts} checkout(s): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"  {inserted} event(s) inserted before it stopped are kept — they were "
              f"measured. The scan row records the abort rather than looking unfinished.",
              file=sys.stderr)
        return 1
    finish(conn, scan_id, checkouts=checkouts, events_inserted=inserted,
           events_already_present=skipped, repos_excluded=excluded,
           repos_truncated=truncated, repos_unreadable=unreadable,
           repos_quiet=len(quiet), checkouts_unpublished=unpublished,
           checkouts_remote_not_inventoried=remote_elsewhere,
           commits_shared=sum(n for _, _, n in shared), degraded=degraded)
    total = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    conn.close()
    print(f"scan {scan_id}")
    # NAMED ON STDOUT TOO. The tick swallows this into a log, but the log is
    # where an operator looks when a number moves, and "checkouts 174" hides
    # whether the new source was reached at all.
    print(f"  of them {unpublished} unpublished checkout(s) with no repository id"
          if unpublished else "  no unpublished checkout had history to read")
    if remote_elsewhere:
        print(f"  and {remote_elsewhere} checkout(s) whose remote is on a host not inventoried here")
    print(f"  checkouts {checkouts} | inserted {inserted} | already present {skipped} "
          f"| total {total}" + (f" | window {horizon}d" if horizon else " | NO window"))
    # BY RULE, not one line per repository. 118 policy exclusions in `degraded`
    # made `degraded sources` a number about the estate's own policy, and a
    # broken checkout was one line inside it.
    print(f"  excluded {excluded} repo(s) as not this estate's own work:")
    for why, rids in sorted(excluded_why.items(), key=lambda kv: -len(kv[1])):
        print(f"    {len(rids):5}  {why}")
    print(f"  quiet {len(quiet)} repo(s): git answered, nothing in the window — "
          f"an answer, not a degradation")
    print(f"  UNREADABLE {unreadable} repo(s): git could not answer at all"
          if unreadable else "  every checkout git was asked about answered")
    # Truncation used to be the one degradation that said nothing: `git log -200`
    # returning 200 rows looks exactly like a repository with 200 commits.
    print(f"  TRUNCATED {truncated} repo(s) at the {depth}-commit safety valve — "
          f"raise it or narrow the window" if truncated
          else f"  no repository hit the {depth}-commit safety valve")
    if shared:
        total = sum(n for _, _, n in shared)
        print(f"  SHARED ANCESTRY: {total} commit(s) appear in more than one repository and "
              f"are recorded once, under the older address:")
        for first, second, n in shared:
            print(f"    {n:5}  {first.split(':',1)[1]}  also in  {second.split(':',1)[1]}")
    print(f"  degraded sources: {len(degraded)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
