#!/usr/bin/env python3
"""Lint the engine board, docs/backlog.md, for what stops being true after a merge.

The board is canonical for OBS rows: engine execution tasks, closed by a pull request
and a release. Product packages, priorities and operator gates are on the organization's
board and are cited here as `parent: PB-nnn`; a PB row's status is derived from the OBS rows
it lists, so an OBS status written here is read by another board as fact. That board's check
(org-index `scripts/check_observatory_registers.py`, as merged in org-index #67) reads this file
as an allowlisted format, not as Markdown, and this lint holds the board to that format. It
reads the raw bytes as that check does, and is stricter in the places marked (stricter):

- no CR anywhere in the document (a CRLF file included);
- one `## Board` heading, not hidden by a code fence, an HTML comment or an HTML block left open
  above it (any line starting with up to three spaces and `<` opens a block until the next blank
  line, so `<3 text` directly above the heading hides it). Above the heading, a line that starts
  an HTML block of CommonMark types 1–7 (`<div>`, `<script`, `<!-- `, …) is refused as the
  check refuses it, and (stricter) so is any code fence, even closed, and any `<!--`, even
  mid-line and closed. Inline tags mid-line, and a `<`-line that is no tag (`<3`) closed by a
  blank line, pass. From the heading to the next heading only blank lines, the header exactly
  `| ID | Item | Status | Source |`, the separator `|---|---|---|---|` and contiguous rows
  `| OBS-n | item | status | source |` (four cells; no backslash, so no escaped `|`; no `<`; no
  character of Unicode category Cc, Cf, Zl or Zp and no space but U+0020 (stricter: the check
  refuses a fixed list of these), and (stricter) no Hangul filler U+115F/U+1160/U+3164/U+FFA0,
  no U+034F, no Khmer inherent vowel, no Mongolian or Unicode variation selector, and no
  unassigned default-ignorable code point (U+2065, U+FFF0–FFF8, U+E0000, …)); the rows end at
  the first blank line, and nothing but blank lines follows them before the next heading; no
  line outside the section holds a `|` and an OBS id, and no line right under a table line
  names one;
- an OBS id of ASCII digits, `OBS-1` or the two-digit `OBS-01` the first rows use (stricter:
  `OBS-001` and `OBS-0` are refused, and `OBS-01` beside `OBS-1` is one id written twice; the
  check reads `\\d` and compares strings);
- (stricter) `superseded by OBS-n` names an existing row, spelled as that row spells it (the
  check reads an unknown successor as open and refuses nothing);
- a Status cell of printable ASCII without `& * _ ~` (the dashes of the forms aside) that is,
  whole, exactly one of: `done`, `done in X.Y.Z`, `done in X.Y.Z (#n[, #m…])`,
  `done — merged (#n[, #m…])`, `done — merged (#n[, #m…]), unreleased`, `cancelled`,
  `superseded by OBS-n` (one successor), or `open`/`blocked` with an optional note after
  ` — `, `: `, `; `, `, ` or ` (`. A closed form takes no note: notes go in the Item column;
- at most one `parent:` token in Source, followed by a `PB-n` id of ASCII digits (stricter: the
  check reads `\\d`, which admits other scripts' digits).

On top of the format it refuses:

- an id written twice, or at/above the OBS register's `**Next free ID:**` (reserve ids with
  agent-sync's `reserve OBS`; `.claude/agent-sync.json` declares the register), or a board
  with no such line;
- stale-at-merge wording in a status: "in this PR", "to follow", "merging",
  "once … restart(s)", "awaiting review", "ready", "closes when … merges", "lands with";
- an id below the next free one with no row and no declaration on the board's
  `**Vacant ids:**` line, written `OBS-n` (reason); a declaration whose row exists;
- an open or blocked row whose id a merged commit on the checked branch delivers: a subject ending
  in `(#n)` that names the id in a parenthesised list, `(OBS-n)` or `(OBS-n, DEC-m)` (a commit
  that only files a row writes it bare or with a word, `(OBS-n filed)`). The one exception is a row
  whose status says `partly done` (`open — partly done: …`). The history is read only for this
  repository's own board, and a shallow clone or a missing Git is named on stderr and in the JSON
  (`history`), never read as clean; `--require-history` makes it a failure. In a pull_request run
  check.yml passes the pull request's own title as `TITLE (#N)` in CHECK_BACKLOG_PR_SUBJECT (and, for a
  pull request of one commit, which the repository squashes under that commit's headline, its number and
  commit count, so the headline is checked too), so a
  title that would deliver a row still open fails the pull request, not main after it. A revert
  (`Revert "…"`) delivers nothing, and a row whose status says `reopened` is exempt like `partly done`;
- (vacancies) a gap whose reservation is younger than GAP_GRACE_DAYS (14) is a pull request in flight
  and needs no declaration; older, it is an abandoned reservation and must be declared. The ages come
  from the history of the `Next free ID` line; without the history the gaps are not checked, and
  the note says so;
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
import subprocess
import sys
import unicodedata

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
    # A pull request's state on the day it was written (OBS-110 "awaiting review", OBS-133 "PR #297
    # ready", OBS-118 "closes when PR #297 merges"): all three stayed on the board after the merge.
    (re.compile(r"\bawaiting (?:review|merge|a review)\b", re.I), "awaiting review"),
    (re.compile(r"(?<!\bnot )\bready\b(?!-)", re.I), "ready"),
    (re.compile(r"\bcloses? when\b[^.;]{0,120}?\bmerge", re.I), "closes when … merges"),
    # OBS-80, OBS-84…87, OBS-89: "lands with the 0.25.0 integration pull request (#303)", a week after it did.
    (re.compile(r"\b(?:lands?|landing) with\b", re.I), "lands with"),
)
#: The one phrase that keeps an open row open after a merged commit names it: the row says what
#: shipped and what remains (`open — partly done: …`, the form AGENTS.md prescribes for a
#: remainder). Any other open or blocked row whose id a merged commit carries is stale.
PARTLY_DONE = re.compile(r"\bpartly done\b", re.I)
#: A row a merged commit delivered and that is open again on purpose (its change reverted, or a defect found
#: after the merge) says `reopened` in its status: `open — reopened: …`.
REOPENED = re.compile(r"\breopened\b", re.I)
#: In a pull_request run, check.yml hands the pull request's title and number here as one subject,
#: `TITLE (#N)`: the squash commit main will carry. Without it a title naming `(OBS-n)` for a row still
#: open passes its own pull request and turns main red after the merge (OBS-63 was named by eight).
PENDING_SUBJECT_ENV = "CHECK_BACKLOG_PR_SUBJECT"
#: The repository squashes with COMMIT_OR_PR_TITLE: a pull request of ONE commit lands under that commit's
#: headline, not its title. check.yml passes the number and the commit count; with one commit the
#: headline of the pull request's head (`HEAD^2` of the merge checkout) is checked as `HEADLINE (#N)` too.
PR_NUMBER_ENV = "CHECK_BACKLOG_PR_NUMBER"
PR_COMMITS_ENV = "CHECK_BACKLOG_PR_COMMITS"
#: A gap below the next free id whose reservation is younger than this is taken as a pull request still
#: in flight; older than this, it must be a row or a declared vacancy (an abandoned reservation).
GAP_GRACE_DAYS = 14
#: A merged commit on the checked branch, as squash merges write it: the subject ends with the
#: pull request, `(#n)`, and names the row it delivers in a parenthesised list, `(OBS-n)` or
#: `(OBS-n, DEC-m)`. A row FILED by a commit is written bare (`docs(backlog): OBS-96..99`) or with
#: a word (`(OBS-118 filed)`), and is not a delivery.
MERGED_PR = re.compile(r"\(#(\d+)\)\s*$")
PAREN_GROUP = re.compile(r"\(([^()]*)\)")
DELIVERED_ID = re.compile(r"OBS-0*([1-9][0-9]*)")
#: Ids the board accounts for without a row: `**Vacant ids:** `OBS-n` (why); `OBS-m` (why)`.
VACANT_LINE = re.compile(r"^\*\*Vacant ids:\*\*(.*)$", re.M)
VACANT_ENTRY = re.compile(r"`(OBS-(?:0[1-9]|[1-9][0-9]*))` \(([^()]*)\)")

# -- the canonical format (org-index check_observatory_registers.py, #67) -------------------------
BOARD_HEADING = "## Board"
HEADER = "| ID | Item | Status | Source |"
SEPARATOR = re.compile(r"\|(?: ?:?-{3,}:? ?\|){4}")
#: An OBS id: ASCII digits only (`\d` would admit `OBS-١`), and no padding beyond the two-digit
#: form the first rows were written in (OBS-01…OBS-09 are stable ids); `OBS-001` and `OBS-0` are
#: refused, and `OBS-01` beside `OBS-1` is one number written twice (see `check_board`).
OBS_ID = r"OBS-(?:0[1-9]|[1-9][0-9]*)"
#: A PB id, as org-index writes it (zero-padded, `PB-012`): ASCII digits only.
PB_ID = r"PB-[0-9]+"
ROW = re.compile(rf"\| ({OBS_ID}) \|(.*)\|")
ROW_ID = re.compile(r"^\|\s*(OBS-\S*?)\s*\|")
HEADING_LINE = re.compile(r"#{1,6}(?:\s|$)")
#: Any line holding a `|` and naming an OBS id (however wrapped) is a row to that check.
OBS_NAMED = re.compile(r"\bOBS(?:-|&#?[0-9A-Za-z]+;)\d+")
ANY_OBS_ROW = re.compile(r"^(?=.*\|).*" + OBS_NAMED.pattern)
#: HTML blocks of CommonMark types 1–5 (run across blank lines) and 6/7 (a tag on its own line).
HTML_BLOCK_1_5 = re.compile(r" {0,3}(?:<(?:script|pre|style|textarea)(?:[\s>]|$)|<!--|<\?|<![A-Za-z]|<!\[CDATA\[)",
                            re.I)
HTML_BLOCK_6_7 = re.compile(r" {0,3}</?[A-Za-z][A-Za-z0-9-]*(?:[\s/>]|$)")
FENCE_OPEN = re.compile(r" {0,3}(`{3,}|~{3,})")
#: Characters a reader does not see that are not in a Unicode category `invisible` refuses:
#: the Hangul fillers (letters, Lo) and the default-ignorable combining marks (Mn) — the combining
#: grapheme joiner, the Khmer inherent vowels, the Mongolian and Unicode variation selectors.
INVISIBLE_EXTRA = frozenset(
    [0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180C, 0x180D, 0x180F, 0x3164, 0xFFA0]
    + list(range(0xFE00, 0xFE10)) + list(range(0xE0100, 0xE01F0)))
#: The ranges of Unicode's Default_Ignorable_Code_Point (DerivedCoreProperties.txt). A code point in
#: them that is still unassigned (category Cn: U+2065, U+FFF0–FFF8, U+E0000, U+E0002–E001F, …)
#: renders as nothing once a later Unicode assigns it, so it is refused now; the assigned ones are
#: Cf, or listed in INVISIBLE_EXTRA.
DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
    (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)


def _default_ignorable(cp: int) -> bool:
    return any(low <= cp <= high for low, high in DEFAULT_IGNORABLE)
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
    (re.compile(rf"superseded by ({OBS_ID})", re.I), "superseded"),
)
OPEN_FORM = re.compile(rf"(?:open|blocked)(?:(?: {_D} |: |; |, | \().+)?", re.I | re.S)
#: Printable ASCII without & * _ ~, plus the two dashes the forms use.
STATUS_CHARS = re.compile(r"[ !-%'-)+-^`-}—–]*")
PARENT_TOKEN = re.compile(r"\bparent:", re.I)
PARENT = re.compile(rf"\bparent:\s*{PB_ID}(?![\w-])", re.I)
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
    """(kind, detail) for a Status cell, or the reason the grammar refuses it (a str).

    kind is `closed`, `released` (detail: the version of `done in X.Y.Z`), `partial`,
    `superseded` (detail: the successor id as written) or `open`. The characters are checked on
    the raw cell, before spaces collapse."""
    if not STATUS_CHARS.fullmatch(text):
        bad = next(ch for ch in text if not STATUS_CHARS.fullmatch(ch))
        return (f"status holds {bad!r} (U+{ord(bad):04X}); a Status cell is printable ASCII without "
                "& * _ ~ (the dashes of the forms aside); move the note to the Item column")
    s = re.sub(" +", " ", text).strip()
    for pattern, kind in EXACT_FORMS:
        m = pattern.fullmatch(s)
        if m:
            return kind, (m.group(1) if kind in ("released", "superseded") else None)
    if OPEN_FORM.fullmatch(s):
        return "open", None
    if re.match(r"(?:done|cancelled|superseded)\b", s, re.I):
        return (f"status {s[:50]!r} is a closed form with a note; a closed status is exactly one form "
                f"and its note goes to the Item column ({FORMS_HELP})")
    return f"status {s[:50]!r} is outside the vocabulary ({FORMS_HELP})"


def privacy_problems(where: str, text: str, deny: list[str]) -> list[str]:
    hits = _privacy().scan_text(text, deny)
    return [f"{where}: {kind} ×{count} (value not printed)" for kind, count in sorted(hits.items())]


def invisible(text: str) -> str | None:
    """The first character of `text` a reader does not see, or None.

    Refused: categories Cc (controls, tab and CR among them), Cf (format: soft hyphen, zero-width
    and bidi controls, tags), Zl and Zp (line and paragraph separators), every Zs space but U+0020
    (NBSP, figure and ideographic spaces), INVISIBLE_EXTRA, and an unassigned (Cn) code point in
    the DEFAULT_IGNORABLE ranges."""
    for ch in text:
        category = unicodedata.category(ch)
        if (category in ("Cc", "Cf", "Zl", "Zp") or (category == "Zs" and ch != " ")
                or ord(ch) in INVISIBLE_EXTRA or (category == "Cn" and _default_ignorable(ord(ch)))):
            return ch
    return None


def _line_problem(line: str) -> str | None:
    odd = invisible(line)
    if odd:
        return f"a control or invisible character U+{ord(odd):04X}"
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
    hidden = _hidden_at(lines, start)
    if hidden:
        problems.append(f"docs/backlog.md line {start + 1}: `{BOARD_HEADING}` sits inside {hidden}; GitHub does "
                        "not render it, so the organization's check does not read it")
    return problems


def _hidden_at(lines: list[str], start: int) -> str | None:
    """What hides line `start` from a reader on GitHub: an HTML comment opened anywhere on a line,
    an HTML block (any line starting with up to three spaces and `<`, open until a blank line) or a
    code fence opened above it and still open there; None when nothing does. The same rule as the
    organization's `_hidden_at`, conservative on purpose: `<3` opens a block as `<div>` does."""
    state, opened, fence = None, 0, ""
    for n in range(start):
        line = lines[n]
        if state == "comment":
            if "-->" not in line:
                continue
            state, line = None, line.split("-->", 1)[1]
        if state == "html":
            if not line.strip():
                state = None
            continue
        if state == "fence":
            m = re.match(r" {0,3}(`{3,}|~{3,})\s*$", line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                state = None
            continue
        m = FENCE_OPEN.match(line)
        if m:
            state, opened, fence = "fence", n, m.group(1)
            continue
        rest = line
        while "<!--" in rest:
            rest = rest.split("<!--", 1)[1]
            if "-->" in rest:
                rest = rest.split("-->", 1)[1]
            else:
                state, opened = "comment", n
                break
        if state is None and re.match(r" {0,3}<", line):
            state, opened = "html", n
    names = {"comment": "an HTML comment", "html": "an HTML block", "fence": "a code fence"}
    return f"{names[state]} opened on line {opened + 1}" if state else None


def check_board(text: str, changelog: str, deny: list[str] | None = None, *,
                merged: dict[int, list[str]] | None = None, gaps: bool = True,
                in_flight: set[int] | None = None) -> list[str]:
    """Every refusal for one board, one per line. `merged` maps an OBS number to the pull requests
    (`#n`) whose merged commit delivered it (`merged_ids`); None when the history was not read.
    `gaps` turns on the vacancy rule (fixtures that hold a few rows under a high next-free id
    turn it off); `in_flight` names gaps whose reservation is younger than GAP_GRACE_DAYS
    (`gap_ages`), which need no declaration yet. Without it every gap needs one."""
    deny = deny or []
    problems: list[str] = []
    headings = set(re.findall(r"^##\s+(\d+\.\d+\.\d+)\b", changelog, re.M))
    m = NEXT_FREE.search(text)
    next_free = int(m.group(1)) if m else None
    if next_free is None:
        problems.append("docs/backlog.md: no `**Next free ID:** `OBS-n`` line, so the OBS register "
                        "(.claude/agent-sync.json idRegisters.OBS) cannot reserve an id")
    if "\r" in text:
        # The organization reads the raw document and refuses a CR anywhere (a CRLF file included):
        # GitHub and Python split lines on it where a reader of the text does not.
        at = text.count("\n", 0, text.index("\r")) + 1
        problems.append(f"docs/backlog.md line {at}: the document holds a CR (U+000D); the board is read only "
                        "from LF-only text")
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

    seen: dict[int, int] = {}
    successors: list[tuple[str, str]] = []
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
        number = int(rid.split("-")[1])
        if number in seen:
            problems.append(f"{rid}: written twice (lines {seen[number]} and {n + 1}; `OBS-01` and `OBS-1` "
                            "are one id); reserve a new id")
        seen.setdefault(number, n + 1)
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
        kind, detail = parsed
        if (kind == "open" and merged and number in merged and not PARTLY_DONE.search(status)
                and not REOPENED.search(status)):
            problems.append(f"{rid}: open, but merged commit(s) {', '.join(merged[number])} name it as delivered "
                            "(`(OBS-n)` in a subject ending in `(#n)`); close it (`done in X.Y.Z (#n)` or `done — "
                            "merged (#n), unreleased`), write `open — partly done: …` when something remains, or, for "
                            "a step toward the row, name it without parentheses in the title (`OBS-n: step 2`)")
        if kind == "released" and detail not in headings:
            problems.append(f"{rid}: `done in {detail}` but CHANGELOG.md has no `## {detail}` heading")
        if kind == "superseded":
            successors.append((rid, detail))
    # A successor is an existing row, spelled as that row spells its id: `OBS-1` for `OBS-01` (or
    # `obs-01`) names no row to a reader of strings, and the organization's check then reads the
    # chain as open (stricter: that check does not refuse it).
    spelled = {line.split(" | ", 1)[0][2:] for line in lines[start + 1:end] if ROW.fullmatch(line)}
    for rid, successor in successors:
        if successor not in spelled:
            problems.append(f"{rid}: `superseded by {successor}` names no row on the board spelled that way; "
                            "write the successor's id exactly as its row does")
    if gaps and next_free is not None:
        problems += _vacancy_problems(text, set(seen), next_free, in_flight or set())
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


def _vacancy_problems(text: str, present: set[int], next_free: int, in_flight: set[int] = frozenset()) -> list[str]:
    """Every id below the next free one is a row or a declared vacancy with its reason.

    An id the register handed out and nobody wrote (OBS-146, reserved in #300's window and never
    written) reads, without a declaration, exactly like a row that was deleted. An id reserved within
    GAP_GRACE_DAYS (`in_flight`) is a pull request still open and needs nothing; past that, a
    reservation nobody wrote is abandoned and must be declared. A declaration whose row exists fails."""
    problems: list[str] = []
    lines = VACANT_LINE.findall(text)
    if len(lines) > 1:
        problems.append("docs/backlog.md: more than one `**Vacant ids:**` line; keep one")
    declared: dict[int, str] = {}
    for entry in VACANT_ENTRY.finditer(" ".join(lines)):
        number = int(entry.group(1).split("-")[1])
        if number in declared:
            problems.append(f"{entry.group(1)}: declared vacant twice")
        declared[number] = entry.group(2).strip()
        if not declared[number]:
            problems.append(f"{entry.group(1)}: declared vacant without a reason; write why, in the parentheses")
    if lines and len(re.findall(r"`OBS-[^`]*`", " ".join(lines))) != len(VACANT_ENTRY.findall(" ".join(lines))):
        problems.append("docs/backlog.md: the `**Vacant ids:**` line holds an id not written as `OBS-n` (reason)")
    for number in range(1, next_free):
        if number not in present and number not in declared and number not in in_flight:
            problems.append(f"OBS-{number}: no row and not declared vacant; a reader cannot tell a lost row from "
                            f"an id never written (reserved more than {GAP_GRACE_DAYS} days ago, or the history is "
                            "unread). Add it to the `**Vacant ids:**` line: `OBS-n` (why)")
    for number in sorted(declared):
        if number in present:
            problems.append(f"OBS-{number}: declared vacant but its row exists; remove its entry from the "
                            "`**Vacant ids:**` line")
        elif number >= next_free:
            problems.append(f"OBS-{number}: declared vacant at or above the next free id OBS-{next_free}")
    return problems


def delivered(subjects: list[str]) -> tuple[dict[int, list[str]], int]:
    """{OBS number: ["#n", …]} from squash subjects, and how many carried a pull request number.

    A revert (`Revert "…"`) delivers nothing: it takes a delivery back."""
    found: dict[int, list[str]] = {}
    commits = 0
    for subject in subjects:
        pr = MERGED_PR.search(subject)
        if not pr or subject.startswith('Revert "'):
            continue
        commits += 1
        for group in PAREN_GROUP.findall(subject[:pr.start()]):
            for token in group.split(","):
                m = DELIVERED_ID.fullmatch(token.strip())
                if m:
                    found.setdefault(int(m.group(1)), []).append(f"#{pr.group(1)}")
    return found, commits


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # Reads only, but the subcommand is spliced in: say so to git as the engine's other readers do.
    return subprocess.run(["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=120)


def _history_unreadable(repo: Path) -> str | None:
    """Why the history of `repo` cannot be read whole, or None when it can."""
    try:
        shallow = _git(repo, "rev-parse", "--is-shallow-repository")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"git unavailable ({type(exc).__name__})"
    if shallow.returncode != 0:
        return "not a Git checkout"
    if shallow.stdout.strip() == "true":
        return "a shallow clone (fetch the whole history, `git fetch --unshallow`)"
    return None


def merged_ids(repo: Path = ROOT, ref: str = "HEAD",
               pending: list[str] | None = None) -> tuple[dict[int, list[str]] | None, str]:
    """{OBS number: ["#n", …]} from the merged commits of `ref`, plus `pending` subjects (the pull request
    under test, `TITLE (#N)`), and a note saying what was read.

    None when the history cannot be read, with the reason: not a Git checkout, the ref unknown, or a
    shallow clone, whose history stops early and would pass a stale row silently. The caller says so;
    it never reads as clean."""
    why = _history_unreadable(repo)
    if why:
        return None, f"merged commits NOT checked: {why}"
    log = _git(repo, "log", "--format=%s", ref)
    if log.returncode != 0:
        return None, f"merged commits NOT checked: `git log {ref}` failed"
    subjects = log.stdout.splitlines() + [p for p in (pending or []) if p.strip()]
    found, commits = delivered(subjects)
    extra = f", plus this pull request's title" if pending and any(p.strip() for p in pending) else ""
    return found, f"merged commits: {commits} read on {ref}{extra}, {len(found)} OBS ids delivered"


def pending_subjects(repo: Path = ROOT, env: dict | None = None) -> list[str]:
    """The squash subjects the pull request under test may leave on main: its title, and, when it has one
    commit, that commit's headline (both as `… (#N)`). Empty outside a pull_request run."""
    env = os.environ if env is None else env
    out = [env.get(PENDING_SUBJECT_ENV, "").strip()]
    number, commits = env.get(PR_NUMBER_ENV, "").strip(), env.get(PR_COMMITS_ENV, "").strip()
    if number.isdigit() and commits == "1":
        parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD")
        if parents.returncode == 0 and len(parents.stdout.split()) == 3:   # the merge checkout: HEAD^2 is the head
            headline = _git(repo, "log", "-1", "--format=%s", "HEAD^2")
            if headline.returncode == 0 and headline.stdout.strip():
                out.append(f"{headline.stdout.strip()} (#{number})")
    return [s for s in out if s]


def gap_ages(repo: Path = ROOT, ref: str = "HEAD", path: str = "docs/backlog.md",
             grace_days: int = GAP_GRACE_DAYS) -> tuple[set[int] | None, str]:
    """The ids whose reservation is younger than `grace_days`: those the board's `**Next free ID:**` line
    passed on `ref`'s first-parent history less than that before `ref`'s own commit time, or not at all
    yet (a working-tree change). None, with the reason, when the history cannot be read."""
    why = _history_unreadable(repo)
    if why:
        return None, f"reservation ages NOT read: {why}"
    head = _git(repo, "log", "-1", "--format=%ct", ref)
    log = _git(repo, "log", "--first-parent", "--reverse", "--format=%x00%ct", "-p", "-U0", ref, "--", path)
    if head.returncode != 0 or log.returncode != 0:
        return None, f"reservation ages NOT read: `git log {ref}` failed"
    now = int(head.stdout.strip())
    passed: dict[int, int] = {}            # id -> when the next free id first moved past it
    highest = 0
    for chunk in log.stdout.split("\x00")[1:]:
        when = int(chunk.split("\n", 1)[0])
        for value in re.findall(r"^\+\*\*Next free ID:\*\* `OBS-(\d+)`", chunk, re.M):
            for number in range(highest, int(value)):
                passed.setdefault(number, when)
            highest = max(highest, int(value))
    young = {n for n, when in passed.items() if now - when < grace_days * 86400}
    young |= set(range(highest, 100000))  # past the last committed line: this change itself
    return young, f"reservation ages read from {len(passed)} ids' history (grace {grace_days} days)"


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
    parser.add_argument("--history-ref", default="HEAD",
                        help="the branch whose merged commits are read (this repository's own board only)")
    parser.add_argument("--require-history", action="store_true",
                        help="exit 1 when the merged commits cannot be read (a shallow clone)")
    args = parser.parse_args(argv)
    try:
        # Raw bytes: read_text turns CR and CRLF into LF and would hide a CR the organization refuses.
        text = args.board.read_bytes().decode("utf-8")
        changelog = args.changelog.read_bytes().decode("utf-8")
        deny, note = load_denylist(args.private_denylist)
    except (OSError, ValueError) as exc:
        print(f"check_backlog: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.board.resolve() == BOARD.resolve():
        merged, history = merged_ids(ROOT, args.history_ref, pending_subjects(ROOT))
        in_flight, ages = gap_ages(ROOT, args.history_ref)
        history += "; " + ages
    else:
        merged, history = None, "merged commits NOT checked: --board is not this repository's board"
        in_flight = None
    # Without the history a reservation in flight cannot be told from an abandoned one: the gaps are then
    # not checked, and the note below says so (as for the merged commits).
    problems = check_board(text, changelog, deny, merged=merged, in_flight=in_flight, gaps=in_flight is not None)
    if (merged is None or in_flight is None) and args.require_history:
        problems.append(f"check_backlog: {history}")
    for problem in problems:
        print(problem, file=sys.stderr)
    if merged is None or in_flight is None:
        # Said where a reader looks, not only in the JSON: an unread history is not a clean one.
        print(f"check_backlog: {history}", file=sys.stderr)
    print(json.dumps({"board": args.board.name, "problems": len(problems), "privacy": note, "history": history}))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
