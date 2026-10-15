#!/usr/bin/env python3
"""Record what a turn changed in a watched repository. Facts only.

Called by the observatory-log plugin's Stop hook. Standard library only — a hook
is a bad place to discover a missing dependency, so nothing here imports outside
the interpreter.

One record per session, corrected as the work grows: the second turn supersedes
the first by compare-and-swap rather than appending a near-duplicate. `why` is
never written here. The hook cannot know it, and inventing it would be the exact
failure the whole design refuses.

Prints a JSON object on stdout for the hook to act on:
    {"recorded": true, "memoryId": …, "revision": …, "hasWhy": false,
     "project": "project:…", "files": 3, "insertions": 40, "deletions": 5,
     "session": …, "ask": true, "rationale": null}
`ask` says whether the hook should ask for the `why` at all: false once this record
has been asked about (`why_asks.py`, once per record), and false when this
session's own tools already wrote the rationale — the decision register or a
task-pipeline ledger line, attributed through `--transcript` — linked in
`rationale` and in the record's evidence instead. When `ask` is true, `journal`
names the file the prompt hook appends its delivery to.
Any refusal is `{"recorded": false, "reason": …}` — never a traceback, because
the caller is a hook that must degrade silently.
"""
from __future__ import annotations
import argparse, json, os, re, shlex, sqlite3, subprocess, sys, time, pathlib
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic                                                                    
import paths                                                                     
import safe_git                                                                  
import why_asks                                                                  

OWNER = "agent:claude-code"
MAX_SUBJECTS = 8


#: Reasons that are ANSWERS about the work rather than failures of the recorder.
#: Everything else in `reason` is a fault, and a fault must not be silent: the
#: same distinction drawn for collectors, applied to the one component that
#: runs in every session of every project.
NOT_A_FAULT = (
    "not a git repository",
    "not in the registry",
    "nothing changed",
    "unchanged since the last turn",
    "no session id",
    # THE RULE WORKING, not the recorder failing. `estate.py` exists to answer
    # "what counts as this estate's OWN work" in one place, and declining a turn
    # inside a third-party clone is that answer: "somebody else's history".
    # Missing from this list, it made every such turn a fault: a session inside
    # a checked-out external project raised `companion.not_recording`, and a
    # durable line was appended for every one of those turns as well.
    "project ownership is",
)


#: Set by `main` before anything is written, so `out` can stamp them on
#: every path including the outer exception handler. `_cwd` is here for the same
#: reason the session id is: a lost turn's first question is WHICH PROJECT, and
#: the outer handler has no arguments left to read it from.
_session_id = ""
_cwd = ""


