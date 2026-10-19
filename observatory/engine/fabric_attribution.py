"""Which Fabric project each Observatory project is: pulled from Fabric, kept in Observatory's store.

Fabric owns the canonical project id (fabric-agent-contract DEC-0089; Fabric ADR-0166, "Fabric is
the core"). Observatory's `project:<slug>` is recorded against it as an alias, and the record is
Observatory's own: each tick `collectors/scan_fabric_attribution.py` asks Fabric's hub
(`fabric_hub.Resolver`) for every folder the registry holds and writes the answers to
`store/raw/fabric-attribution.json`. Fabric writes nothing here (D5).

What the record says, and why each rule:

- **An alias is a measurement, not a proposal.** `fabric:<uuid>` on `project:<slug>` carries its
  provenance (`fabric`, the hub's pid, the time). It needs no review: Fabric owns the id it names.
- **Two Fabric projects for one Observatory project is a conflict, never a merge.** No alias is
  kept; `fabric.attribution_conflict` names it.
- **An answer can be undone.** A later `detached` or `no-project` for every folder withdraws the
  alias; a different uuid replaces it. Each change is a `history` row; nothing is rewritten.
- **A folder in no Fabric project stays unattributed** and is asked again next tick; Observatory
  never creates a Fabric project.
- **An unreachable hub changes nothing it recorded.** The reason (`hub-absent`, `hub-dead`,
  `hub-origin-refused`, `access-pending`, `access-refused`, `tool-absent`, `hub-error`, `budget`)
  is named in `degraded`, and the last answers stay.

While Fabric is not running, `attribute_local` applies the contract's own rules
(`folder_attribution.py`, its vectors in `fabric/attribution-vectors/`) to what Observatory knows:
its projects and their folders, the folders that hold agent worktrees (`_*` under the projects
root) and `folder_exclusions.json` as declared containers. A container ends a listed folder's
reach, so this is load-bearing, not cosmetic.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import folder_attribution
import paths
import safe_git

SCHEMA_VERSION = 1
OUTPUT = "fabric-attribution.json"
#: A success is asked again after this long; a refusal every tick.
FRESH_SECONDS = 6 * 3600
MAX_FOLDERS = 500
BUDGET_SECONDS = 60.0
HISTORY_LIMIT = 500
GIT_TIMEOUT = 5
_UUID = re.compile(r"^fabric:[0-9A-Za-z-]{8,64}\Z")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def output_path() -> Path:
    return paths.SCRATCH / OUTPUT


def load() -> dict:
    try:
        doc = json.loads(output_path().read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _registry(name: str) -> dict:
    try:
        doc = json.loads((paths.REGISTRY / name).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _real(path: str | os.PathLike) -> str:
    return Path(os.path.realpath(path)).as_posix()


# ---- the facts a snapshot is built from ------------------------------------------------------------

def common_dir_of(path: str) -> str | None:
    """A checkout's Git common dir, read from disk without running Git (cheap, for every project folder)."""
    dot = Path(path) / ".git"
    if dot.is_dir():
        return _real(dot)
    if dot.is_file():
        try:
            line = dot.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if line.startswith("gitdir:"):
            gitdir = Path(path) / line[7:].strip()
            common = gitdir / "commondir"
            try:
                return _real(gitdir / common.read_text(encoding="utf-8").strip())
            except OSError:
                return _real(gitdir)
    return None


def project_folders(projects: list[dict] | None = None, repositories: list[dict] | None = None,
                    relations: list[dict] | None = None) -> dict[str, list[dict]]:
    """project id -> its folders as the contract's snapshot spells them, from the registry."""
    projects = projects if projects is not None else _registry("projects.json").get("projects") or []
    repositories = repositories if repositories is not None else _registry("repositories.json").get("repositories") or []
    relations = relations if relations is not None else _registry("relations.json").get("relations") or []
    repos = {r.get("id"): r for r in repositories if isinstance(r, dict)}
    by_project: dict[str, list[str]] = {}
    for r in relations:
        if isinstance(r, dict) and r.get("type") == "implemented_by":
            by_project.setdefault(str(r.get("from")), []).append(str(r.get("to")))
    data = paths.DATA
    out: dict[str, list[dict]] = {}
    for p in projects:
        pid = p.get("id") if isinstance(p, dict) else None
        if not isinstance(pid, str):
            continue
        found: dict[str, str | None] = {}
        for name in p.get("local_folders") or []:
            if isinstance(name, str) and name:
                found.setdefault(name if os.path.isabs(name) else str(data / name), None)
        local = (p.get("local_only") or {}).get("path")
        if isinstance(local, str) and local:
            found.setdefault(local, None)
        for rid in by_project.get(pid, []):
            rep = repos.get(rid) or {}
            loc = rep.get("local") or {}
            for where in [loc.get("path"), *((c or {}).get("path") for c in loc.get("extra_checkouts") or [])]:
                if isinstance(where, str) and where:
                    found[where] = rep.get("url") if isinstance(rep.get("url"), str) else found.get(where)
        folders = []
        for where, origin in found.items():
            if not os.path.isdir(where):
                continue
            real = _real(where)
            if not folder_attribution.is_path(real):
                continue
            common = common_dir_of(real)
            folders.append({"path": real, "commonDir": common, "origin": origin,
                            "toplevel": real if common else None})
        out[pid] = folders
    return out


