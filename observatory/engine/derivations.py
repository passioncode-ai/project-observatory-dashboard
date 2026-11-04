#!/usr/bin/env python3
"""One repository derived from another: a copy kept in step with its source, or a move.

A team sometimes keeps two repositories of one piece of work: the original, and a copy
published somewhere else (rewritten for another owner, exported, or simply moved there).
Git cannot say so. A rewritten copy shares no commit with its source, so no merge base, no
fork flag and no remote names the relation, and the copy falls behind in silence. This
module records the relation and measures how far behind each copy is. It never writes to a
repository: carrying changes across is a person's (or a session's) reviewed work.

Three rules carry the design:

- **The operator's file is the decision.** `config/derivations.json` (schema
  `derivations/1`) is curated in the private workspace and never shipped. Each entry names
  a copy and its source by the registry's repository ids. An entry the registry cannot
  stand behind is refused BY NAME (`unknown-repository`, `self-derivation`, `cycle`,
  `move-with-sync-base`, ...) in `degraded` and in `refused`, never repaired by a guess.
- **The copy may say it too, without naming its source.** A copy can carry a tracked
  `.copy/upstream.json` (kind `rewritten`, `export` or `move`, copy_point, sync_base, deny paths;
  `rewritten` and `export` are both the curated kind `copy`). It is read from the
  copy's local clone at HEAD, so an uncommitted edit does not count. Where both sides give
  a value the curated one wins, the difference is published (`differs`), and every value
  says which side it came from (`provenance`: `curated` or `copy`).
- **Behind is measured, never fetched.** The commits on the source branch after
  `sync_base` are counted in the source's local clone (its `origin/<branch>` as of the
  last fetch, else the local branch). No clone, no branch or no such commit is `unknown`
  with the reason; nothing here starts a network call.

The registry document `registry/derivations.json` is rebuilt on every emit; the findings
(`tools/derivation_findings.py`), `project.detail` and `full copies` read it. `project.resolve`
does not: its published answer is closed, and it is deliberately left as published. Git runs
through `safe_git` with the offline profile.

ONE UNTRUSTED CLONE NEVER EMPTIES THE DOCUMENT. A copy's `.copy/upstream.json` is content from
a repository, read on every tick: its size is asked of git before the blob is read, any
failure to parse it (a deeply nested array raises RecursionError on CPython 3.11 to 3.13, not
ValueError) makes that file `unreadable`, and each relation and each unrecorded copy is built
in its own guard, so a fault in one is named in `degraded` and the others stand.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable

import credential_shape

SCHEMA = "derivations/1"
SCHEMA_VERSION = 1
KINDS = ("copy", "move")
UPSTREAM_FILE = ".copy/upstream.json"
#: A `.copy/upstream.json` larger than this is not a small record of a relation.
UPSTREAM_MAX_BYTES = 64 * 1024
#: The source of the registry document, declared by the emit.
SOURCE_ID = "SRC-0023"
#: `last_synced` is read as an alias of `sync_base` (the name the first drafts used); both is a refusal.
ALIASES = {"last_synced": "sync_base"}
#: The kinds a copy writes in its own `.copy/upstream.json`, and the curated kind each is: a copy
#: rewritten by a history filter and a copy made by export are both kept in step (`copy`).
UPSTREAM_KINDS = {"rewritten": "copy", "export": "copy", "move": "move"}
ENTRY_FIELDS = {"copy", "source", "kind", "copy_point", "sync_base", "last_synced", "builder", "source_branch",
                "note", "evidence"}
POINT_FIELDS = {"source_commit", "copy_commit", "date"}
BUILDER_FIELDS = {"path_hint", "rules_count", "rules_sha256"}
_COMMIT = re.compile(r"^[0-9a-f]{7,64}\Z")
_SHA256 = re.compile(r"^[0-9a-f]{64}\Z")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
_BRANCH = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,199}\Z")
_REPO = re.compile(r"^repository:\S{1,240}\Z")
#: The refusal codes, each with what it means. Published in COMPATIBILITY.md.
CODES = {
    "unreadable": "the file is not JSON, or not an object of schema derivations/1",
    "not-an-object": "an entry is not an object",
    "unknown-field": "an entry carries a field this schema does not define",
    "bad-repository": "copy or source is not a `repository:` id",
    "unknown-repository": "copy or source is not a repository in the registry",
    "self-derivation": "a repository is recorded as derived from itself",
    "bad-kind": "kind is neither `copy` nor `move`",
    "bad-point": "copy_point or sync_base is not a commit or {source_commit, copy_commit?, date?}",
    "missing-copy-point": "a copy without copy_point: behind cannot be measured",
    "move-with-sync-base": "a move is never synced, so it carries no sync_base",
    "bad-builder": "builder is not {path_hint, rules_count?, rules_sha256?}",
    "bad-branch": "source_branch is not a branch name",
    "bad-text": "note or evidence is not short plain text, or looks like a credential",
    "duplicate-copy": "two entries name the same copy: a copy has one source",
    "cycle": "the entries form a cycle of derivations",
}

Runner = Callable[[list[str], str], tuple[int, str]]


def _run_git(args: list[str], repo: str) -> tuple[int, str]:
    """One read-only git call in `repo`, offline: (exit code, stdout)."""
    import safe_git
    try:
        p = safe_git.run(args, repo=repo, offline=True, timeout=30)
    except (OSError, ValueError) as exc:
        return 128, type(exc).__name__
    except Exception as exc:                                            # noqa: BLE001  timeout
        return 124, type(exc).__name__
    return p.returncode, p.stdout or ""


def _shown(value: Any) -> str:
    return credential_shape.shown(value)


def _point(value: Any) -> dict | None:
    """A commit (`abc1234`) or {source_commit, copy_commit?, date?}, normalised; None when not."""
    if isinstance(value, str):
        value = {"source_commit": value}
    if not isinstance(value, dict) or set(value) - POINT_FIELDS:
        return None
    commit = value.get("source_commit")
    if not isinstance(commit, str) or not _COMMIT.match(commit.lower()):
        return None
    copy_commit = value.get("copy_commit")
    if copy_commit is not None and (not isinstance(copy_commit, str) or not _COMMIT.match(copy_commit.lower())):
        return None
    date = value.get("date")
    if date is not None and (not isinstance(date, str) or not _DATE.match(date)):
        return None
    return {"source_commit": commit.lower(),
            "copy_commit": copy_commit.lower() if isinstance(copy_commit, str) else None,
            "date": date}


def same_commit(a: str | None, b: str | None) -> bool:
    """Two spellings of one commit: one is a prefix of the other (both at least 7 digits)."""
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    return a.startswith(b) or b.startswith(a)


def _text_ok(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 300 and "\n" not in value
            and not credential_shape.shaped(value, uuids=False))


def _builder(value: Any) -> dict | None:
    if not isinstance(value, dict) or set(value) - BUILDER_FIELDS or not _text_ok(value.get("path_hint")):
        return None
    count, digest = value.get("rules_count"), value.get("rules_sha256")
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
        return None
    if digest is not None and (not isinstance(digest, str) or not _SHA256.match(digest.lower())):
        return None
    return {"path_hint": value["path_hint"], "rules_count": count,
            "rules_sha256": digest.lower() if isinstance(digest, str) else None}


def _refusal(code: str, index: int, entry: Any, why: str) -> dict:
    copy = entry.get("copy") if isinstance(entry, dict) else None
    source = entry.get("source") if isinstance(entry, dict) else None
    return {"entry": index, "code": code,
            "copy": copy if isinstance(copy, str) and _REPO.match(copy) else None,
            "source": source if isinstance(source, str) and _REPO.match(source) else None,
            "reason": f"entry {index} refused ({code}): {why}"}


def _entry(index: int, raw: Any, repos: dict[str, dict]) -> tuple[dict | None, dict | None]:
    """(the normalised entry, None) or (None, its refusal)."""
    if not isinstance(raw, dict):
        return None, _refusal("not-an-object", index, raw, "an entry must be an object")
    extra = sorted(set(raw) - ENTRY_FIELDS)
    if extra:
        return None, _refusal("unknown-field", index, raw, "fields this schema does not define: "
                              + ", ".join(_shown(x) for x in extra))
    for alias, name in ALIASES.items():
        if alias in raw:
            if name in raw:
                return None, _refusal("unknown-field", index, raw, f"`{alias}` is an alias of `{name}`; give one")
            raw = {**{k: v for k, v in raw.items() if k != alias}, name: raw[alias]}
    ids = {}
    for side in ("copy", "source"):
        value = raw.get(side)
        if not isinstance(value, str) or not _REPO.match(value):
            return None, _refusal("bad-repository", index, raw,
                                  f"`{side}` must be a `repository:<owner>/<name>` id, not {_shown(value)}")
        if value not in repos:
            return None, _refusal("unknown-repository", index, raw,
                                  f"`{side}` {_shown(value)} is not a repository in the registry")
        ids[side] = value
    if ids["copy"] == ids["source"]:
        return None, _refusal("self-derivation", index, raw, f"{ids['copy']} is recorded as derived from itself")
    kind = raw.get("kind")
    if kind not in KINDS:
        return None, _refusal("bad-kind", index, raw, f"kind must be `copy` or `move`, not {_shown(kind)}")
    points = {}
    for field in ("copy_point", "sync_base"):
        if raw.get(field) is None:
            points[field] = None
            continue
        points[field] = _point(raw[field])
        if points[field] is None:
            return None, _refusal("bad-point", index, raw,
                                  f"`{field}` must be a source commit (7 to 64 hex digits) or "
                                  "{source_commit, copy_commit?, date?}")
    if kind == "move" and points["sync_base"] is not None:
        return None, _refusal("move-with-sync-base", index, raw,
                              f"{ids['copy']} is a move: nothing is carried after a move, so it has no sync_base")
    builder = None
    if raw.get("builder") is not None:
        builder = _builder(raw["builder"])
        if builder is None:
            return None, _refusal("bad-builder", index, raw,
                                  "builder must be {path_hint, rules_count?, rules_sha256?} with a plain path "
                                  "hint, a non-negative count and a 64-digit sha256")
    branch = raw.get("source_branch")
    if branch is not None and (not isinstance(branch, str) or not _BRANCH.match(branch) or ".." in branch):
        return None, _refusal("bad-branch", index, raw, f"source_branch {_shown(branch)} is not a branch name")
    for field in ("note", "evidence"):
        if raw.get(field) is not None and not _text_ok(raw[field]):
            return None, _refusal("bad-text", index, raw, f"`{field}` must be one line of up to 300 characters "
                                  "that does not look like a credential")
    return {"copy": ids["copy"], "source": ids["source"], "kind": kind, "copy_point": points["copy_point"],
            "sync_base": points["sync_base"], "builder": builder, "source_branch": branch,
            "note": raw.get("note"), "evidence": raw.get("evidence")}, None


def read_config(path: Path, repos: dict[str, dict], label: str = "config/derivations.json"
                ) -> tuple[list[dict], list[dict], list[dict]]:
    """(accepted entries, refusals, degraded) from the curated file. An absent file is no relation."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], [], []
    except OSError as exc:
        return [], [], [{"source": label, "reason": f"unreadable: {type(exc).__name__}"}]
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return [], [], [{"source": label, "reason": f"refused (unreadable): not JSON ({exc.msg} at line {exc.lineno})"}]
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("derivations"), list):
        return [], [], [{"source": label, "reason": f"refused (unreadable): the file must be an object with "
                                                    f"`schema`: \"{SCHEMA}\" and a `derivations` list"}]
    accepted, refused = [], []
    for index, raw in enumerate(doc["derivations"]):
        entry, refusal = _entry(index, raw, repos)
        if refusal:
            refused.append(refusal)
        elif entry["kind"] == "copy" and entry["copy_point"] is None:
            refused.append(_refusal("missing-copy-point", index, raw,
                                    f"{entry['copy']} is a copy with no copy_point, so how far behind it is "
                                    "cannot be measured"))
        else:
            accepted.append({**entry, "_index": index})
    # ONE SOURCE PER COPY. Two entries naming one copy are both withheld: which is right is
    # the operator's decision, and "the last one wins" would make it silently.
    by_copy: dict[str, list[dict]] = {}
    for e in accepted:
        by_copy.setdefault(e["copy"], []).append(e)
    kept = []
    for e in accepted:
        if len(by_copy[e["copy"]]) > 1:
            others = ", ".join(str(o["_index"]) for o in by_copy[e["copy"]] if o is not e)
            refused.append(_refusal("duplicate-copy", e["_index"], e,
                                    f"{e['copy']} is also the copy of entry {others}; a copy has one source"))
        else:
            kept.append(e)
    # CYCLES. With one source per copy every repository has at most one outgoing edge, so a
    # walk from each copy either ends or returns to a repository it has seen.
    edge = {e["copy"]: e["source"] for e in kept}
    in_cycle: set[str] = set()
    for start in edge:
        seen, node = [], start
        while node in edge and node not in seen:
            seen.append(node)
            node = edge[node]
        if node in seen:
            in_cycle |= set(seen[seen.index(node):])
    final = []
    for e in kept:
        if e["copy"] in in_cycle:
            loop = " -> ".join(_cycle_from(e["copy"], edge))
            refused.append(_refusal("cycle", e["_index"], e, f"the derivations form a cycle: {loop}"))
        else:
            final.append(e)
    refused.sort(key=lambda r: (r["entry"], r["code"]))
    degraded = [{"source": label, "reason": r["reason"]} for r in refused]
    return final, refused, degraded