def out(payload: dict, receipt: dict | None = None) -> int:
    """Print the result for the hook, and leave a receipt for the findings.

    **The silence this closes.** `ask-why.py` says nothing when `recorded` is
    false, the hook discards stderr, and the hook is *designed* to be quiet
    about work that is not its business — so a genuine failure was
    indistinguishable from a quiet turn. Measured 2026-09-07: roughly seventy-two
    Stop hooks in one session each raised `IllegalTransition` and reported
    nothing, and the only sign was a ledger row that had stopped moving fifteen
    hours earlier.
    """
    print(json.dumps(payload, ensure_ascii=False))
    reason = payload.get("reason") or ""
    # THE SESSION, because this file is SHARED. Every session of every watched
    # project on the machine writes it — 49 Claude processes were running when
    # that was measured — so the last writer wins and a reader must be able to
    # tell whose turn the reason describes. Without it `companion.not_recording`
    # says "the companion could not record the last turn it tried" and cannot
    # say which one.
    # `receipt` is what the receipt carries and the hook does not need — the
    # `whyAsks` measurement — so the hook's stdin stays the size it was.
    payload = {**payload, **(receipt or {}),
               "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "session": _session_id or "",
               "fault": bool(reason) and not any(k in reason for k in NOT_A_FAULT)}
    try:
        atomic.write_json(paths.SCRATCH / "record-turn.json", payload)
    except Exception as exc:                                                      
        # NAMED ON STDERR, not swallowed. A hook must not fail because its own
        # receipt could not be written — the facts are already in the ledger —
        # but a receipt that disappears silently is the very defect this
        # function was added to close, one layer in. The hook discards stderr,
        # so this is for a person running the tool by hand.
        print(f"the record-turn receipt could not be written: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)

    # AND THE DURABLE HALF, because the receipt above is a SLOT. It holds one
    # turn, every session on this machine writes it, and the board is rebuilt
    # every 1800 seconds — so a fault was reported only if it happened to be the
    # newest turn on the whole machine when the tick ran. The incident that
    # produced `companion.not_recording` survived because it was permanent;
    # a transient fault, which is the class this machine actually has, was
    # erased by the next quiet turn of any project.
    if payload["fault"]:
        try:
            import companion_faults                                                 
            kept = companion_faults.append(
                {"at": payload["at"], "session": payload["session"],
                 "cwd": _cwd, "reason": reason})
            where = companion_faults.LOG
        except Exception as exc:                                                   
            kept, where = None, f"the fault log ({type(exc).__name__}: {exc})"
        if kept is None:
            # Named on stderr and NOT swallowed, for the same reason as above:
            # the hook discards stderr, so this is for a person running the tool
            # by hand — and the finding rule reports the orphan separately,
            # because a count with a silently missing line is worse than none.
            print(f"the lost turn could not be added to {where}: the receipt is "
                  f"now its only record", file=sys.stderr)
    return 0


def _fault(exc: BaseException) -> int:
    """The one place an exception becomes a receipt, for both handlers.

    A `sqlite3` error ALSO goes to `store_faults`, which is where free space,
    WAL size and the holder count at the moment of failure are captured. This
    recorder is the machine's most frequent store-toucher — every turn of every
    session opens the database — and it was the one caller that module was never
    wired into. A domain refusal such as `IllegalTransition` is deliberately not
    filed there: every reader of that log reports that the STORE failed, and an
    `lsof` per turn per session would be telemetry about nothing.
    """
    if isinstance(exc, sqlite3.Error):
        try:
            import store_faults                                                     
            store_faults.record("companion.record_turn", exc)                 
        except Exception:                                                          
            pass                                                                   
    return out({"recorded": False, "reason": f"{type(exc).__name__}: {exc}"})


def git(cwd: pathlib.Path, *args: str) -> str:
    """stdout, or "" — through `safe_git`. This runs from the Stop hook on every
    agent turn, and bare git there ran the operator's gpg (signature display),
    fsmonitor, clean filters and index hooks each time."""
    try:
        p = safe_git.run(args, repo=cwd, timeout=20)
    except Exception:
        return ""
    return p.stdout if p.returncode == 0 else ""


def repo_id_for(cwd: pathlib.Path) -> tuple[str | None, str | None]:
    """Map a checkout to a registry repository id, by remote first then by path."""
    try:
        repos = json.loads((paths.REGISTRY / "repositories.json").read_text(encoding="utf-8"))
    except Exception:
        return None, None
    remote = git(cwd, "remote", "get-url", "origin").strip()
    nwo = None
    m = re.search(r"(?:github\.com|bitbucket\.org)[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?$", remote)
    if m:
        nwo = f"{m.group(1)}/{m.group(2)}"
    top = git(cwd, "rev-parse", "--show-toplevel").strip()
    for r in repos["repositories"]:
        if nwo and r["name_with_owner"] == nwo:
            return r["id"], top
        local = r.get("local") or {}
        if top and local.get("path") == top:
            return r["id"], top
    return None, top


#: Only the operator's own work is recorded — the rule itself lives in
#: `estate.py`, which exists BECAUSE this file and `collectors/scan_events.py`
#: once disagreed about what the estate is.
  
#: This module kept a COPY of that set, and the test named
#: `test_one_rule_two_readers` could not detect a divergence: it compared
#: `estate.RECORDED_OWNERSHIP` against a literal spelled out inside the test,
#: then checked only that the STRING "RECORDED_OWNERSHIP" appeared here. Driven
#: 2026-09-07 against a planted copy reading `{"external"}` — this recorder
#: inverted to write about nothing BUT somebody else's history — and both
#: assertions passed. `sys.path` already reaches the root for
#: `atomic` and `paths`, so the copy was never necessary.
import estate                                                                    
records_events = estate.records_events


def _knowledge_age(top: pathlib.Path) -> dict:
    """Days since the code graph was built and the wiki notes were touched.

    The graph's age is measured from the checkout itself; the wiki's from the
    vault scan's receipt, because the recorder must not walk the vault on every
    turn. Both degrade to None — absent is not stale.
    """
    import datetime as _dt
    out: dict = {"graphAgeDays": None, "wikiAgeDays": None}
    gj = top / "graphify-out" / "graph.json"
    if gj.is_file():
        try:
            out["graphAgeDays"] = max(0, int(
                (_dt.datetime.now(_dt.timezone.utc)
                 - _dt.datetime.fromtimestamp(gj.stat().st_mtime, _dt.timezone.utc)
                 ).total_seconds() // 86400))
        except OSError:
            pass
    vf = paths.SCRATCH / "vault.json"
    if vf.is_file():
        try:
            rows = json.loads(vf.read_text(encoding="utf-8"))
            row = next((r for r in rows if r.get("folder") == top.name), None)
            stamp = (row or {}).get("notes_updated_on")
            if stamp:
                out["wikiAgeDays"] = max(0, (
                    _dt.datetime.now(_dt.timezone.utc).date()
                    - _dt.date.fromisoformat(stamp)).days)
        except (OSError, ValueError, TypeError):
            pass
    return out


def project_for(repo_id: str) -> tuple[str | None, str | None]:
    """Return (project_id, ownership) for a repository, or (None, None)."""
    import paths
    try:
        rels = json.loads((paths.REGISTRY / "relations.json").read_text(encoding="utf-8"))
        projects = {p["id"]: p for p in
                    json.loads((paths.REGISTRY / "projects.json").read_text(encoding="utf-8"))["projects"]}
    except Exception:
        return None, None
    for rel in rels["relations"]:
        if rel["type"] == "implemented_by" and rel["to"] == repo_id:
            pid = rel["from"]
            return pid, (projects.get(pid) or {}).get("ownership")
    return None, None


def diff_facts(cwd: pathlib.Path) -> dict:
    """What changed, from git rather than from anything the agent says."""
    porcelain = [l for l in git(cwd, "status", "--porcelain").splitlines() if l.strip()]
    numstat = [l for l in git(cwd, "diff", "HEAD", "--numstat").splitlines() if l.strip()]
    ins = dele = 0
    for line in numstat:
        parts = line.split("\t")
        if len(parts) >= 2:
            ins += int(parts[0]) if parts[0].isdigit() else 0
            dele += int(parts[1]) if parts[1].isdigit() else 0
    files = sorted({l[3:].split(" -> ")[-1].strip() for l in porcelain})
    # A deleted file holds no rationale: removing `docs/DECISIONS.md` is not
    # writing one, so a `D` in either column takes the file out of the reading.
    deleted = sorted({l[3:].split(" -> ")[-1].strip() for l in porcelain if "D" in l[:2]})
    untracked = sorted({l[3:].strip() for l in porcelain if l.startswith("??")})
    if any(u.endswith("/") for u in untracked):
        # `status` folds an untracked directory into one `?? docs/` line, which
        # hides the register inside it. Only the files a rationale can live in are
        # asked for, so a large untracked tree costs a walk, not a listing.
        untracked = sorted(set(untracked) | {l.strip() for l in git(
            cwd, "ls-files", "--others", "--exclude-standard", "--",
            ":(glob,icase)**/decisions.md", ":(glob,icase)**/adr/**",
            ".task-pipeline/run.md").splitlines() if l.strip()})
    head = git(cwd, "rev-parse", "--short", "HEAD").strip()
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD").strip()
    upstream = bool(git(cwd, "rev-parse", "--abbrev-ref", "@{u}").strip())
    unpushed = [l for l in git(cwd, "log", "--oneline", "@{u}..HEAD").splitlines() if l.strip()] \
        if upstream else []
    # The files the unpushed commits touched: a turn that COMMITTED its decision
    # register wrote its rationale as surely as one that left it uncommitted.
    committed = sorted({l.strip() for l in git(cwd, "diff", "--name-only", "--diff-filter=d",
                                               "@{u}..HEAD").splitlines() if l.strip()}) \
        if unpushed else []
    return {"files": files, "insertions": ins, "deletions": dele, "head": head,
            "branch": branch, "unpushed": [u[:120] for u in unpushed[:MAX_SUBJECTS]],
            "untracked": untracked, "committed": committed, "deleted": deleted,
            "base": "@{u}" if unpushed else "HEAD"}


def statement_of(facts: dict, repo_id: str) -> str:
    n = len(facts["files"])
    bits = [f"{repo_id.split(':', 1)[1]} on {facts['branch'] or '?'}: "
            f"{n} file{'s' if n != 1 else ''} changed, "
            f"+{facts['insertions']}/-{facts['deletions']}"]
    if facts["unpushed"]:
        bits.append(f"{len(facts['unpushed'])} unpushed commit(s)")
    shown = facts["files"][:MAX_SUBJECTS]
    bits.append("touched: " + ", ".join(shown) + ("…" if n > len(shown) else ""))
    return " | ".join(bits)


#: Where a project writes its rationale down. A decision register is any
#: `decisions.md` (the conventional `docs/DECISIONS.md`, a run's
#: `<artifacts>/decisions.md`) and any file under an `adr/` directory; the
#: task-pipeline ledger counts only for a `decision:` or `stage:` line, because the
#: ledger also records plain notes.
RUN_LEDGER = ".task-pipeline/run.md"
LEDGER_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:decision|stage)\s*:\s*(.*)$", re.I)
HEADING = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
ANCHOR_CHARS = 120
#: An untracked or ignored ledger or register is read whole, so it is read bounded.
READ_LIMIT = 256 * 1024
#: Somebody else's history inside the checkout: a vendored library's own ADRs are
#: not this project's rationale. Submodules are added from `.gitmodules`.
VENDORED = frozenset({"vendor", "vendors", "node_modules", "third_party", "third-party",
                      "thirdparty", "external", "bower_components", ".venv", "venv",
                      "site-packages", ".git"})
#: A line shorter than this (`---`, `## x`) is too common to attribute anything.
MIN_NEEDLE = 6
#: The whole attribution — the transcript pass, the diffs, the ledger read — must
#: finish inside this many seconds, AFTER the record is written. The Stop hook has
#: twenty; a turn whose attribution runs out of time is asked about.
ATTRIBUTION_SECONDS = 2.0
#: The part of a session transcript read for attribution: its end, where this
#: session's newest work is. A register written further back is not found, and the
#: turn is asked about — the failure is toward asking.
TRANSCRIPT_TAIL = 4 << 20
#: The tail of an ignored run ledger that is read: its newest lines.
LEDGER_TAIL = 64 * 1024
#: The ledger lines tried, newest first.
LEDGER_LINES = 200
#: The register lines compared when the register adds no heading.
REGISTER_LINES = 200
#: Tools whose call names the file it writes, and the input field holding the text.
FILE_TOOLS = {"Edit": ("file_path", ("new_string",)),
              "Write": ("file_path", ("content",)),
              "MultiEdit": ("file_path", ("edits",)),
              "NotebookEdit": ("notebook_path", ("new_source",))}
#: A shell write: `> file`, `>> file` (also `cat > file <<EOF`), `tee [-a] file`.
#: A `>` inside prose (`a -> b`, `x => y`) is not a redirection, so the character
#: before it must start a word.
_REDIRECT = re.compile(r"(?:^|[\s;|&(])(?:\d|&)?>>?\s*([\"']?)([^\s'\";|&<>()]+)\1")
_TEE = re.compile(r"\btee\s+(?:-\S+\s+)*([\"']?)([^\s'\";|&<>()]+)\1")
_SED_INPLACE = re.compile(r"\bsed\b[^;|&\n]*?\s(?:-i\S*|--in-place\S*)\s[^;|&\n]*")
#: task-pipeline's own ledger writer, which names no file on its command line.
_LEDGER_WRITER = re.compile(r"\bstage_checkpoint\.py\b")
def _is_register(rel: str) -> bool:
    parts = pathlib.PurePosixPath(rel).parts
    if not parts:
        return False
    return parts[-1].lower() == "decisions.md" or (
        "adr" in (p.lower() for p in parts[:-1]) and parts[-1].lower().endswith((".md", ".markdown", ".txt")))


def _submodules(top: pathlib.Path) -> list[str]:
    if not (top / ".gitmodules").is_file():
        return []
    out = git(top, "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$")
    return [l.split(None, 1)[1].strip().rstrip("/") for l in out.splitlines() if len(l.split(None, 1)) == 2]


def _vendored(rel: str, submodules: list[str]) -> bool:
    if any(p.lower() in VENDORED for p in pathlib.PurePosixPath(rel).parts[:-1]):
        return True
    return any(rel == s or rel.startswith(s + "/") for s in submodules)


def _read_bounded(path: pathlib.Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(READ_LIMIT).splitlines()
    except OSError:
        return []


def _added_lines(top: pathlib.Path, rel: str, facts: dict) -> list[str]:
    """The non-blank lines this work added to `rel`: the whole file when git does not
    track it, else what `git diff <base>` adds — the uncommitted change and the
    unpushed commits together — ignoring whitespace, so re-indenting a register
    adds nothing."""
    if rel in facts.get("untracked", ()):
        lines = _read_bounded(top / rel)
    else:
        diff = git(top, "diff", "--ignore-all-space", "--ignore-blank-lines",
                   facts.get("base") or "HEAD", "--", rel)
        lines = [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    return [l for l in lines if l.strip()]


class _OutOfTime(Exception):
    """Attribution passed its budget: the turn is asked about."""


def _check(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise _OutOfTime()


def _shell_targets(command: str) -> list[str]:
    """The files a shell command WRITES: redirections, `tee`, `sed -i`. A command that
    only reads (`grep 'x' docs/DECISIONS.md`) writes nothing."""
    out = [m.group(2) for m in _REDIRECT.finditer(command)]
    out += [m.group(2) for m in _TEE.finditer(command)]
    for m in _SED_INPLACE.finditer(command):
        try:
            words = shlex.split(m.group(0))
        except ValueError:
            words = m.group(0).split()
        if words and not words[-1].startswith("-"):
            out.append(words[-1])
    return out


def _resolve(target: str, cwd: str | None, top: pathlib.Path) -> str:
    target = os.path.expanduser(target)
    base = cwd if cwd and os.path.isabs(cwd) else str(top)
    return os.path.realpath(os.path.join(base, target))


def session_writes(transcript: str | None, top: pathlib.Path, deadline: float) -> list[dict]:
    """This session's WRITING tool calls, from one bounded pass over its transcript.

    Each is `{"targets": {real path, …}, "text": str, "ledger": bool}`: an
    Edit/Write/MultiEdit/NotebookEdit with the file it names and the text it wrote,
    or a Bash command with the files it writes (`_shell_targets`) and its text.
    `ledger` is true for a call that writes the run ledger, including task-pipeline's
    `stage_checkpoint.py`, which names no file. Only the transcript's last
    `TRANSCRIPT_TAIL` bytes are read, once, and only lines carrying a `tool_use` are
    parsed. Raises `_OutOfTime` past the deadline.
    """
    if not transcript:
        return []
    try:
        with open(transcript, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TRANSCRIPT_TAIL))
            data = fh.read()
    except OSError:
        return []
    ledger = os.path.realpath(str(top / RUN_LEDGER))
    calls: list[dict] = []
    for n, line in enumerate(data.split(b"\n")):
        if n % 256 == 0:
            _check(deadline)
        if b'"tool_use"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content")
        cwd = rec.get("cwd") if isinstance(rec.get("cwd"), str) else None
        for item in content if isinstance(content, list) else ():
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            name, inp = item.get("name"), item.get("input")
            if not isinstance(inp, dict):
                continue
            if name in FILE_TOOLS:
                key, fields = FILE_TOOLS[name]
                target = inp.get(key)
                if not isinstance(target, str):
                    continue
                parts = []
                for f in fields:
                    v = inp.get(f)
                    if isinstance(v, list):          # MultiEdit's edits
                        parts += [e.get("new_string", "") for e in v if isinstance(e, dict)]
                    elif isinstance(v, str):
                        parts.append(v)
                targets = {_resolve(target, cwd, top)}
                calls.append({"targets": targets, "text": "\n".join(p for p in parts if isinstance(p, str)),
                              "ledger": ledger in targets})
            elif name == "Bash" and isinstance(inp.get("command"), str):
                cmd = inp["command"]
                targets = {_resolve(t, cwd, top) for t in _shell_targets(cmd)}
                writes_ledger = ledger in targets or bool(_LEDGER_WRITER.search(cmd))
                if targets or writes_ledger:
                    calls.append({"targets": targets, "text": cmd, "ledger": writes_ledger})
    return calls


def _register_written(added: list[str], text: str) -> bool:
    """Does `text` — what this session's calls wrote INTO this file — carry the
    register's addition? Its newest added heading, or most of its added lines. One
    common line is not enough."""
    heads = [l.strip() for l in added if HEADING.match(l)]
    if heads and heads[-1] in text:
        return True
    lines = [l.strip() for l in added if len(l.strip()) >= MIN_NEEDLE][:REGISTER_LINES]
    return bool(lines) and 2 * sum(1 for l in lines if l in text) > len(lines)


def _tail_lines(path: pathlib.Path, limit: int) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[1:] if size > limit and lines else lines


def rationale_of(top: pathlib.Path, facts: dict, transcript: str | None = None,
                 deadline: float | None = None) -> dict | None:
    """`{source, ref, anchor?}` when THIS session already wrote its rationale, else None.

    Best-effort and bounded: the caller has already written the record, and this
    answers within `ATTRIBUTION_SECONDS` or not at all (None, so the turn is asked).

    A decision register counts when it is a `decisions.md` or an `adr/` file
    (`.md`, `.markdown`, `.txt`) outside a vendored tree or a submodule, not deleted,
    the work added at least one non-blank line to it (whitespace ignored), and one of
    this session's writing calls targets that exact file with text carrying the
    addition (`_register_written`). The run ledger is consulted only when one of
    those calls wrote it; its newest `decision:` / `stage:` line counts when that
    line's text is in what those calls wrote. Errors read as "no rationale", which
    asks — the old behaviour.
    """
    deadline = deadline if deadline is not None else time.monotonic() + ATTRIBUTION_SECONDS
    try:
        deleted = set(facts.get("deleted") or ())
        touched = sorted((set(facts.get("files") or ()) | set(facts.get("committed") or ())
                          | set(facts.get("untracked") or ())) - deleted)
        candidates = [r for r in touched if _is_register(r)]
        ledger_file = top / RUN_LEDGER
        if not candidates and not ledger_file.is_file():
            return None
        calls = session_writes(transcript, top, deadline)
        if not calls:
            return None
        subs = _submodules(top) if candidates else []
        for rel in candidates:
            _check(deadline)
            if _vendored(rel, subs):
                continue
            real = os.path.realpath(str(top / rel))
            text = "\n".join(c["text"] for c in calls if real in c["targets"])
            if not text:
                continue
            added = _added_lines(top, rel, facts)
            if not added or not _register_written(added, text):
                continue
            # The LAST heading added: a register grows at its end, and a new
            # register's first heading is its title, not a decision.
            heads = [m.group(1) for l in added if (m := HEADING.match(l))]
            anchor = heads[-1] if heads else None
            return {"source": "decision-register", "ref": rel,
                    **({"anchor": anchor[:ANCHOR_CHARS]} if anchor else {})}
        ledger_text = "\n".join(c["text"] for c in calls if c["ledger"])
        if ledger_text and ledger_file.is_file():
            _check(deadline)
            tracked_change = RUN_LEDGER in touched and RUN_LEDGER not in (facts.get("untracked") or ())
            lines = (_added_lines(top, RUN_LEDGER, facts) if tracked_change
                     else _tail_lines(ledger_file, LEDGER_TAIL))
            tried = 0
            for line in reversed(lines):
                m = LEDGER_LINE.match(line)
                if not m:
                    continue
                tried += 1
                if tried > LEDGER_LINES:
                    break
                said = m.group(1).strip()
                if len(said) >= MIN_NEEDLE and said in ledger_text:
                    return {"source": "task-pipeline", "ref": RUN_LEDGER,
                            "anchor": line.strip()[:ANCHOR_CHARS]}
    except _OutOfTime:
        return None
    except Exception:                                                              # noqa: BLE001
        return None
    return None


def _why_asks(conn) -> dict | None:
    """The ask's answer rate for the receipt; None (and a line on stderr) when it
    cannot be measured — a measurement must never cost the turn its record."""
    try:
        return why_asks.measure(conn)
    except Exception as exc:                                                       # noqa: BLE001
        print(f"the why-ask rate could not be measured: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--session-id", default="")
    # The session's own transcript (the Stop payload's `transcript_path`): what this
    # session wrote, as against what any session left in the shared working tree.
    ap.add_argument("--transcript", default="")
    args = ap.parse_args()
    # BEFORE THE FIRST GUARD, not beside the one that checks it. The assignment
    # sat after the git-repository check, so the earliest returns — the common
    # case on an unwatched directory — wrote a receipt with an empty session.
    global _session_id, _cwd
    _session_id = args.session_id or ""
    _cwd = args.cwd or ""

    cwd = pathlib.Path(args.cwd)
    if not cwd.is_dir():
        return out({"recorded": False, "reason": "cwd does not exist"})
    if not git(cwd, "rev-parse", "--git-dir").strip():
        return out({"recorded": False, "reason": "not a git repository"})

    repo_id, top = repo_id_for(cwd)
    if repo_id is None:
        return out({"recorded": False,
                    "reason": "this repository is not in the registry; run "
                              "`project-observatory full local` to add it"})
    facts = diff_facts(pathlib.Path(top or cwd))
    if not facts["files"] and not facts["unpushed"]:
        return out({"recorded": False, "reason": "nothing changed"})

    from store import db as store_db
    from store import ledger as L
    project_id, ownership = project_for(repo_id)
    if not estate.records_events(ownership):
        return out({"recorded": False,
                    "reason": f"project ownership is {ownership!r}; only "
                              f"{sorted(estate.RECORDED_OWNERSHIP)} are recorded"})
    project_id = project_id or repo_id
    statement = statement_of(facts, repo_id)
    evidence = [{"uri": f"repo:{repo_id}", "head": facts["head"], "branch": facts["branch"]},
                {"uri": "git:status --porcelain", "files": facts["files"][:40]}]

    # A row with no session id can never be REVISED: the prior-row lookup below
    # is keyed on the session, so every turn would mint a fresh `proposed`
    # memory and the "unchanged since the last turn" guard could never fire.
    # One long session already holds twenty revisions of one memory; without an
    # id that would have been twenty memories in the review queue, each needing
    # a decision. Refusing is the honest answer, and the hook now resolves its
    # interpreter before reading the payload so an empty id means the CALLER
    # omitted it rather than the parse having failed silently.
    if not args.session_id:
        return out({"recorded": False,
                    "reason": "no session id: a row keyed to no session cannot be "
                              "revised, so every turn would add one to the review "
                              "queue instead of correcting the last",
                    "project": project_id})

    # THE WORKFLOW THIS SESSION EXECUTES, when whoever started it said so (the
    # account manager or Fabric sets `OBSERVATORY_WORKFLOW_ID`). It links a
    # running session to the workflow it holds, so "who is working on this
    # now" is answered by a live session rather than by the last checkpoint
    # alone. Anything not shaped like a workflow id is ignored, not stored.
    workflow_id = os.environ.get("OBSERVATORY_WORKFLOW_ID") or None
    if workflow_id is not None and not re.fullmatch(r"wf_[0-9a-f]{16}", workflow_id):
        workflow_id = None

    conn = store_db.connect()
    try:
        prior = None
        if args.session_id:
            # `NOT IN tombstones`, because a record an erasure has closed takes
            # no further revisions and this hook runs on every turn
            # of every session. Without it the append would raise and the hook
            # would report `recorded: false` for the rest of the session; with
            # it the turn starts a fresh record, which is what an erasure means.
            row = conn.execute(
                "SELECT l.memory_id, l.revision, l.why, l.statement, l.state, l.workflow_id"
                " FROM ledger l JOIN (SELECT memory_id, MAX(revision) r FROM ledger"
                "   WHERE session_id = ? AND kind = 'session' GROUP BY memory_id) m"
                "  ON m.memory_id = l.memory_id AND m.r = l.revision"
                " WHERE l.memory_id NOT IN (SELECT memory_id FROM tombstones)"
                " ORDER BY l.revision DESC LIMIT 1", (args.session_id,)).fetchone()
            prior = row if row and row["memory_id"] else None
        # A PROMOTED RECORD IS FINISHED, as far as this writer is concerned.
        #
        # **The defect this closes was silent and total.** `tools/corroborate.py`
        # promotes a `proposed` session row to `observed` once it can re-check
        # the commit sha — which is exactly its job — and the next turn's append
        # then asked for `state="proposed"` on an `observed` record. The
        # lifecycle refuses that edge, correctly, so `L.append` raised
        # `IllegalTransition: observed -> proposed`, the handler below turned it
        # into `recorded: false`, `ask-why.py` says nothing on a false, and the
        # hook's stderr is discarded. Measured 2026-09-07: **once a session was
        # corroborated the companion stopped recording it for ever** — this
        # session's own record sat at revision 2 from 20:12 the previous evening
        # while roughly seventy-two Stop hooks ran and wrote nothing.
        #
        # Revising it in place at its CURRENT state would be worse than the bug:
        # the new statement describes work the corroborator never checked, so
        # keeping `observed` would claim a corroboration the text never got. A
        # fresh `proposed` record is what an automated writer is allowed to make,
        # and the link to what came before travels in `provenance` — not in
        # `supersedes`, which would say the earlier record is no longer true when
        # it is still true about earlier work.
        continues = None
        if prior is not None and (prior["state"] or "") != "proposed":
            continues = prior["memory_id"]
            prior = None
        if prior is not None and prior["statement"] == statement \
                and (workflow_id is None or prior["workflow_id"] == workflow_id):
            # Nothing moved since the last turn. Appending here would add a
            # revision that says exactly what the previous one said, and
            # append-only is a reason to be careful about what gets appended.
            return out({"recorded": False, "reason": "unchanged since the last turn",
                        "memoryId": prior["memory_id"], "revision": prior["revision"],
                        "hasWhy": bool(prior["why"]), "project": project_id},
                       {"whyAsks": _why_asks(conn)})
        result = L.append(
            conn, owner=OWNER, kind="session", statement=statement,
            why=prior["why"] if prior else None,
            memory_id=prior["memory_id"] if prior else None,
            expected_revision=prior["revision"] if prior else None,
            project_id=project_id, session_id=args.session_id or None,
            workflow_id=workflow_id,
            function="episodic", scope="project", state="proposed",
            # NO CONFIDENCE, deliberately. This record restates a `git status`
            # — "166 files changed, +14051/-809" — and a fact anyone can re-run
            # in a second is not fifty per cent likely. `confidence=0.5` was
            # hardcoded on 77 rows and `./observatory.py digest` prints that
            # number beside real judgements from 0.01 to 0.95, so a reviewer
            # read a fabricated measurement as a writer's own doubt. NULL is the
            # honest third answer: THIS IS NOT A JUDGEMENT.
            confidence=None, evidence=evidence,
            provenance=[{"source": "observatory-log/Stop", "measured": True,
                         **({"continues": continues} if continues else {})}])
        has_why = bool(prior and prior["why"])
        # THE RECORD FIRST, the attribution after it. Reading the session's transcript
        # is best-effort and bounded (`ATTRIBUTION_SECONDS`); when it ran before the
        # append, a large transcript cost the turn its record — the Stop hook was
        # killed at its timeout with nothing written. Now a slow or failed
        # attribution only means the turn is asked about.
        rationale = None
        if not has_why:
            rationale = rationale_of(pathlib.Path(top or cwd), facts, args.transcript or None,
                                     time.monotonic() + ATTRIBUTION_SECONDS)
        if rationale:
            try:
                # A follow-up revision of the SAME record that links the rationale.
                result = L.append(
                    conn, owner=OWNER, kind="session", statement=statement, why=None,
                    memory_id=result["memoryId"], expected_revision=result["revision"],
                    project_id=project_id, session_id=args.session_id or None,
                    workflow_id=workflow_id, function="episodic", scope="project",
                    state="proposed", confidence=None,
                    evidence=evidence + [{"uri": f"file:{rationale['ref']}", "kind": "rationale",
                                          **({"anchor": rationale["anchor"]}
                                             if rationale.get("anchor") else {})}],
                    provenance=[{"source": "observatory-log/Stop", "measured": True,
                                 "rationale": rationale["source"],
                                 **({"continues": continues} if continues else {})}])
            except Exception as exc:                                               # noqa: BLE001
                print(f"the rationale link could not be added: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                rationale = None
        # ONCE PER RECORD, and never for a turn that wrote its own rationale. The
        # ask ran after every changed turn — 3,593 times in fourteen days on one
        # machine for 34 answers — although a session is ONE record revised per
        # turn, so every ask after the first was the same question again.
        ask = {"ask": False}
        if not has_why and rationale is None:
            ask = why_asks.ask_once(result["memoryId"], args.session_id, project_id)
        return out({"recorded": True, "memoryId": result["memoryId"],
                    "revision": result["revision"], "hasWhy": has_why,
                    "session": args.session_id, "ask": ask["ask"],
                    **({"askJournal": ask["journal"]} if "journal" in ask else {}),
                    # Where the prompt hook records that it DELIVERED the ask: the
                    # answer rate is measured on delivered asks only.
                    **({"journal": str(why_asks.JOURNAL)} if ask["ask"] else {}),
                    "rationale": rationale,
                    "continues": continues,
                    "project": project_id, "files": len(facts["files"]),
                    "insertions": facts["insertions"], "deletions": facts["deletions"],
                    "unpushed": len(facts["unpushed"]),
                    # Knowledge freshness AT THE MOMENT OF WORK — the one moment
                    # an agent can act on it for free, being already inside the
                    # project. The board's aggregated rows (`project.graph_stale`,
                    # `project.wiki_stale`) tell the operator later; this tells
                    # the agent NOW, and the ask-why hook turns it into words.
                    # None means "does not apply": a project with no graph has
                    # not adopted graphify, and that is a choice, not staleness
                    #.
                    **_knowledge_age(pathlib.Path(top or cwd))},
                   {"whyAsks": _why_asks(conn)})
    except Exception as exc:
        return _fault(exc)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:                                                                
        # THROUGH `out`, so the receipt exists for the one path that most needs
        # it. A bare print here left the outer failure — the class that swallowed
        # `IllegalTransition` for fifteen hours — as the only outcome with no
        # record at all.
        _fault(exc)
        raise SystemExit(0)