def containers() -> list[str]:
    """Declared containers: `_*` folders under the projects root and `folder_exclusions.json`."""
    data = paths.DATA
    names: set[str] = set()
    prefixes: list[str] = []
    try:
        doc = json.loads(paths.config_file("folder_exclusions.json").read_text(encoding="utf-8"))
        names = {x["name"] for x in doc.get("names") or [] if isinstance(x, dict) and isinstance(x.get("name"), str)}
        prefixes = [x["prefix"] for x in doc.get("prefixes") or [] if isinstance(x, dict) and isinstance(x.get("prefix"), str)]
    except (OSError, ValueError, AttributeError):
        pass
    out: set[str] = set()
    try:
        children = list(os.scandir(data))
    except OSError:
        children = []
    for child in children:
        if not child.is_dir(follow_symlinks=True):
            continue
        if child.name.startswith("_") or child.name in names or any(child.name.startswith(p) for p in prefixes if p):
            real = _real(child.path)
            if folder_attribution.is_path(real):
                out.add(real)
    return sorted(out)


def temp_roots() -> list[str]:
    home = Path.home()
    candidates = [tempfile.gettempdir(), "/tmp", "/private/tmp", "/private/var/folders", "/var/tmp",
                  home / ".cache", home / "Library" / "Caches", home / "AppData" / "Local" / "Temp"]
    out: list[str] = []
    for c in candidates:
        if os.path.isdir(c):
            for form in (Path(c).as_posix(), _real(c)):
                if folder_attribution.is_path(form) and form not in out:
                    out.append(form)
    return out


class GitProbeFailed(Exception):
    """Git could not be read: the contract says do not attribute (a failed probe is not "no Git")."""