def _cycle_from(start: str, edge: dict[str, str]) -> list[str]:
    out, node = [start], edge[start]
    while node != start and len(out) < 50:
        out.append(node)
        node = edge[node]
    return out + [start]


def _clone(repo: dict | None) -> str | None:
    path = ((repo or {}).get("local") or {}).get("path")
    return path if isinstance(path, str) and path and os.path.isdir(path) else None


def read_upstream(clone: str | None, run: Runner) -> dict:
    """What the copy's tracked `.copy/upstream.json` says at HEAD.

    `state`: `read` (with the values), `absent` (not tracked at HEAD), `no-clone` (no local
    clone to read) or `unreadable` (with the reason). A `source` field, if the file has one,
    is ignored: the copy is not where the source is named."""
    if not clone:
        return {"state": "no-clone", "reason": "no local clone of the copy on this machine"}
    rc, _ = run(["cat-file", "-e", f"HEAD:{UPSTREAM_FILE}"], clone)
    if rc != 0:
        return {"state": "absent", "reason": f"{UPSTREAM_FILE} is not tracked at HEAD of the copy's clone"}
    # THE SIZE FIRST, from git, so a large blob is refused before it is read into memory.
    rc, size = run(["cat-file", "-s", f"HEAD:{UPSTREAM_FILE}"], clone)
    if rc != 0 or not size.strip().isdigit():
        return {"state": "unreadable", "reason": f"git could not size {UPSTREAM_FILE} (exit {rc})"}
    if int(size.strip()) > UPSTREAM_MAX_BYTES:
        return {"state": "unreadable", "reason": f"{UPSTREAM_FILE} is over {UPSTREAM_MAX_BYTES} bytes"}
    rc, text = run(["cat-file", "-p", f"HEAD:{UPSTREAM_FILE}"], clone)
    if rc != 0:
        return {"state": "unreadable", "reason": f"git could not read {UPSTREAM_FILE} (exit {rc})"}
    if len(text.encode("utf-8")) > UPSTREAM_MAX_BYTES:
        return {"state": "unreadable", "reason": f"{UPSTREAM_FILE} is over {UPSTREAM_MAX_BYTES} bytes"}
    try:
        doc = json.loads(text)
    except ValueError:
        return {"state": "unreadable", "reason": f"{UPSTREAM_FILE} is not JSON"}
    except Exception as exc:                                            # noqa: BLE001  RecursionError
        return {"state": "unreadable", "reason": f"{UPSTREAM_FILE} could not be parsed ({type(exc).__name__})"}
    if not isinstance(doc, dict):
        return {"state": "unreadable", "reason": f"{UPSTREAM_FILE} is not an object"}
    out: dict[str, Any] = {"state": "read", "kind": None, "copy_kind": None, "copy_point": None,
                           "sync_base": None, "deny": None, "problems": []}
    if doc.get("kind") is not None:
        if doc["kind"] in UPSTREAM_KINDS:
            out["kind"], out["copy_kind"] = UPSTREAM_KINDS[doc["kind"]], doc["kind"]
        else:
            out["problems"].append(f"kind {_shown(doc['kind'])} is not `rewritten`, `export` or `move`")
    if doc.get("sync_base") is None and doc.get("last_synced") is not None:
        doc = {**doc, "sync_base": doc["last_synced"]}          # the alias, read only when sync_base is absent
    for field in ("copy_point", "sync_base"):
        if doc.get(field) is not None:
            out[field] = _point(doc[field])
            if out[field] is None:
                out["problems"].append(f"`{field}` is not a source commit or {{source_commit, copy_commit?, date?}}")
    if doc.get("deny") is not None:
        deny = doc["deny"]
        if (isinstance(deny, list) and len(deny) <= 200
                and all(isinstance(p, str) and 0 < len(p) <= 200 and not p.startswith(("/", ":", "-"))
                        and ".." not in p.split("/") and "\n" not in p for p in deny)):
            out["deny"] = sorted(set(deny))
        else:
            out["problems"].append("`deny` is not a list of relative paths")
    return out


