#!/usr/bin/env python3
"""`project-observatory full copies list|plan COPY`: the recorded copies, and how to carry one forward.

Reads `registry/derivations.json` (OBS-321, `derivations.py`). PRINTS ONLY: Observatory never
syncs, builds, applies or pushes anything to a repository. The plan is the procedure a
person or a coding session runs, in the copy's own clones:

1. build the source at `sync_base` and at the source branch's head with the copy's own
   builder (the same rules file, checked by its sha256), so a raw source diff, which holds
   what the builder exists to remove, never reaches the copy;
2. diff the two builds, minus the copy's deny paths;
3. apply the patch with `git apply --3way` on a branch `sync/<head12>` of the copy;
4. run the copy's own audits and tests, open a reviewed pull request, integrate it;
5. record the new `sync_base` in `config/derivations.json` and the copy's `.copy/upstream.json`.

A move is never carried: its plan says so.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shlex
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _doc() -> tuple[dict | None, list[dict]]:
    import derivations
    import paths
    return derivations.load(paths.REGISTRY, paths.config_file("derivations.json"))


def _branch_arg(branch) -> str:
    """`origin/<branch>` for a shell line, quoted; a placeholder when the measured name is not a
    plain branch name. The name comes from a repository's measured default branch, and a value
    printed into a command a person pastes must not be able to run anything."""
    import derivations
    if isinstance(branch, str) and derivations._BRANCH.match(branch) and ".." not in branch:
        return shlex.quote(f"origin/{branch}")
    return "origin/<default branch>"


def plan(rel: dict) -> dict:
    """The steps for one relation, as data: `steps` (text) and `commands` (shell lines)."""
    copy, source = rel["copy"], rel["source"]
    if rel.get("kind") == "move":
        return {"copy": copy, "source": source, "kind": "move", "steps": [
            f"{copy} is a move of {source}: nothing is carried after a move, either way.",
            f"Archive {source} at its host once nothing pushes to it, and point remaining clones at {copy}."],
            "commands": []}
    behind = rel.get("behind") or {}
    since = (rel.get("sync_base") or rel.get("copy_point") or {}).get("source_commit") or "<sync_base>"
    head = behind.get("head") or "<source head>"
    ref = behind.get("ref") or f"origin/{rel.get('source_branch') or '<branch>'}"
    builder = rel.get("builder") or {}
    hint = builder.get("path_hint") or "<the copy's builder>"
    deny = rel.get("deny") or []
    q = shlex.quote
    exclude = " ".join(q(f":(exclude){p}") for p in deny)
    steps = []
    if behind.get("state") != "measured":
        steps.append(f"How far {source} has moved on is not measured here ({behind.get('reason')}); fetch the "
                     f"source's clone and run `project-observatory full emit` first, or fill in the head yourself.")
    elif not behind.get("commits"):
        steps.append(f"{copy} is level with {ref} at {head}: there is nothing to carry.")
    steps += [
        f"Build the source at {since} and at {ref} ({head}) with {hint}"
        + (f", {builder['rules_count']} rules" if builder.get("rules_count") is not None else "")
        + (f", rules sha256 {builder['rules_sha256']}" if builder.get("rules_sha256") else "")
        + "; check the rules file is unchanged before each build. Never diff the source itself: its raw diff "
          "carries what the builder removes.",
        "Diff the two builds" + (f", leaving out the copy's deny paths ({', '.join(deny)})" if deny else
                                 " (the copy lists no deny paths in .copy/upstream.json)") + ".",
        f"Apply the patch on a new branch sync/{head[:12]} of the copy with `git apply --3way`; a conflict can "
        "only be a file a copy-only commit changed by hand.",
        "Run the copy's own audits (secret scan, its tests) on the branch, open a reviewed pull request "
        f"titled \"sync: source at {head[:12]}\", and integrate it the way the copy's history does.",
        f"Record the new sync_base {head[:12]} in config/derivations.json and in the copy's "
        ".copy/upstream.json, then run `project-observatory full emit`.",
        "A fix made in the copy goes back to the source by hand, as a reviewed pull request there; never "
        "push from the copy to the source.",
    ]
    commands = [
        "BASE=$(mktemp -d) NEW=$(mktemp -d)",
        f"# build {since} into $BASE and {head} into $NEW with {hint}",
        'git -C "$BASE" init -q && git -C "$BASE" add -A && git -C "$BASE" commit -qm base',
        'rsync -a --delete --exclude .git "$NEW"/ "$BASE"/',
        'git -C "$BASE" add -A && git -C "$BASE" diff --cached --binary HEAD -- .'
        + (f" {exclude}" if exclude else "") + " > sync.patch",
        f"git -C <copy clone> switch -c sync/{head[:12]} {_branch_arg(rel.get('copy_branch'))}",
        "git -C <copy clone> apply --3way \"$PWD/sync.patch\"",
    ]
    return {"copy": copy, "source": source, "kind": "copy", "since": since, "head": head, "ref": ref,
            "behind": behind.get("commits"), "steps": steps, "commands": commands}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="project-observatory full copies",
                                description="The recorded copies and moves (config/derivations.json), and the steps "
                                            "to carry a copy forward. Prints only; nothing is synced.")
    sub = p.add_subparsers(dest="command", required=True)
    ls = sub.add_parser("list", help="every recorded relation, with how far each copy is behind")
    ls.add_argument("--json", action="store_true")
    pl = sub.add_parser("plan", help="print the sync steps for one copy (nothing is run)")
    pl.add_argument("copy", help="the copy's repository id, e.g. repository:example-org/alpha-agent")
    pl.add_argument("--json", action="store_true")
    return p


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    doc, degraded = _doc()
    if doc is None:
        reason = degraded[0]["reason"] if degraded else ("no relation is recorded: add one to "
                                                          "config/derivations.json, then run "
                                                          "`project-observatory full emit`")
        print(f"copies: {reason}", file=sys.stderr)
        return 2
    relations = [r for r in doc.get("relations") or [] if isinstance(r, dict)]
    if args.command == "list":
        rows = [{"copy": r["copy"], "source": r["source"], "kind": r["kind"],
                 "behind": (r.get("behind") or {}).get("commits"),
                 "behind_state": (r.get("behind") or {}).get("state")} for r in relations]
        if args.json:
            print(json.dumps({"relations": rows, "refused": doc.get("refused") or [],
                              "unrecorded": doc.get("unrecorded") or [], "degraded": doc.get("degraded") or []},
                             indent=2))
            return 0
        for r in rows:
            state = ("move" if r["kind"] == "move" else
                     f"{r['behind']} behind" if r["behind_state"] == "measured" else "behind: not measured")
            print(f"{r['copy']}  <-  {r['source']}  ({state})")
        for r in doc.get("refused") or []:
            print(f"refused: {r.get('reason')}")
        for r in doc.get("unrecorded") or []:
            print(f"unrecorded: {r.get('reason')}")
        if not rows and not doc.get("refused") and not doc.get("unrecorded"):
            print("no copy or move is recorded")
        return 0
    rel = next((r for r in relations if r["copy"] == args.copy), None)
    if rel is None:
        refused = [r for r in doc.get("refused") or [] if r.get("copy") == args.copy]
        why = refused[0]["reason"] if refused else "no accepted entry in config/derivations.json names it as a copy"
        print(f"copies plan: {args.copy}: {why}", file=sys.stderr)
        return 2
    out = plan(rel)
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"Carrying {out['source']} into {out['copy']} ({out['kind']}). Nothing below is run by Observatory.")
    for i, step in enumerate(out["steps"], 1):
        print(f"{i}. {step}")
    if out["commands"]:
        print("\nCommands, for the person or session that runs them:")
        for line in out["commands"]:
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
