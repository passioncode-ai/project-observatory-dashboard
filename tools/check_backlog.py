#!/usr/bin/env python3
"""Lint the engine board, docs/backlog.md, for what stops being true after a merge.

The board is canonical for OBS rows: engine execution tasks, closed by a pull request
and a release. Product packages, priorities and operator gates are on the organization's
board and are cited here as `parent: PB-nnn`; a PB row's status is derived from the OBS rows
it lists, so an OBS status written here is read by another board as fact. That board's check
(org-index `scripts/check_observatory_registers.py`, as merged in org-index #67) reads this file
as an allowlisted format, not as Markdown, and this lint holds the board to the same format:

- one `## Board` heading; from it to the next heading only blank lines, the header exactly
  `| ID | Item | Status | Source |`, the separator `|---|---|---|---|` and contiguous rows
  `| OBS-n | item | status | source |` (four cells; no backslash, so no escaped `|`; no `<`;
  no CR, VT, FF, NBSP, zero-width or other invisible character); the rows end at the first blank
  line, and nothing but blank lines follows them before the next heading; no line outside the
  section holds a `|` and an OBS id; no code fence, HTML comment or HTML block above the board;
- a Status cell of printable ASCII without `& * _ ~` (the dashes of the forms aside) that is,
  whole, exactly one of: `done`, `done in X.Y.Z`, `done in X.Y.Z (#n[, #m…])`,
  `done — merged (#n[, #m…])`, `done — merged (#n[, #m…]), unreleased`, `cancelled`,
  `superseded by OBS-n` (one successor), or `open`/`blocked` with an optional note after
  ` — `, `: `, `; `, `, ` or ` (`. A closed form takes no note: notes go in the Item column;
- at most one `parent:` token in Source, followed by a `PB-n` id.

On top of the format it refuses:

- an id written twice, or at/above the OBS register's `**Next free ID:**` (reserve ids with
  agent-sync's `reserve OBS`; `.claude/agent-sync.json` declares the register), or a board
  with no such line;
- stale-at-merge wording in a status: "in this PR", "to follow", "merging",
  "once … restart(s)";
- `done` together with "remain" (write `open — partly done: …` instead);
- `done in X.Y.Z` naming a release CHANGELOG.md has no heading for;
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
CELL_SPLIT = re.compile(r"(?<!\\)\|")
STALE = (
    (re.compile(r"\bin this PR\b", re.I), "in this PR"),
    (re.compile(r"\bto follow\b", re.I), "to follow"),
    (re.compile(r"\bmerging\b", re.I), "merging"),
    (re.compile(r"\bonce\b[^.;]{0,120}?\brestart", re.I), "once … restarts"),
)

# -- the canonical format (org-index check_observatory_registers.py, #67) -------------------------
BOARD_HEADING = "## Board"
HEADER = "| ID | Item | Status | Source |"
SEPARATOR = re.compile(r"\|(?: ?:?-{3,}:? ?\|){4}")
ROW = re.compile(r"\| (OBS-\d+) \|(.*)\|")
ROW_ID = re.compile(r"^\|\s*(OBS-\d+)\s*\|")
HEADING_LINE = re.compile(r"#{1,6}(?:\s|$)")
#: Any line holding a `|` and naming an OBS id (however wrapped) is a row to that check.
OBS_NAMED = re.compile(r"\bOBS(?:-|&#?[0-9A-Za-z]+;)\d+")
ANY_OBS_ROW = re.compile(r"^(?=.*\|).*" + OBS_NAMED.pattern)
#: HTML blocks of CommonMark types 1–5 (run across blank lines) and 6/7 (a tag on its own line).
HTML_BLOCK_1_5 = re.compile(r" {0,3}(?:<(?:script|pre|style|textarea)(?:[\s>]|$)|<!--|<\?|<![A-Za-z]|<!\[CDATA\[)",
                            re.I)
HTML_BLOCK_6_7 = re.compile(r" {0,3}</?[A-Za-z][A-Za-z0-9-]*(?:[\s/>]|$)")
FENCE_OPEN = re.compile(r" {0,3}(`{3,}|~{3,})")
#: Characters read as line breaks or spaces that a reader does not see.
ODD_CHARS = re.compile("[\r\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x85\xa0؜  -‏ - "
                       " -⁤⁦-⁩　﻿]")
_V = r"\d+\.\d+\.\d+"
_PRS = r"\(#\d+(?:, #\d+)*\)"
_D = r"[—–-]"
#: The whole cell, after runs of spaces collapse. A closed or partial status takes NO note.
EXACT_FORMS = (
    (re.compile(rf"done {_D} merged {_PRS}, unreleased", re.I), "partial"),
    (re.compile(rf"done {_D} merged {_PRS}", re.I), "closed"),
    (re.compile(rf"done in ({_V})(?: {_PRS})?", re.I), "released"),
    (re.compile(r"done", re.I), "closed"),
    (re.compile(r"cancelled", re.I), "closed"),
    (re.compile(r"superseded by OBS-\d+", re.I), "superseded"),
)
OPEN_FORM = re.compile(rf"(?:open|blocked)(?:(?: {_D} |: |; |, | \().+)?", re.I | re.S)
#: Printable ASCII without & * _ ~, plus the two dashes the forms use.
STATUS_CHARS = re.compile(r"[ !-%'-)+-^`-}—–]*")
PARENT_TOKEN = re.compile(r"\bparent:", re.I)
PARENT = re.compile(r"\bparent:\s*PB-\d+(?![\w-])", re.I)
FORMS_HELP = ("`done`, `done in X.Y.Z`, `done in X.Y.Z (#n)`, `done — merged (#n)`, "
              "`done — merged (#n), unreleased`, `cancelled`, `superseded by OBS-n`, or `open`/`blocked` "
              "with an optional note")


def _privacy():
    spec = importlib.util.spec_from_file_location("check_public_release", ROOT / "tools/check_public_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cells(line: str) -> list[str]:
    parts = CELL_SPLIT.split(line.strip())
    return [c.strip() for c in parts[1:-1]] if len(parts) > 2 else []


def parse_status(text: str) -> tuple[str, str | None] | str:
    """(kind, version) for a Status cell, or the reason the grammar refuses it (a str).

    kind is `closed`, `released` (`done in X.Y.Z`, with its version), `partial`, `superseded`
    or `open`. The characters are checked on the raw cell, before spaces collapse."""
    if not STATUS_CHARS.fullmatch(text):
        bad = next(ch for ch in text if not STATUS_CHARS.fullmatch(ch))
        return (f"status holds {bad!r} (U+{ord(bad):04X}); a Status cell is printable ASCII without "
                "& * _ ~ (the dashes of the forms aside); move the note to the Item column")
    s = re.sub(" +", " ", text).strip()
    for pattern, kind in EXACT_FORMS:
        m = pattern.fullmatch(s)
        if m:
            return kind, (m.group(1) if kind == "released" else None)
    if OPEN_FORM.fullmatch(s):
        return "open", None
    if re.match(r"(?:done|cancelled|superseded)\b", s, re.I):
        return (f"status {s[:50]!r} is a closed form with a note; a closed status is exactly one form "
                f"and its note goes to the Item column ({FORMS_HELP})")
    return f"status {s[:50]!r} is outside the vocabulary ({FORMS_HELP})"


def privacy_problems(where: str, text: str, deny: list[str]) -> list[str]:
    hits = _privacy().scan_text(text, deny)
    return [f"{where}: {kind} ×{count} (value not printed)" for kind, count in sorted(hits.items())]


def _line_problem(line: str) -> str | None:
    odd = ODD_CHARS.search(line)
    if odd:
        return f"a control or invisible character U+{ord(odd.group()):04X}"
    if "\\" in line:
        return "a backslash (the organization's reader refuses any; word a `|` instead of escaping it)"
    if "<" in line:
        return "HTML or `<`"
    return None


def _hidden_above(lines: list[str], start: int) -> list[str]:
    """Refusals for a code fence, an HTML comment or an HTML block above the board heading."""
    problems = []
    fence = None
    for n in range(start):
        line = lines[n]
        if fence:
            m = re.match(r" {0,3}(`{3,}|~{3,})\s*$", line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        m = FENCE_OPEN.match(line)
        if m:
            fence = m.group(1)
            problems.append(f"docs/backlog.md line {n + 1}: a code fence above the board; the board is read "
                            "as text, and a fence or HTML block there hides or reorders what GitHub shows")
            continue
        if HTML_BLOCK_1_5.match(line) or HTML_BLOCK_6_7.match(line) or "<!--" in line:
            problems.append(f"docs/backlog.md line {n + 1}: HTML above the board (a comment or an HTML block)")
    return problems


def check_board(text: str, changelog: str, deny: list[str] | None = None) -> list[str]:
    deny = deny or []
    problems: list[str] = []
    headings = set(re.findall(r"^##\s+(\d+\.\d+\.\d+)\b", changelog, re.M))
    m = NEXT_FREE.search(text)
    next_free = int(m.group(1)) if m else None
    if next_free is None:
        problems.append("docs/backlog.md: no `**Next free ID:** `OBS-n`` line, so the OBS register "
                        "(.claude/agent-sync.json idRegisters.OBS) cannot reserve an id")
    lines = text.split("\n")
    board_at = [n for n, line in enumerate(lines) if line.rstrip() == BOARD_HEADING]
    if len(board_at) != 1:
        where = ("no `## Board` heading" if not board_at else
                 "`## Board` on lines " + ", ".join(str(n + 1) for n in board_at))
        problems.append(f"docs/backlog.md: {where}; the board is exactly one `## Board` section, its table "
                        "directly under the heading")
    if board_at:
        start = board_at[0]
        problems += _hidden_above(lines, start)
    else:
        # Still read the rows of a board that lacks its heading (an older board, 9a3778f): from the line
        # above its first table header, so every row-level refusal below is reported as well.
        start = next((n - 1 for n, line in enumerate(lines) if line.startswith("| ID |")), len(lines))
    end = next((n for n in range(start + 1, len(lines)) if HEADING_LINE.match(lines[n])), len(lines))
    found_table = start < len(lines)

    def refuse(n: int, why: str, rid: str | None = None) -> None:
        problems.append(f"{rid}: {why} (line {n + 1})" if rid else f"docs/backlog.md line {n + 1}: {why}")

    seen: dict[str, int] = {}
    phase = "before"
    rows = 0
    for n in range(start + 1, end):
        line = lines[n]
        blank = not line.strip()
        if blank and phase in ("before", "after"):
            continue
        if blank and phase == "rows":
            phase = "after"
            continue
        if phase == "after":
            rid = ROW_ID.match(line)
            refuse(n, "after the board's table (its rows end at the first blank line)", rid and rid.group(1))
            continue
        problem = _line_problem(line)
        if phase == "before":
            if problem or line != HEADER:
                refuse(n, problem or f"expected the header `{HEADER}` directly under `{BOARD_HEADING}`")
                if not line.lstrip().startswith("|"):
                    continue
            phase = "separator"
            continue
        if phase == "separator":
            if problem or not SEPARATOR.fullmatch(line):
                refuse(n, problem or "expected the separator `|---|---|---|---|` (four cells)")
            phase = "rows"
            continue
        rid_match = ROW_ID.match(line)
        rid = rid_match.group(1) if rid_match else None
        if problem:
            refuse(n, problem, rid)
            continue
        canonical = ROW.fullmatch(line)
        if not canonical:
            refuse(n, "not a canonical row `| OBS-n | item | status | source |` (single spaces around the id, "
                   "a pipe at each end, nothing after the last)", rid)
            continue
        rid = canonical.group(1)
        parts = [c.strip() for c in canonical.group(2).split("|")]
        if len(parts) != 3:
            refuse(n, f"{len(parts) + 1} cells where the header has 4 (a `|` inside a cell splits it; "
                   "word it instead)", rid)
            continue
        rows += 1
        _item, status, source = parts
        if rid in seen:
            problems.append(f"{rid}: written twice (lines {seen[rid]} and {n + 1}); reserve a new id")
        seen.setdefault(rid, n + 1)
        if next_free is not None and int(rid.split("-")[1]) >= next_free:
            problems.append(f"{rid}: at or above the register's next free id OBS-{next_free}; "
                            "reserve it (agent-sync `reserve OBS`) and move the line")
        for kind, count in _privacy().scan_text(line, deny).items():
            problems.append(f"{rid}: {kind} ×{count} (value not printed)")
        tokens = len(PARENT_TOKEN.findall(source))
        if tokens > 1:
            problems.append(f"{rid}: {tokens} `parent:` tokens in Source; a row has exactly one parent package")
        elif tokens == 1 and not PARENT.search(source):
            problems.append(f"{rid}: `parent:` in Source is not followed by a PB-n id")
        for pattern, label in STALE:
            hit = pattern.search(status)
            if hit:
                said = status[max(0, hit.start() - 24):hit.end()].split(" ", 1)[-1] if hit.start() else hit.group(0)
                problems.append(f"{rid}: status says {said!r} ({label}), which is false once merged; write "
                                "what is true on main (`done — merged (#n), unreleased`, `done in X.Y.Z (#n)`)")
        if re.match(r"done\b", status, re.I) and re.search(r"\bremains?\b", status, re.I):
            problems.append(f"{rid}: `done` with something that remains; write `open — partly done: …`")
        parsed = parse_status(status)
        if isinstance(parsed, str):
            problems.append(f"{rid}: {parsed}")
            continue
        kind, version = parsed
        if kind == "released" and version not in headings:
            problems.append(f"{rid}: `done in {version}` but CHANGELOG.md has no `## {version}` heading")
    if found_table and phase in ("before", "separator"):
        problems.append(f"docs/backlog.md: the board section (line {start + 1}) has no header and separator")
    elif found_table and not rows and not any(p.startswith(("OBS-", "docs/backlog.md line")) for p in problems):
        problems.append(f"docs/backlog.md: the board section (line {start + 1}) has no OBS rows")
    for n, line in enumerate(lines):
        if found_table and start <= n < end:
            continue
        after_table = n > 0 and "|" in lines[n - 1] and "|" not in line and OBS_NAMED.search(line)
        if ANY_OBS_ROW.search(line) or after_table:
            refuse(n, f"an OBS row outside the board (`{BOARD_HEADING}`"
                      + (f", lines {start + 1}–{end})" if board_at else ")"))
    problems += [p for p in privacy_problems("CHANGELOG.md", changelog, deny)]
    # The board's prose outside the rows (header, notes) is public too.
    prose = "\n".join(l for l in lines if not ROW_ID.match(l))
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