def measure_behind(clone: str | None, branch: str | None, since: str | None, deny: list[str] | None,
                   run: Runner) -> dict:
    """How many commits the source branch has after `since`, read in the source's local clone.

    Never fetches: the count is as of the clone's last fetch, and says which ref it read."""
    unknown = lambda why: {"state": "unknown", "commits": None, "outside_deny": None, "reason": why}  # noqa: E731
    if since is None:
        return unknown("no sync_base or copy_point to count from")
    if not clone:
        return unknown("no local clone of the source on this machine; clone it to measure")
    if not branch:
        return unknown("the source's default branch is not known; set source_branch")
    ref = None
    for candidate in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        rc, out = run(["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"], clone)
        if rc == 0 and out.strip():
            ref, head = candidate, out.strip()
            break
    if ref is None:
        return unknown(f"branch {branch} is not in the local clone of the source")
    rc, out = run(["rev-parse", "--verify", "--quiet", f"{since}^{{commit}}"], clone)
    if rc != 0 or not out.strip():
        return unknown(f"commit {since} is not in the local clone of the source: fetch it, or the record is wrong")
    full = out.strip()
    rc, out = run(["rev-list", "--count", f"{full}..{ref}"], clone)
    if rc != 0 or not out.strip().isdigit():
        return unknown(f"git could not count the commits after {since} (exit {rc})")
    commits = int(out.strip())
    rc, _ = run(["merge-base", "--is-ancestor", full, ref], clone)
    # OFF THE BRANCH, `since..ref` is every commit of the branch after the point where the copy's
    # history left it (their merge base), not after the recorded commit: said, with that point.
    base = None
    if rc != 0:
        brc, bout = run(["merge-base", full, ref], clone)
        base = bout.strip()[:12] if brc == 0 and bout.strip() else None
    result: dict[str, Any] = {"state": "measured", "commits": commits, "outside_deny": None,
                              "ref": ref.replace("refs/remotes/", "").replace("refs/heads/", ""),
                              "head": head[:12], "on_branch": rc == 0, "counted_from": base or full[:12],
                              "oldest_on": None, "reason": "as of the source clone's last fetch"}
    if deny is not None:
        if not commits or not deny:
            result["outside_deny"] = commits
        else:
            spec = ["."] + [f":(exclude){p}" for p in deny]
            # --full-history: without it a side branch merged back TREESAME is simplified away,
            # and its commits in kept paths go uncounted.
            rc, out = run(["rev-list", "--count", "--full-history", f"{full}..{ref}", "--", *spec], clone)
            if rc == 0 and out.strip().isdigit():
                result["outside_deny"] = int(out.strip())
    if commits:
        rc, out = run(["log", "--reverse", "--format=%cs", f"{full}..{ref}"], clone)
        first = out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
        result["oldest_on"] = first if _DATE.match(first) else None
    return result


