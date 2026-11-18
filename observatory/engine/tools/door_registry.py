#!/usr/bin/env python3
"""Which providers this engine covers, as the doors and collectors declare themselves (OBS-605, CP-13).

WHY. Integration health called a provider `untracked` when it was missing from a list kept by hand beside it
(`TRACKED`), apart from the doors: a provider could count as tracked with no door, and a new door was tracked only
once someone remembered to edit the list. Now each door and each collector says what it covers, in its own file,
and this module reads those declarations; nothing else lists providers.

THE DECLARATIONS.

  door       a module under `tools/` with a top-level `DOOR_PROVIDER = "<name>"`: the one door to that provider's
             keys (issue, rotate, revoke)
  collector  a module under `collectors/` with a top-level `COVERS_PROVIDERS = ("<name>", ...)`: it reads that
             provider's answers into the registry

They are read from the source text with `ast` and `ast.literal_eval`, never by importing the module: listing the
providers must not run a door. A declaration that is not a literal, or names no provider, is named in `degraded`
with a code and contributes nothing.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
COLLECTORS = ROOT / "collectors"

#: A provider name: lower case, as the integration tables name it.
_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


def _good(name) -> bool:
    return isinstance(name, str) and bool(name) and set(name) <= _NAME_CHARS


def _assigned(tree: ast.Module, name: str):
    """The node assigned to `name` at the top level of a module, or None."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return node.value
    return None


def _read(path: pathlib.Path, kind: str) -> tuple[list[dict], list[dict]]:
    label = "DOOR_PROVIDER" if kind == "door" else "COVERS_PROVIDERS"
    try:
        text = path.read_text(encoding="utf-8")
        # Only a module that names the declaration is parsed: most modules here declare nothing.
        tree = ast.parse(text, filename=str(path)) if label in text else None
    except (OSError, SyntaxError, ValueError, UnicodeError) as exc:
        return [], [{"source": f"door-registry:{path.name}", "code": "module-unreadable",
                     "reason": f"{path.name} could not be read ({type(exc).__name__})"}]
    node = _assigned(tree, label) if tree is not None else None
    if node is None:
        return [], []
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        value = None
    names = ([value] if kind == "door" and isinstance(value, str)
             else list(value) if kind == "collector" and isinstance(value, (tuple, list)) else [])
    good = [n for n in names if _good(n)]
    if not good or len(good) != len(names):
        return [], [{"source": f"door-registry:{path.name}", "code": "declaration-unshaped",
                     "reason": f"{path.name} declares {label} in no known shape"}]
    return [{"provider": n, "kind": kind, "module": path.stem} for n in good], []


def declarations(tools: pathlib.Path | None = None, collectors: pathlib.Path | None = None
                 ) -> tuple[list[dict], list[dict]]:
    """(every declaration found, what could not be read). A declaration is {provider, kind, module}."""
    out: list[dict] = []
    degraded: list[dict] = []
    for folder, kind in ((tools or TOOLS, "door"), (collectors or COLLECTORS, "collector")):
        try:
            files = sorted(pathlib.Path(folder).glob("*.py"))
        except OSError as exc:
            degraded.append({"source": f"door-registry:{pathlib.Path(folder).name}", "code": "folder-unreadable",
                             "reason": f"the {kind} folder could not be listed ({type(exc).__name__})"})
            continue
        for path in files:
            found, bad = _read(path, kind)
            out += found
            degraded += bad
    out.sort(key=lambda d: (d["provider"], d["kind"], d["module"]))
    return out, degraded


def providers(tools: pathlib.Path | None = None, collectors: pathlib.Path | None = None) -> frozenset:
    """Every provider a door or a collector here covers."""
    return frozenset(d["provider"] for d in declarations(tools, collectors)[0])


def doors(tools: pathlib.Path | None = None, collectors: pathlib.Path | None = None) -> dict[str, str]:
    """provider -> the door module that declares it."""
    found, _ = declarations(tools, collectors)
    return {d["provider"]: d["module"] for d in found if d["kind"] == "door"}
