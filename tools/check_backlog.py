#!/usr/bin/env python3
"""Lint the engine board, docs/backlog.md, for what stops being true after a merge.

The board is canonical for OBS rows: engine execution tasks, closed by a pull request
and a release. Product packages, priorities and operator gates are on the organization's
board and are cited here as `PB-nnn`; a PB row's status is derived from the OBS rows it
lists, so an OBS status written here is read by another board as fact. This lint refuses:

- a row whose cell count differs from the header (a raw `|` inside a code span splits a
  GFM cell);
- an id written twice, or at/above the OBS register's `**Next free ID:**` (reserve ids with
  agent-sync's `reserve OBS`; `.claude/agent-sync.json` declares the register), or a board
  with no such line;
- a status that does not start with `open`, `blocked`, `done`, `closed` or
  `superseded by OBS-n`;
- stale-at-merge wording in a status: "in this PR", "to follow", "merging",
  "once … restart(s)";
- `done` together with "remain" (write `open — partly done: …` instead);
- `done in X.Y.Z` (or a range X.Y.Z–A.B.C) naming a release CHANGELOG.md has no heading for;
- in the board and CHANGELOG.md: token shapes, private-key blocks, personal machine paths
  and credential URLs always, and private names when a LOCAL denylist is given
  (`--private-denylist` or OBSERVATORY_PRIVATE_DENYLIST, the same JSON array
  `tools/check_public_release.py` reads, minus `tools/public-identifiers.json`). Matched
  values are never printed: only the row id, the category and the count.

Exit 0 clean, 1 with problems (one per line on stderr), 2 on unusable input.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "docs/backlog.md"
CHANGELOG = ROOT / "CHANGELOG.md"

#: The OBS register's next-free line; `.claude/agent-sync.json` idRegisters.OBS.pattern is
#: this exact expression, so agent-sync reserves from the line this lint reads.
NEXT_FREE = re.compile(r"\*\*Next free ID:\*\* `OBS-(\d+)`")
ROW = re.compile(r"^\|\s*(OBS-\d+)\s*\|")
CELL_SPLIT = re.compile(r"(?<!\\)\|")
STATUS_START = re.compile(r"^(?:(?:open|blocked|done|closed)\b|superseded by OBS-\d+)")
STALE = (
    (re.compile(r"\bin this PR\b", re.I), "in this PR"),
    (re.compile(r"\bto follow\b", re.I), "to follow"),
    (re.compile(r"\bmerging\b", re.I), "merging"),
    (re.compile(r"\bonce\b[^.;]{0,120}?\brestart", re.I), "once … restarts"),
)
VERSION = re.compile(r"\d+\.\d+\.\d+")


def _privacy():
    spec = importlib.util.spec_from_file_location("check_public_release", ROOT / "tools/check_public_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cells(line: str) -> list[str]:
    parts = CELL_SPLIT.split(line.strip())
    return [c.strip() for c in parts[1:-1]] if len(parts) > 2 else []


def released(status: str) -> list[str]:
    """Versions a `done in …` status claims, from the clause before its first ':' or ' — '."""
    head = status[len("done in "):]
    head = re.split(r":| — ", head, maxsplit=1)[0]
    head = re.sub(r"\([^)]*\)", "", head)
    return VERSION.findall(head)


def privacy_problems(where: str, text: str, deny: list[str]) -> list[str]:
    hits = _privacy().scan_text(text, deny)
    return [f"{where}: {kind} ×{count} (value not printed)" for kind, count in sorted(hits.items())]


def check_board(text: str, changelog: str, deny: list[str] | None = None) -> list[str]:
    deny = deny or []
    problems: list[str] = []
    headings = set(re.findall(r"^##\s+(\d+\.\d+\.\d+)\b", changelog, re.M))
    m = NEXT_FREE.search(text)
    next_free = int(m.group(1)) if m else None
    if next_free is None:
        problems.append("docs/backlog.md: no `**Next free ID:** `OBS-n`` line, so the OBS register "
                        "(.claude/agent-sync.json idRegisters.OBS) cannot reserve an id")
    width = None
    seen: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if width is None and line.startswith("| ID |"):
            width = len(cells(line))
            continue
        row = ROW.match(line)
        if not row:
            continue
        rid = row.group(1)
        parts = cells(line)
        if width is not None and len(parts) != width:
            problems.append(f"{rid}: {len(parts)} cells where the header has {width} (line {number}; "
                            "escape a `|` inside a cell as `\\|`)")
        if rid in seen:
            problems.append(f"{rid}: written twice (lines {seen[rid]} and {number}); reserve a new id")
        seen.setdefault(rid, number)
        if next_free is not None and int(rid.split("-")[1]) >= next_free:
            problems.append(f"{rid}: at or above the register's next free id OBS-{next_free}; "
                            "reserve it (agent-sync `reserve OBS`) and move the line")
        status = parts[2] if len(parts) > 2 else ""
        for kind, count in _privacy().scan_text(line, deny).items():
            problems.append(f"{rid}: {kind} ×{count} (value not printed)")
        if not STATUS_START.match(status):
            problems.append(f"{rid}: status {status[:40]!r} is outside the vocabulary "
                            "(open…, blocked…, done…, closed…, superseded by OBS-n…)")
            continue
        for pattern, label in STALE:
            hit = pattern.search(status)
            if hit:
                said = status[max(0, hit.start() - 24):hit.end()].split(" ", 1)[-1] if hit.start() else hit.group(0)
                problems.append(f"{rid}: status says {said!r} ({label}), which is false once merged; write "
                                "what is true on main (`done — merged (#n), unreleased`, `done in X.Y.Z (#n)`)")
        if status.startswith("done") and re.search(r"\bremains?\b", status, re.I):
            problems.append(f"{rid}: `done` with something that remains; write `open — partly done: …`")
        if status.startswith("done in "):
            for v in released(status):
                if v not in headings:
                    problems.append(f"{rid}: `done in {v}` but CHANGELOG.md has no `## {v}` heading")
    problems += [p for p in privacy_problems("CHANGELOG.md", changelog, deny)]
    # The board's prose outside the rows (header, notes) is public too.
    prose = "\n".join(l for l in text.splitlines() if not ROW.match(l))
    problems += privacy_problems("docs/backlog.md (outside the rows)", prose, deny)
    return problems


def load_denylist(path: Path | None) -> tuple[list[str], str]:
    if path is None:
        env = os.environ.get("OBSERVATORY_PRIVATE_DENYLIST")
        path = Path(env) if env else None
    if path is None:
        return [], "no local denylist: private names were NOT checked (shape patterns only)"
    deny = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(deny, list) or any(not isinstance(x, str) for x in deny):
        raise ValueError("the private denylist must be a JSON array of strings")
    public = _privacy().public_identifiers()
    kept = [x for x in deny if x.strip().lower() not in public]
    return kept, f"private denylist: {len(kept)} entries ({len(deny) - len(kept)} published on purpose)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--board", type=Path, default=BOARD)
    parser.add_argument("--changelog", type=Path, default=CHANGELOG)
    parser.add_argument("--private-denylist", type=Path)
    args = parser.parse_args(argv)
    try:
        text = args.board.read_text(encoding="utf-8")
        changelog = args.changelog.read_text(encoding="utf-8")
        deny, note = load_denylist(args.private_denylist)
    except (OSError, ValueError) as exc:
        print(f"check_backlog: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    problems = check_board(text, changelog, deny)
    for problem in problems:
        print(problem, file=sys.stderr)
    print(json.dumps({"board": args.board.name, "problems": len(problems), "privacy": note}))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