def _relation(e: dict, repos: dict[str, dict], run: Runner) -> dict:
    """One accepted entry, read and measured."""
    copy_repo, source_repo = repos[e["copy"]], repos[e["source"]]
    upstream = read_upstream(_clone(copy_repo), run)
    provenance = {"kind": "curated"}
    values: dict[str, Any] = {}
    for field in ("copy_point", "sync_base"):
        if e[field] is not None:
            values[field], provenance[field] = e[field], "curated"
        elif upstream.get(field) is not None and not (field == "sync_base" and e["kind"] == "move"):
            values[field], provenance[field] = upstream[field], "copy"
        else:
            values[field] = None
    deny = upstream.get("deny") if upstream.get("state") == "read" else None
    if deny is not None:
        provenance["deny"] = "copy"
    differs = []
    if upstream.get("state") == "read":
        if upstream.get("kind") and upstream["kind"] != e["kind"]:
            differs.append({"field": "kind", "curated": e["kind"], "copy": upstream["copy_kind"]})
        for field in ("copy_point", "sync_base"):
            mine, theirs = e[field], upstream.get(field)
            if mine and theirs and not same_commit(mine["source_commit"], theirs["source_commit"]):
                differs.append({"field": field, "curated": mine["source_commit"],
                                "copy": theirs["source_commit"]})
    branch = e["source_branch"] or source_repo.get("default_branch") or None
    since_point = values["sync_base"] or values["copy_point"]
    behind = None
    if e["kind"] == "copy":
        behind = measure_behind(_clone(source_repo), branch, since_point and since_point["source_commit"],
                                deny, run)
        behind["since"] = "sync_base" if values["sync_base"] else "copy_point"
    record = {k: v for k, v in upstream.items() if k in ("state", "reason", "problems", "copy_kind")}
    record["differs"] = differs
    return {
        "copy": e["copy"], "source": e["source"], "kind": e["kind"], "source_branch": branch,
        "copy_branch": copy_repo.get("default_branch") or None,
        "copy_point": values["copy_point"], "sync_base": values["sync_base"],
        "builder": e["builder"], "deny": deny, "provenance": provenance,
        "copy_record": record, "behind": behind,
        "source_archived": source_repo.get("archived"),
        **({"note": e["note"]} if e["note"] else {}),
        **({"evidence": e["evidence"]} if e["evidence"] else {})}


