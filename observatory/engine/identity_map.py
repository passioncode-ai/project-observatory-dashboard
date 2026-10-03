"""Persisted identity map: which merge keys and anchors belong to which project id.

Contract: docs/design/IDENTITY.md (repository root). In short:

- an id is minted once and never reused, even after its project is retired;
- a project whose merge key changed keeps its recorded id when exactly one
  recorded project shares a STRONG anchor with it (a repository it is
  implemented by, including a repository's former name; the root commit of a
  local Git history with no remote; the absolute path of a checkout);
- ambiguity is never guessed: it mints a new id and is reported;
- a project absent from a complete scan is retired, never deleted;
- identity_overrides.json always wins.

The map lives at registry/identity.json and is written by the emit step. This
module only computes; it does no I/O except reading a Git root commit.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import safe_git

SCHEMA_VERSION = 1


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "-", value.lower()).strip("-")


def empty() -> dict:
    return {"schema_version": SCHEMA_VERSION, "projects": {}, "repositories": {}, "ambiguities": []}


def root_commit(path: str | None) -> str | None:
    """The first commit of a local history, or None. Only asked for unpublished folders."""
    if not path or not (Path(path) / ".git").exists():
        return None
    try:
        p = safe_git.run(["rev-list", "--max-parents=0", "HEAD"], repo=path, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    roots = sorted(p.stdout.split()) if p.returncode == 0 else []
    return roots[0] if roots else None


def anchors_of(project: dict, repos: dict, former_names: dict[str, list[str]]) -> dict:
    """Strong anchors of one merged project (names are never anchors)."""
    repositories = set()
    for nwo in project.get("repos") or []:
        repositories.add(nwo)
        repositories.update(former_names.get(nwo, []))
    checkouts = set()
    for nwo in project.get("repos") or []:
        local = (repos.get(nwo) or {}).get("local") or {}
        if local.get("path"):
            checkouts.add(local["path"])
        for extra in local.get("extra_checkouts") or []:
            if extra.get("path"):
                checkouts.add(extra["path"])
    roots = set()
    lo = project.get("local_only") or {}
    if lo.get("path"):
        checkouts.add(lo["path"])
        if lo.get("unpublished"):
            r = root_commit(lo["path"])
            if r:
                roots.add(r)
    return {"repositories": sorted(repositories), "root_commits": sorted(roots),
            "checkouts": sorted(checkouts)}


def _shares(a: dict, b: dict) -> bool:
    return any(set(a.get(k) or []) & set(b.get(k) or []) for k in ("repositories", "root_commits", "checkouts"))


def _union(a: dict, b: dict) -> dict:
    return {k: sorted(set(a.get(k) or []) | set(b.get(k) or [])) for k in ("repositories", "root_commits", "checkouts")}


def resolve(projects: dict, repos: dict, transfers: dict[str, str], overrides: dict[str, str],
            doc: dict | None, today: str, complete: bool) -> tuple[dict[str, str], dict, list[dict]]:
    """Return (key -> id, updated map, changes). `complete` is False when any source degraded.

    Deterministic: keys are processed in sorted order.
    """
    doc = {**empty(), **(doc or {})}
    entries: dict[str, dict] = {pid: dict(e) for pid, e in (doc.get("projects") or {}).items()}
    repo_former: dict[str, list[str]] = {}
    for rid, r in (doc.get("repositories") or {}).items():
        repo_former[rid.split(":", 1)[1]] = list(r.get("former") or [])
    for old, new in (transfers or {}).items():
        repo_former.setdefault(new, [])
        if old not in repo_former[new]:
            repo_former[new].append(old)
    key_owner = {k: pid for pid, e in entries.items() for k in e.get("keys") or []}
    used = set(entries)
    resolved: dict[str, str] = {}
    changes: list[dict] = []
    ambiguities: list[dict] = []

    def mint(base: str) -> str:
        candidate, n = f"project:{base}", 2
        while candidate in used:
            candidate, n = f"project:{base}-{n}", n + 1
        used.add(candidate)
        return candidate

    claimed: dict[str, str] = {}
    pending: list[str] = []

    def take(key: str, pid: str, anchors: dict) -> None:
        if pid in claimed:
            # Two current projects resolved to one id: the first (sorted) keeps it.
            ambiguities.append({"key": key, "candidates": [pid],
                                "reason": f"{claimed[pid]!r} already holds this id in this run"})
            pid = mint(slug(key))
            changes.append({"change": "minted", "id": pid, "key": key})
        claimed[pid] = key
        resolved[key] = pid
        e = entries.setdefault(pid, {"minted_on": today, "keys": [], "anchors": {}, "retired_on": None})
        e["keys"] = sorted(set(e.get("keys") or []) | {key})
        e["anchors"] = _union(e.get("anchors") or {}, anchors)
        e["last_seen_on"] = today
        if e.get("retired_on"):
            e["retired_on"] = None
            changes.append({"change": "returned", "id": pid, "key": key})

    anchors_by_key = {key: anchors_of(projects[key], repos, repo_former) for key in sorted(projects)}
    # Pass 1: explicit and already-known keys claim their ids first, so the order
    # of keys cannot decide which recorded project a newcomer is matched against.
    for key in sorted(projects):
        if key in overrides:
            pid = "project:" + overrides[key]
            used.add(pid)
            take(key, pid, anchors_by_key[key])
        elif key in key_owner:
            take(key, key_owner[key], anchors_by_key[key])
        else:
            pending.append(key)
    # Pass 2: a new key keeps a recorded id only through exactly one unclaimed
    # recorded project sharing a strong anchor; two candidates are ambiguous.
    for key in pending:
        anchors = anchors_by_key[key]
        matches = sorted(q for q, e in entries.items()
                         if not e.get("retired_on") and q not in claimed and _shares(anchors, e.get("anchors") or {}))
        if len(matches) == 1:
            changes.append({"change": "renamed", "id": matches[0], "key": key,
                            "was": sorted(entries[matches[0]].get("keys") or [])})
            take(key, matches[0], anchors)
            continue
        if len(matches) > 1:
            ambiguities.append({"key": key, "candidates": matches,
                                "reason": "shares strong anchors with more than one recorded project"})
        base = f"project:{slug(key)}"
        pid = base if base not in used else mint(slug(key))
        used.add(pid)
        changes.append({"change": "minted", "id": pid, "key": key})
        take(key, pid, anchors)
    if complete:
        for pid, e in entries.items():
            if pid not in claimed and not e.get("retired_on"):
                e["retired_on"] = today
                changes.append({"change": "retired", "id": pid})
    repositories = {f"repository:{new}": {"former": sorted(set(old))}
                    for new, old in repo_former.items() if old}
    out = {"schema_version": SCHEMA_VERSION, "projects": dict(sorted(entries.items())),
           "repositories": dict(sorted(repositories.items())), "ambiguities": ambiguities}
    return resolved, out, changes