def git_facts(folder: str, home: str) -> dict | None:
    """The snapshot's `git`: None outside Git (or for a home kept under Git); raises when the probe fails."""
    def git(*args: str) -> subprocess.CompletedProcess:
        try:
            return safe_git.run(list(args), repo=folder, timeout=GIT_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitProbeFailed(type(exc).__name__) from None

    top = git("rev-parse", "--show-toplevel", "--git-common-dir")
    if top.returncode != 0:
        if "not a git repository" in (top.stderr or "").lower():
            return None
        raise GitProbeFailed("rev-parse")
    lines = [ln for ln in (top.stdout or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        # A bare repository has no top level: its folder is the common dir.
        common = _real(os.path.join(folder, lines[0])) if lines else _real(folder)
        toplevel = common
    else:
        toplevel, common = _real(lines[0]), _real(os.path.join(folder, lines[1]))
    if toplevel == "/" or folder_attribution.under(home, toplevel):
        return None  # a home kept under Git is not a repository folders belong to
    main = toplevel
    listing = git("worktree", "list", "--porcelain")
    if listing.returncode == 0:
        first = (listing.stdout or "").split("\n\n")[0].splitlines()
        if first and first[0].startswith("worktree "):
            main = _real(first[0][9:])
            if "bare" in first[1:]:
                main = common
    origin = git("config", "--get", "remote.origin.url")
    return {"toplevel": toplevel, "commonDir": common, "mainCheckout": main,
            "origin": (origin.stdout or "").strip() or None if origin.returncode == 0 else None}


def snapshot(folder: str, folders: dict[str, list[dict]] | None = None) -> dict:
    """The contract's snapshot for `folder`, from what Observatory knows."""
    home = _real(Path.home())
    real = _real(folder)
    folders = folders if folders is not None else project_folders()
    return {"folder": real, "home": home, "tempRoots": temp_roots(), "git": git_facts(real, home),
            "projects": [{"id": pid, "folders": fl, "sources": []} for pid, fl in sorted(folders.items())],
            "containers": containers()}


def attribute_local(folder: str, folders: dict[str, list[dict]] | None = None) -> dict:
    """`folder_attribution.explain_folder` over Observatory's own facts; `{"probe": "failed"}` when Git could not be read."""
    try:
        snap = snapshot(folder, folders)
    except GitProbeFailed:
        return {"answer": {"fault": "git-probe-failed"}, "row": None, "record": None}
    return folder_attribution.explain_folder(snap)


# ---- the pull ---------------------------------------------------------------------------------------

def _history(doc: dict, row: dict) -> None:
    doc.setdefault("history", []).append(row)
    doc["history"] = doc["history"][-HISTORY_LIMIT:]


def collect(previous: dict, resolver_factory: Callable[[], Any], *, folders: dict[str, list[dict]] | None = None,
            budget: float = BUDGET_SECONDS, clock: Callable[[], float] = time.monotonic, stamp: str | None = None) -> dict:
    """One pass: ask the hub for every project folder, fold the answers into the alias record."""
    import fabric_hub

    at = stamp or now()
    folders = folders if folders is not None else project_folders()
    doc = {"schema_version": SCHEMA_VERSION, "checked_at": at, "hub": None, "degraded": [],
           "aliases": {}, "conflicts": [], "unattributed": [],
           "folders": dict(previous.get("folders") or {}), "history": list(previous.get("history") or [])}
    prior = previous.get("aliases") if isinstance(previous.get("aliases"), dict) else {}
    started = clock()
    resolver = None
    unavailable: str | None = None
    asked = 0
    answered: dict[str, list[dict]] = {}
    fresh_after = time.time() - FRESH_SECONDS
    for pid in sorted(folders):
        for entry in folders[pid]:
            path = entry["path"]
            cached = doc["folders"].get(path)
            if (isinstance(cached, dict) and cached.get("ok") and isinstance(cached.get("epoch"), (int, float))
                    and cached["epoch"] >= fresh_after):
                answered.setdefault(pid, []).append(cached)
                continue
            if unavailable:
                continue
            if asked >= MAX_FOLDERS or clock() - started > budget:
                unavailable = unavailable or "budget"
                continue
            try:
                if resolver is None:
                    resolver = resolver_factory()
                result = resolver.resolve(path)
            except fabric_hub.HubUnavailable as exc:
                unavailable = exc.code
                doc["degraded"].append({"source": "fabric-hub", "reason": exc.code, "detail": exc.detail})
                continue
            except (OSError, RuntimeError, ValueError) as exc:
                # The credential store or the answer could not be handled: said, never a failed tick.
                unavailable = "hub-error"
                doc["degraded"].append({"source": "fabric-hub", "reason": "hub-error", "detail": type(exc).__name__})
                continue
            asked += 1
            row = {**result, "at": at, "epoch": time.time()}
            doc["folders"][path] = row
            answered.setdefault(pid, []).append(row)
    if unavailable == "budget":
        doc["degraded"].append({"source": "fabric-hub", "reason": "budget",
                                "detail": f"asked {asked} folder(s) within {budget:.0f} s; the rest next tick"})
    hub = getattr(resolver, "hub", None)
    if hub:
        doc["hub"] = {"origin": hub["origin"], "pid": hub["pid"], "startedAt": hub.get("startedAt")}
    # Forget folders the registry no longer holds.
    live = {e["path"] for fl in folders.values() for e in fl}
    doc["folders"] = {k: v for k, v in doc["folders"].items() if k in live}

    for pid in sorted(folders):
        rows = answered.get(pid) or []
        had = prior.get(pid) if isinstance(prior.get(pid), dict) else None
        uuids = sorted({r["projectId"] for r in rows if r.get("ok")})
        refusals = [r.get("refusal") for r in rows if not r.get("ok")]
        if len(uuids) > 1:
            doc["conflicts"].append({"project": pid, "fabric": ["fabric:" + u for u in uuids],
                                     "folders": sorted(p for p, r in doc["folders"].items()
                                                       if r.get("ok") and any(e["path"] == p for e in folders[pid]))})
            if had:
                _history(doc, {"at": at, "project": pid, "change": "withdrawn", "from": had["alias"],
                               "reason": "conflict: its folders resolve to several Fabric projects"})
            continue
        if len(uuids) == 1:
            best = next(r for r in rows if r.get("ok") and r["projectId"] == uuids[0])
            alias = "fabric:" + uuids[0]
            record = {"alias": alias, "name": best.get("name"), "root": best.get("root"), "how": best.get("how"),
                      "remoteKey": best.get("remoteKey"),
                      "provenance": {"source": "fabric", "pid": (doc["hub"] or {}).get("pid", (had or {}).get("provenance", {}).get("pid")),
                                     "at": best.get("at", at)}}
            if had and had.get("alias") == alias:
                record["provenance"] = had.get("provenance") or record["provenance"]
            doc["aliases"][pid] = record
            if not had:
                _history(doc, {"at": at, "project": pid, "change": "added", "to": alias})
            elif had.get("alias") != alias:
                _history(doc, {"at": at, "project": pid, "change": "replaced", "from": had.get("alias"), "to": alias})
            continue
        if refusals and len(refusals) == len(folders[pid]):
            # Every folder answered, none in a Fabric project: withdraw what was recorded.
            doc["unattributed"].append(pid)
            if had:
                _history(doc, {"at": at, "project": pid, "change": "withdrawn", "from": had.get("alias"),
                               "reason": "Fabric answered " + ", ".join(sorted({str(r) for r in refusals}))})
            continue
        if had:
            doc["aliases"][pid] = had  # not asked, or not all folders answered: the record stands
        elif folders[pid]:
            doc["unattributed"].append(pid)
    return doc


def write(doc: dict, out: Path | None = None) -> None:
    out = out or output_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, out)


# ---- lookups ----------------------------------------------------------------------------------------

def aliases(doc: dict | None = None) -> dict[str, dict]:
    """project id -> its recorded Fabric alias record."""
    doc = doc if doc is not None else load()
    got = doc.get("aliases")
    return {k: v for k, v in got.items() if isinstance(v, dict) and _UUID.match(str(v.get("alias")))} if isinstance(got, dict) else {}


def projects_for(alias: str, doc: dict | None = None) -> list[str]:
    """The Observatory projects a `fabric:<uuid>` names (two is a tie the caller reports)."""
    return sorted(pid for pid, rec in aliases(doc).items() if rec.get("alias") == alias)


def session_project(cwd: str, *, timeout: float = 2.0) -> dict:
    """At a session's start: ask Fabric first, the contract's rules locally second (D4).

    `{"source": "fabric"|"local", "project": id}`, `{"source": ..., "ambiguous": [ids]}`, or
    `{"source": None, "reason": ...}`. Never asks Fabric for access here: that is the tick's.
    """
    import fabric_hub

    reason = ""
    try:
        answer = fabric_hub.Resolver(timeout=timeout, ask_access=False).resolve(_real(cwd))
        if answer.get("ok"):
            mine = projects_for("fabric:" + answer["projectId"])
            if len(mine) == 1:
                return {"source": "fabric", "project": mine[0]}
            if len(mine) > 1:
                return {"source": "fabric", "ambiguous": mine}
            reason = "fabric-project-not-recorded"
        else:
            reason = "fabric:" + str(answer.get("refusal"))
    except fabric_hub.HubUnavailable as exc:
        reason = exc.code
    explained = attribute_local(cwd)
    answer = explained["answer"]
    if answer.get("outcome") == "attached":
        return {"source": "local", "project": answer["project"], "row": explained["row"], "fabric": reason}
    if answer.get("outcome") == "ambiguous":
        return {"source": "local", "ambiguous": answer["projects"], "row": explained["row"], "fabric": reason}
    return {"source": None, "reason": reason, "local": answer}


def conflict_findings(doc: dict, titled: Callable[..., dict]) -> list[dict]:
    """`fabric.attribution_conflict` rows: a project whose folders resolve to two Fabric projects.

    Reported, never merged: which Fabric project is right is decided in Fabric (detach a folder there,
    or move it), and until then no alias is kept. `titled` is `finding_types.titled`."""
    rows = []
    for c in (doc.get("conflicts") or []) if isinstance(doc, dict) else []:
        if not isinstance(c, dict) or not isinstance(c.get("project"), str):
            continue
        rows.append({
            "type": "fabric.attribution_conflict", "subject": c["project"], "severity": "warning",
            **titled("{project} belongs to {count} Fabric projects", project=c["project"],
                     count=len(c.get("fabric") or [])),
            "detail": ("Fabric attributes this project's folders to " + ", ".join(c.get("fabric") or [])
                       + "; Observatory keeps no Fabric alias for it until they agree."),
            "action": "in Fabric, detach the folder that belongs elsewhere or move it into one project; the next "
                      "tick (or `project-observatory full fabric-attribution`) records the answer",
            "evidence": ["store/raw/fabric-attribution.json"]})
    return rows