def _unbuilt(e: dict, repos: dict[str, dict], exc: BaseException) -> dict:
    """An entry whose reading or measuring failed: the curated values, and why nothing was measured."""
    why = f"not measured: {type(exc).__name__}"
    return {"copy": e["copy"], "source": e["source"], "kind": e["kind"],
            "source_branch": e["source_branch"], "copy_branch": repos[e["copy"]].get("default_branch") or None,
            "copy_point": e["copy_point"], "sync_base": e["sync_base"], "builder": e["builder"], "deny": None,
            "provenance": {"kind": "curated", **{f: "curated" for f in ("copy_point", "sync_base") if e[f]}},
            "copy_record": {"state": "unreadable", "reason": why, "differs": []},
            "behind": ({"state": "unknown", "commits": None, "outside_deny": None, "reason": why,
                        "since": "sync_base" if e["sync_base"] else "copy_point"} if e["kind"] == "copy" else None),
            "source_archived": repos[e["source"]].get("archived")}


def build(config: Path, repositories: Iterable[dict], obs_date: str, *, run: Runner | None = None,
          label: str = "config/derivations.json") -> dict:
    """The registry document: every accepted relation with its measurement, the copies that
    say they are copies but are not recorded, the refusals, and what could not be read."""
    run = run or _run_git
    repos = {r["id"]: r for r in repositories if isinstance(r, dict) and isinstance(r.get("id"), str)}
    entries, refused, degraded = read_config(config, repos, label)
    relations = []
    for e in entries:
        try:
            relations.append(_relation(e, repos, run))
        except Exception as exc:                                        # noqa: BLE001  one copy, not all
            relations.append(_unbuilt(e, repos, exc))
            degraded.append({"source": label, "reason": f"{e['copy']}: not measured ({type(exc).__name__})"})
    # THE COPIES THAT SAY SO, AND NOTHING HERE RECORDS. Read only where the worktree holds
    # the file, so a tick does not ask git about every repository on the machine.
    recorded = {e["copy"] for e in entries}
    refused_copies = {r["copy"] for r in refused if r.get("copy")}
    unrecorded = []
    for rid in sorted(repos):
        if rid in recorded:
            continue
        try:
            clone = _clone(repos[rid])
            if not clone or not os.path.isfile(os.path.join(clone, *UPSTREAM_FILE.split("/"))):
                continue
            upstream = read_upstream(clone, run)
        except Exception as exc:                                        # noqa: BLE001  one clone, not all
            degraded.append({"source": UPSTREAM_FILE, "reason": f"{rid}: not read ({type(exc).__name__})"})
            continue
        if upstream["state"] != "read":
            continue
        unrecorded.append({"copy": rid, "kind": upstream.get("kind"),
                           "refused": rid in refused_copies,
                           "reason": f"{rid} carries {UPSTREAM_FILE} but no accepted entry in {label} "
                                     "names its source"})
    totals = {"relations": len(relations), "copies": sum(1 for r in relations if r["kind"] == "copy"),
              "moves": sum(1 for r in relations if r["kind"] == "move"), "refused": len(refused),
              "unrecorded": len(unrecorded),
              "behind": sum(1 for r in relations if (r["behind"] or {}).get("commits"))}
    return {"schema_version": SCHEMA_VERSION, "schema": SCHEMA, "updated_on": obs_date, "note": NOTE,
            "relations": relations, "unrecorded": unrecorded, "refused": refused, "degraded": degraded,
            "totals": totals, "source_refs": [SOURCE_ID]}


