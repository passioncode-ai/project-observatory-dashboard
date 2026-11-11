#!/usr/bin/env python3
"""Add only, improve only: what the engine already shipped to its callers is never taken away or narrowed.

Operator rule, 2026-11-11 (rule R28 of the organization's agent templates): what an agent shipped stays readable
and repeatable; a changed thing is added beside the old one under a new name or version; a thing leaves only through
a `retired` row with its date and reason; a change is taken only when a test, an acceptance or a before/after reading
proves it no worse, and every change has its backlog row and its reason.

For this engine "what shipped to callers" is its tool surface, the thing an agent depends on:

- every MCP tool `mcp/server.py` declares with `@server.tool(...)`, with the parameters a caller may send and the ones
  it must send (read from the source with `ast`, so the check needs neither the MCP SDK nor a running server);
- every Fabric capability `fabric-agent.json` declares, with its effect.

The lock `tools/shipped.lock.json` remembers each of them. A later change may add tools, optional parameters and
capabilities, and may loosen a required parameter; it may never stop serving a locked tool or capability, drop a
parameter, require a new one or change a capability's effect. A capability's schemas need no row here: the manifest
pins them at a release tag (`tools/check_manifest_urls.py`), and a tag never changes.

The reference of the organization's templates hashes whole entries, which fits a style or a recipe but refuses every
change of a tool whose description and schema grow by optional fields; this check compares what a caller depends on
instead.

    python tools/check_add_only.py check [--base-ref REF]   # exit 1 on a finding
    python tools/check_add_only.py lock                     # add new names; refuses while check finds anything

`--base-ref` (default `origin/main`) reads the lock that revision holds, so a change cannot hide a removal by editing
the lock in the same pull request (`lock-rewritten`); when the ref does not resolve the check says so on stderr and
compares with the working lock only.

Exit 0 when nothing was found, 1 on a finding (each printed as `code name: detail`), 2 on a refusal.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "observatory" / "engine" / "mcp" / "server.py"
MANIFEST = ROOT / "observatory" / "engine" / "fabric-agent.json"
LOCK = ROOT / "tools" / "shipped.lock.json"
LOCK_PATH = "tools/shipped.lock.json"
SCHEMA = "add-only-lock/1"
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Parameters FastMCP fills itself; a caller never sends them.
INJECTED = {"self", "ctx", "context"}


def _is_tool_decorator(node: ast.expr) -> Optional[ast.Call]:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "tool" \
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "server":
        return node
    return None


def tools_of(source: str) -> Dict[str, Dict[str, List[str]]]:
    """The MCP tools a server source declares: name -> {"input": [...], "required": [...]}."""
    out: Dict[str, Dict[str, List[str]]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            call = _is_tool_decorator(deco)
            if call is None:
                continue
            name = node.name
            for kw in call.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    name = kw.value.value
            args = node.args
            positional = list(args.posonlyargs) + list(args.args)
            defaults_from = len(positional) - len(args.defaults)
            params, required = [], []
            for i, arg in enumerate(positional):
                if arg.arg in INJECTED:
                    continue
                params.append(arg.arg)
                if i < defaults_from:
                    required.append(arg.arg)
            for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                if arg.arg in INJECTED:
                    continue
                params.append(arg.arg)
                if default is None:
                    required.append(arg.arg)
            out[name] = {"input": sorted(params), "required": sorted(required)}
    return out


def capabilities_of(manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {c["name"]: {"effect": c.get("effect")} for c in manifest.get("capabilities") or []}


def served() -> Dict[str, Dict[str, Any]]:
    return {"tools": tools_of(SERVER.read_text(encoding="utf-8")),
            "capabilities": capabilities_of(json.loads(MANIFEST.read_text(encoding="utf-8")))}


def _retired_ok(row: Any) -> bool:
    return isinstance(row, dict) and bool(DATE.match(str(row.get("date", "")))) and bool(str(row.get("reason", "")).strip())


def check(now: Dict[str, Dict[str, Any]], lock: Dict[str, Any], base: Optional[Dict[str, Any]] = None) -> List[dict]:
    """Findings, each {code, name, detail}; none means the served surface keeps every promise the lock holds."""
    found: List[dict] = []

    def add(code: str, name: str, detail: str) -> None:
        found.append({"code": code, "name": name, "detail": detail})

    for kind in ("tools", "capabilities"):
        locked = lock.get(kind) or {}
        retired = (lock.get("retired") or {}).get(kind) or {}
        current = now.get(kind) or {}
        for name, row in sorted(retired.items()):
            if not _retired_ok(row):
                add("retired-incomplete", name, "a retired row needs a date (YYYY-MM-DD) and a reason")
            if name not in locked:
                add("retired-unknown", name, "only a locked name can be retired")
        for name, was in sorted(locked.items()):
            cur = current.get(name)
            if cur is None:
                if name not in retired:
                    add("missing", name, "a shipped name is no longer served; keep it, or retire it with a date and reason")
                continue
            if name in retired:
                add("retired-served", name, "a retired name is served again; remove its retired row")
            if kind == "capabilities":
                if cur.get("effect") != was.get("effect"):
                    add("effect-changed", name, f"effect {was.get('effect')!r} became {cur.get('effect')!r}")
                continue
            lost = sorted(set(was.get("input", [])) - set(cur["input"]))
            if lost:
                add("input-removed", name, "callers may send " + ", ".join(lost))
            new = sorted(set(cur["required"]) - set(was.get("required", [])))
            if new:
                add("required-added", name, "callers that omit " + ", ".join(new) + " would be refused")
        for name in sorted(set(current) - set(locked)):
            add("unlocked", name, "served but not in the lock: run python tools/check_add_only.py lock")
        if base is not None:
            for name, was in sorted((base.get(kind) or {}).items()):
                row = locked.get(name)
                if row is None:
                    add("lock-rewritten", name, "the base lock held this name; this lock forgot it")
                elif kind == "capabilities" and row.get("effect") != was.get("effect"):
                    add("lock-rewritten", name, "the lock changed the effect the base promised")
                elif kind == "tools" and (set(was.get("input", [])) - set(row.get("input", []))
                                          or set(row.get("required", [])) - set(was.get("required", []))):
                    add("lock-rewritten", name, "the lock narrowed what the base promised")
            for name, row in sorted(((base.get("retired") or {}).get(kind) or {}).items()):
                if ((lock.get("retired") or {}).get(kind) or {}).get(name) != row:
                    add("lock-rewritten", name, "a retired row of the base lock was changed or removed")
    return found


def locked(now: Dict[str, Dict[str, Any]], old: Dict[str, Any]) -> Dict[str, Any]:
    """The lock after this surface ships: every locked row kept as it is, new names and new parameters added."""
    out: Dict[str, Any] = {"schema": SCHEMA, "tools": {}, "capabilities": {},
                           "retired": {"tools": dict(((old.get("retired") or {}).get("tools") or {})),
                                       "capabilities": dict(((old.get("retired") or {}).get("capabilities") or {}))}}
    for name, cur in (now.get("tools") or {}).items():
        was = (old.get("tools") or {}).get(name)
        if was is None:
            out["tools"][name] = cur
        else:   # parameters only grow; a loosened requirement is recorded as loosened
            out["tools"][name] = {"input": sorted(set(was["input"]) | set(cur["input"])),
                                  "required": sorted(set(was["required"]) & set(cur["required"]))}
    for name, was in (old.get("tools") or {}).items():
        out["tools"].setdefault(name, was)
    for name, cur in (now.get("capabilities") or {}).items():
        out["capabilities"][name] = (old.get("capabilities") or {}).get(name, cur)
    for name, was in (old.get("capabilities") or {}).items():
        out["capabilities"].setdefault(name, was)
    out["tools"] = dict(sorted(out["tools"].items()))
    out["capabilities"] = dict(sorted(out["capabilities"].items()))
    return out


def base_lock(ref: str) -> Optional[Dict[str, Any]]:
    """The lock at `ref`, or None (said on stderr) when the ref or the file is not there."""
    try:
        proc = subprocess.run(["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false", "show", f"{ref}:{LOCK_PATH}"],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"add-only: base lock at {ref} NOT read ({exc.__class__.__name__}); lock-rewritten not checked",
              file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"add-only: no lock at {ref}; lock-rewritten not checked", file=sys.stderr)
        return None
    return json.loads(proc.stdout)


def _read_lock() -> Dict[str, Any]:
    return json.loads(LOCK.read_text(encoding="utf-8")) if LOCK.is_file() else {}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=("check", "lock"))
    ap.add_argument("--base-ref", default="origin/main", help="the revision whose lock this one must keep")
    args = ap.parse_args(argv)
    now = served()
    lock = _read_lock()
    if args.action == "lock":
        found = [f for f in check(now, lock) if f["code"] != "unlocked"]
        if found:
            for f in found:
                print(f"{f['code']} {f['name']}: {f['detail']}")
            print("add-only: refusing to lock while check finds the above", file=sys.stderr)
            return 2
        LOCK.write_text(json.dumps(locked(now, lock), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"add-only: locked {len(now['tools'])} tools and {len(now['capabilities'])} capabilities")
        return 0
    if not lock:
        print(f"add-only: {LOCK_PATH} is missing: run python tools/check_add_only.py lock", file=sys.stderr)
        return 2
    found = check(now, lock, base_lock(args.base_ref))
    for f in found:
        print(f"{f['code']} {f['name']}: {f['detail']}")
    if not found:
        print(f"add-only: {len(lock.get('tools') or {})} tools and {len(lock.get('capabilities') or {})} "
              "capabilities keep their promises")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