NOTE = ("Repositories derived from other repositories: a copy kept in step with its source, or a move. "
        "Curated by the operator in config/derivations.json; each copy's tracked .copy/upstream.json is read "
        "from its local clone, and how far the source has moved on is counted in the source's local clone, "
        "never fetched. Rebuilt on every emit; nothing here writes to a repository.")


def empty_document(obs_date: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "schema": SCHEMA, "updated_on": obs_date, "note": NOTE,
            "relations": [], "unrecorded": [], "refused": [], "degraded": [],
            "totals": {"relations": 0, "copies": 0, "moves": 0, "refused": 0, "unrecorded": 0, "behind": 0},
            "source_refs": [SOURCE_ID]}


# ---- readers ---------------------------------------------------------------------------

def load(registry: Path, config: Path) -> tuple[dict | None, list[dict]]:
    """(the registry document or None, degraded) for a reader.

    No document and no curated file: there is no relation to show, and nothing is degraded.
    A curated file the emit has not turned into a document yet is said, not hidden."""
    path = registry / "derivations.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if config.is_file():
            return None, [{"source": "registry/derivations.json",
                           "reason": "absent: the curated derivations were not emitted yet; run "
                                     "`project-observatory full emit`"}]
        return None, []
    except (OSError, ValueError) as exc:
        return None, [{"source": "registry/derivations.json", "reason": f"unreadable: {type(exc).__name__}"}]
    if not isinstance(doc, dict) or not isinstance(doc.get("relations"), list):
        return None, [{"source": "registry/derivations.json", "reason": "unreadable: no `relations` list"}]
    return doc, []


def wire_row(rel: dict) -> dict:
    """One relation as the readers publish it: flat, the commits as the source spells them."""
    behind = rel.get("behind") or {}
    return {"copy": rel["copy"], "source": rel["source"], "kind": rel["kind"],
            "copy_point": (rel.get("copy_point") or {}).get("source_commit"),
            "sync_base": (rel.get("sync_base") or {}).get("source_commit"),
            "behind": behind.get("commits") if behind.get("state") == "measured" else None}


def relations_of(doc: dict | None, repository_ids: Iterable[str]) -> tuple[list[dict], list[dict], list[dict]]:
    """(derived_from, copies, degraded) for a set of repositories, both ways.

    `derived_from`: the relations in which one of them is the copy; `copies`: those in which
    one of them is the source. A refusal naming one of them is degraded, so a reader is told
    that a relation exists in the file but was not accepted."""
    if not doc:
        return [], [], []
    ids = set(repository_ids)
    derived = [wire_row(r) for r in doc.get("relations") or [] if isinstance(r, dict) and r.get("copy") in ids]
    copies = [wire_row(r) for r in doc.get("relations") or [] if isinstance(r, dict) and r.get("source") in ids]
    degraded = [{"source": "config/derivations.json", "reason": str(r.get("reason"))}
                for r in doc.get("refused") or []
                if isinstance(r, dict) and (r.get("copy") in ids or r.get("source") in ids)]
    return (sorted(derived, key=lambda r: (r["copy"], r["source"])),
            sorted(copies, key=lambda r: (r["copy"], r["source"])), degraded)
