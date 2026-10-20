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
import argparse, hashlib, hmac, json, os, re, shlex, sqlite3, stat, subprocess, sys, time, pathlib
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic                                                                    
import paths                                                                     
import safe_git                                                                  
import why_asks                                                                  
sys.path.insert(1, str(pathlib.Path(__file__).resolve().parent))
import turn_state                                                                

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
    # A BOUNDED HOOK, not a broken one. The recorder's git calls share one budget
    # (`GIT_SECONDS`) so a slow checkout cannot hold the Stop hook to its 20-second
    # kill; a turn that runs out is left unrecorded, silently, with the reason in the
    # receipt. The next turn compares against the last snapshot it kept, so nothing
    # the skipped turn changed is lost: it is counted then.
    "git timed out",
)

#: The whole recorder's git budget, and one call's ceiling within it. Measured
#: 2026-10-20 on one machine: median Stop 838 ms, p90 2.4 s, max 20 s (the hook's
#: own kill), with `git status` alone at 0.45-1.9 s in a checkout with submodules.
GIT_SECONDS = 8.0
GIT_CALL_SECONDS = 5.0


#: Set by `main` before anything is written, so `out` can stamp them on
#: every path including the outer exception handler. `_cwd` is here for the same
#: reason the session id is: a lost turn's first question is WHICH PROJECT, and
#: the outer handler has no arguments left to read it from.
_session_id = ""
_cwd = ""
#: Set by `main`: the monotonic time the git budget ends, and whether a call ran
#: into it. None outside a run (a caller importing `git`).
_git_deadline: float | None = None
_git_timed_out = False


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
    reason = payload.get("reason") or ""
    # THE FAULT TRAVELS TO THE HOOK TOO. `ask-why.py` turns a fault into a Stop
    # notice and stays quiet on a quiet turn; with the mark only in the receipt
    # below, that branch could never run. A boolean: no path, no secret.
    payload = {**payload, "fault": bool(reason) and not any(k in reason for k in NOT_A_FAULT)}
    print(json.dumps(payload, ensure_ascii=False))
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
               "session": _session_id or ""}
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
    fsmonitor, clean filters and index hooks each time.

    BOUNDED: every call takes what is left of `GIT_SECONDS`, at most
    `GIT_CALL_SECONDS`. A call with no time left, or one that runs out, answers ""
    and sets `_git_timed_out`, which `main` turns into an unrecorded turn rather than
    a record built from half the facts."""
    global _git_timed_out
    timeout = GIT_CALL_SECONDS
    if _git_deadline is not None:
        left = _git_deadline - time.monotonic()
        if left <= 0.05:
            _git_timed_out = True
            return ""
        timeout = min(timeout, left)
    try:
        p = safe_git.run(args, repo=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        _git_timed_out = True
        return ""
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


def _numstat(raw: str) -> dict[str, tuple[int, int]]:
    """`{path: (insertions, deletions)}` from `git diff --numstat -z --no-renames`;
    a binary file counts 0/0."""
    out: dict[str, tuple[int, int]] = {}
    for rec in raw.split("\0"):
        parts = rec.split("\t", 2)
        if len(parts) == 3 and parts[2]:
            out[parts[2]] = (int(parts[0]) if parts[0].isdigit() else 0,
                             int(parts[1]) if parts[1].isdigit() else 0)
    return out


def diff_facts(cwd: pathlib.Path) -> dict:
    """What changed, from git rather than from anything the agent says.

    `files` is every entry of `git status` here; `main` narrows it to the entries
    this session changed (`turn_state`) and recounts the lines with `numstat`."""
    entries = turn_state.parse_status(git(cwd, *turn_state.STATUS_ARGS))
    numstat = _numstat(git(cwd, "diff", "HEAD", "--numstat", "-z", "--no-renames",
                           "--ignore-submodules=dirty"))
    ins = sum(a for a, _ in numstat.values())
    dele = sum(d for _, d in numstat.values())
    files = sorted({p for _, p in entries})
    # A deleted file holds no rationale: removing `docs/DECISIONS.md` is not
    # writing one, so a `D` in either column takes the file out of the reading.
    deleted = sorted({p for xy, p in entries if "D" in xy})
    untracked = sorted({p for xy, p in entries if xy == turn_state.UNTRACKED})
    if any(u.endswith("/") for u in untracked):
        # `status` folds an untracked directory into one `?? docs/` line, which
        # hides the register inside it. Only the files a rationale can live in are
        # asked for, so a large untracked tree costs a walk, not a listing.
        untracked = sorted(set(untracked) | {l.strip() for l in git(
            cwd, "ls-files", "--others", "--exclude-standard", "--",
            ":(glob,icase)**/decisions.md", ":(glob,icase)**/adr/**",
            ".task-pipeline/run.md").splitlines() if l.strip()})
    head = git(cwd, "rev-parse", "--short", "HEAD").strip()
    head_full = git(cwd, "rev-parse", "HEAD").strip()
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
            "base": "@{u}" if unpushed else "HEAD",
            "entries": entries, "numstat": numstat, "headFull": head_full}


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

ANCHOR_CHARS = 120
#: The longest heading text that may be linked. A longer heading is never linked
#: (the turn asks): the stored anchor is then always the full heading, never cut.
LINK_HEADING_CHARS = ANCHOR_CHARS
#: The longest raw heading or ledger line, in bytes, that may be linked.
LINK_LINE_BYTES = 1024
#: The fields a rationale link stores and a carry re-checks (`_link_fields`).
LINK_KEYS = ("anchor", "line", "level", "length", "digest", "appendable")
#: An untracked or ignored ledger or register is read whole, so it is read bounded.
READ_LIMIT = 256 * 1024
#: The largest register a carried link is re-checked against, read whole.
CARRY_READ_LIMIT = 8 << 20
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
#: Command separators: a shell command is split on these into segments, once.
_SEGMENT = re.compile(r"[;|&\n]")
#: The most of one command that is scanned. A longer command (a pasted runbook on one
#: line) is not attributed — the turn is asked about — so no command can cost more
#: than a linear pass over this many bytes.
COMMAND_SCAN_BYTES = 64 * 1024
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


def _lines(text: str) -> list[str]:
    """A register's lines: split on newline only, a trailing carriage return dropped.

    Not `str.splitlines`, which also splits on U+2028, U+0085 and form feeds, so a
    line no Markdown reader sees as a heading became one here."""
    return [l[:-1] if l.endswith("\r") else l for l in text.split("\n")]


def _register_path(top: pathlib.Path, rel: str) -> pathlib.Path | None:
    """The register at `rel` when it may be read as this checkout's own, else None.

    The one gate before ANY read of a register or the run ledger, for a fresh
    attribution and for a carried link alike. Refused: a symbolic link, a path under
    one (`resolve()` differs), a path that resolves outside the checkout, and
    anything that is not a regular file."""
    try:
        root = top.resolve()
        named = root / rel
        if named.is_symlink() or named.resolve() != named or root not in named.parents:
            return None
        if not stat.S_ISREG(named.lstat().st_mode):
            return None
        return named
    except (OSError, ValueError, RuntimeError):
        return None


def _read_register(top: pathlib.Path, rel: str, limit: int, mode: str = "whole") -> bytes | None:
    """The bytes of a register or the run ledger, through the gate, or None.

    The path passes `_register_path`, then the file is opened once without following
    a link and without blocking (a FIFO), and the DESCRIPTOR is checked: a regular
    file with one hard link. So a swap between the check and the read reads nothing.
    `mode` is "whole" (refused when larger than `limit`), "head" (the first `limit`
    bytes) or "tail" (the last `limit` bytes)."""
    named = _register_path(top, rel)
    if named is None:
        return None
    try:
        import osprivacy
        fd = osprivacy.open(str(named), os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
    except (OSError, ImportError):
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            return None
        if mode == "tail" and st.st_size > limit:
            os.lseek(fd, st.st_size - limit, os.SEEK_SET)
        want = limit + 1 if mode == "whole" else limit
        chunks, got = [], 0
        while got < want:
            b = os.read(fd, min(1 << 20, want - got))
            if not b:
                break
            chunks.append(b)
            got += len(b)
        data = b"".join(chunks)
        if mode == "whole" and len(data) > limit:
            return None
        return data
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_bounded(top: pathlib.Path, rel: str) -> list[str]:
    data = _read_register(top, rel, READ_LIMIT, "head")
    return [] if data is None else _lines(data.decode("utf-8", errors="replace"))


def _added_lines(top: pathlib.Path, rel: str, facts: dict,
                 deadline: float | None = None) -> list[str]:
    """The non-blank lines this work added to `rel`: the whole file when git does not
    track it, else what `git diff <base>` adds — the uncommitted change and the
    unpushed commits together — ignoring whitespace, so re-indenting a register
    adds nothing. The diff gets only the time left before `deadline`; a diff that
    needs longer (a register of megabytes) raises `_OutOfTime`, and the turn is
    asked about."""
    if rel in facts.get("untracked", ()):
        lines = _read_bounded(top, rel)
    else:
        left = 20.0 if deadline is None else deadline - time.monotonic()
        if left <= 0:
            raise _OutOfTime()
        try:
            p = safe_git.run(["diff", "--ignore-all-space", "--ignore-blank-lines",
                              facts.get("base") or "HEAD", "--", rel], repo=top, timeout=left)
        except subprocess.TimeoutExpired:
            raise _OutOfTime() from None
        except Exception:                                                          # noqa: BLE001
            return []
        diff = p.stdout if p.returncode == 0 else ""
        lines = [l[1:] for l in _lines(diff) if l.startswith("+") and not l.startswith("+++")]
    return [l for l in lines if l.strip()]


class _OutOfTime(Exception):
    """Attribution passed its budget: the turn is asked about."""


def _check(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise _OutOfTime()


def _shell_targets(command: str) -> list[str] | None:
    """The files a shell command WRITES: redirections, `tee`, `sed -i`. A command that
    only reads (`grep 'x' docs/DECISIONS.md`) writes nothing.

    Linear in the command: the redirection and `tee` patterns run once over it, and
    `sed -i` is read by splitting the command into segments on `; | & newline` and
    tokenizing only segments whose first word is `sed`. A command longer than
    `COMMAND_SCAN_BYTES` returns None — not scanned — and the caller asks.
    """
    if len(command) > COMMAND_SCAN_BYTES:
        return None
    out = [m.group(2) for m in _REDIRECT.finditer(command)]
    out += [m.group(2) for m in _TEE.finditer(command)]
    for segment in _SEGMENT.split(command):
        head = segment.lstrip()
        if not head.startswith("sed ") and not head.startswith("sed\t"):
            continue
        try:
            words = shlex.split(head)
        except ValueError:
            words = head.split()
        if any(w == "-i" or w.startswith("-i") or w.startswith("--in-place") for w in words[1:]) \
                and len(words) > 2 and not words[-1].startswith("-"):
            out.append(words[-1])
    return out


def _resolve(target: str, cwd: str | None, top: pathlib.Path) -> str:
    target = os.path.expanduser(target)
    base = cwd if cwd and os.path.isabs(cwd) else str(top)
    return os.path.realpath(os.path.join(base, target))


def session_writes(transcript: str | None, top: pathlib.Path, deadline: float,
                   since: int = 0) -> list[dict]:
    """This session's WRITING tool calls, from one bounded pass over its transcript.

    Each is `{"targets": {real path, …}, "text": str, "ledger": bool}`: an
    Edit/Write/MultiEdit/NotebookEdit with the file it names and the text it wrote,
    or a Bash command with the files it writes (`_shell_targets`) and its text.
    `ledger` is true for a call that writes the run ledger, including task-pipeline's
    `stage_checkpoint.py`, which names no file. Only the transcript's last
    `TRANSCRIPT_TAIL` bytes are read, once, and only lines carrying a `tool_use` are
    parsed. Raises `_OutOfTime` past the deadline. `since` is a byte offset of the
    transcript: calls written before it are not read.
    """
    if not transcript:
        return []
    try:
        with open(transcript, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - TRANSCRIPT_TAIL, since)
            fh.seek(start)
            data = fh.read()
            if start and not data.startswith(b"{"):
                data = data.split(b"\n", 1)[1] if b"\n" in data else b""
    except OSError:
        return []
    ledger = os.path.realpath(str(top / RUN_LEDGER))
    calls: list[dict] = []
    failed: set[str] = set()
    uses: list[tuple[str, dict, str | None]] = []
    for n, line in enumerate(data.split(b"\n")):
        if n % 256 == 0:
            _check(deadline)
        if b'"tool_use"' not in line and b'"is_error"' not in line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, RecursionError):
            continue                     # a torn or absurdly nested line: not a call
        # A subagent's line (`isSidechain`) is another context's work; it does not
        # count for this session, so a subagent's edit is asked about.
        if not isinstance(rec, dict) or rec.get("isSidechain"):
            continue
        content = (rec.get("message") or {}).get("content")
        if rec.get("type") == "user":
            # A call whose result is an error wrote nothing.
            for item in content if isinstance(content, list) else ():
                if isinstance(item, dict) and item.get("type") == "tool_result" \
                        and item.get("is_error") and isinstance(item.get("tool_use_id"), str):
                    failed.add(item["tool_use_id"])
            continue
        if rec.get("type") != "assistant":
            continue
        cwd = rec.get("cwd") if isinstance(rec.get("cwd"), str) else None
        for item in content if isinstance(content, list) else ():
            if isinstance(item, dict) and item.get("type") == "tool_use":
                uses.append((str(item.get("id") or ""), item, cwd))
    for use_id, item, cwd in uses:
        _check(deadline)
        if use_id and use_id in failed:
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
            found = _shell_targets(cmd)
            if found is None:
                continue                          # too long to scan: not attributed
            targets = {_resolve(t, cwd, top) for t in found}
            writes_ledger = ledger in targets or bool(_LEDGER_WRITER.search(cmd))
            if targets or writes_ledger:
                calls.append({"targets": targets, "text": cmd, "ledger": writes_ledger})
    return calls


def _atx(line: str) -> tuple[int, str] | None:
    """(level, text) when `line` is a Markdown ATX heading, else None. Linear, no regex.

    Up to three spaces of indent (a tab, or four spaces, is code), one to six `#`,
    then the end of the line or a space or tab and the text. A closing run of `#` is
    dropped only when a space or tab precedes it, or when it is the whole text, so
    `## Use C#` keeps its `#`."""
    i, n = 0, len(line)
    while i < n and line[i] == " ":
        i += 1
    if i > 3 or (i < n and line[i] == "\t"):
        return None
    j = i
    while j < n and line[j] == "#":
        j += 1
    level = j - i
    if level < 1 or level > 6:
        return None
    if j < n and line[j] not in " \t":
        return None
    text = line[j:].strip(" \t")
    k = len(text)
    while k > 0 and text[k - 1] == "#":
        k -= 1
    if k == 0:
        text = ""
    elif k < len(text) and text[k - 1] in " \t":
        text = text[:k].rstrip(" \t")
    return level, text


def _fence(line: str) -> tuple[str, int, bool] | None:
    """(character, length, has info string) when `line` opens or closes a fence."""
    i, n = 0, len(line)
    while i < n and line[i] == " ":
        i += 1
    if i > 3 or i >= n or line[i] not in "`~":
        return None
    ch, j = line[i], i
    while j < n and line[j] == ch:
        j += 1
    if j - i < 3:
        return None
    info = line[j:].strip(" \t")
    if ch == "`" and "`" in info:
        return None
    return ch, j - i, bool(info)


def _headings(lines: list[str], deadline: float | None = None) -> list[tuple[int, str, int]]:
    """(level, text, line index) of every ATX heading a Markdown reader would see.

    Used only to CHOOSE the anchor of a fresh link; a carried link is checked by
    bytes (`_link_holds`), never by this.

    One linear pass: lines inside a fenced code block (closed only by the same
    character, at least as long, with no info string; an unclosed fence runs to the
    end) and inside an HTML comment that spans lines are skipped."""
    out: list[tuple[int, str, int]] = []
    fence: tuple[str, int] | None = None
    in_comment = False
    for index, line in enumerate(lines):
        if deadline is not None and index % 4096 == 0:
            _check(deadline)
        if fence is not None:
            f = _fence(line)
            if f and f[0] == fence[0] and f[1] >= fence[1] and not f[2]:
                fence = None
            continue
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        stripped = line.lstrip(" ")
        if len(line) - len(stripped) <= 3 and stripped.startswith("<!--"):
            in_comment = "-->" not in stripped[4:]
            continue
        f = _fence(line)
        if f:
            fence = (f[0], f[1])
            continue
        h = _atx(line)
        if h and h[1]:
            out.append((h[0], h[1], index))
    return out


def _entry_heading(added: list[str]) -> str | None:
    """The anchor of the entry this work added: its SHALLOWEST heading, the newest of
    that level. A Context / Decision / Consequences entry anchors on its own title
    (`## DEC-0002 …`), never on a subheading every older entry repeats; a new file's
    `# Decisions` title wins only when it is the only level present."""
    heads = [(level, text) for level, text, _ in _headings(added)]
    if not heads:
        return None
    top = min(level for level, _ in heads)
    entries = [text for level, text in heads if level == top]
    # A new register's `# Title` above its entries is not a decision: a lone
    # level-one heading with deeper headings below defers to the next level.
    if top == 1 and len(entries) == 1 and any(level > top for level, _ in heads):
        nxt = min(level for level, _ in heads if level > top)
        entries = [text for level, text in heads if level == nxt]
    return entries[-1]


def _closed_at_end(text: list[str], deadline: float | None = None) -> bool:
    """True when the file ends outside any fenced code block and HTML comment, the
    same states `_headings` tracks. A link to a file that ends inside one is carried
    only while the file is byte-identical: an append could not be told apart from
    more of the open block."""
    fence: tuple[str, int] | None = None
    in_comment = False
    for index, line in enumerate(text):
        if deadline is not None and index % 4096 == 0:
            _check(deadline)
        if fence is not None:
            f = _fence(line)
            if f and f[0] == fence[0] and f[1] >= fence[1] and not f[2]:
                fence = None
            continue
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        stripped = line.lstrip(" ")
        if len(line) - len(stripped) <= 3 and stripped.startswith("<!--"):
            in_comment = "-->" not in stripped[4:]
            continue
        f = _fence(line)
        if f:
            fence = (f[0], f[1])
    return fence is None and not in_comment


def _link_fields(data: bytes, raw: list[bytes], text: list[str], index: int, level: int,
                 deadline: float | None = None) -> dict | None:
    """The fields a link is checked by on every later turn, for the line at `index`
    of a file whose bytes are `data`: the raw line (at most LINK_LINE_BYTES, UTF-8),
    the entry's heading level (0 for a ledger line), and the length and SHA-256 of
    the WHOLE file, plus whether a later entry may be appended (the file ends in a
    newline, outside any fence and comment, and the entry is a heading). No Markdown
    boundary is guessed: `_link_holds` compares bytes."""
    line = raw[index]
    if len(line) > LINK_LINE_BYTES:
        return None
    try:
        decoded = line.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if deadline is not None:
        _check(deadline)
    appendable = bool(level) and data.endswith(b"\n") and _closed_at_end(text, deadline)
    if deadline is not None:
        _check(deadline)
    return {"line": decoded, "level": level, "length": len(data),
            "digest": hashlib.sha256(data).hexdigest(), "appendable": appendable}


def _split(data: bytes, deadline: float | None) -> tuple[list[bytes], list[str]]:
    raw = data.split(b"\n")
    if deadline is not None:
        _check(deadline)
    text = [l.decode("utf-8", errors="replace") for l in raw]
    text = [t[:-1] if t.endswith("\r") else t for t in text]
    if deadline is not None:
        _check(deadline)
    return raw, text


def _heading_link(top: pathlib.Path, rel: str, anchor: str,
                  deadline: float | None = None) -> dict | None:
    """The link fields for the register heading that reads `anchor`, when exactly one
    heading a Markdown reader would see reads it and the heading is short enough to
    link. None otherwise: the turn is asked about."""
    if len(anchor) > LINK_HEADING_CHARS:
        return None
    data = _read_register(top, rel, CARRY_READ_LIMIT)
    if data is None:
        return None
    raw, text = _split(data, deadline)
    hits = [(level, index) for level, heading, index in _headings(text, deadline) if heading == anchor]
    if len(hits) != 1:
        return None
    return _link_fields(data, raw, text, hits[0][1], hits[0][0], deadline)


def _ledger_link(top: pathlib.Path, line: str, deadline: float | None = None) -> dict | None:
    """The link fields for the run-ledger line `line`, when it occurs exactly once."""
    data = _read_register(top, RUN_LEDGER, CARRY_READ_LIMIT)
    if data is None:
        return None
    raw, text = _split(data, deadline)
    want = line.encode("utf-8")
    hits = [i for i, l in enumerate(raw) if l == want or l == want + b"\r"]
    return _link_fields(data, raw, text, hits[0], 0, deadline) if len(hits) == 1 else None


def _appended_entry(rest: bytes, level: int) -> bool:
    """`rest` (what follows the linked file's bytes) starts, at its first non-blank
    line, with a column-0 ATX heading of `level` or shallower: a new entry."""
    for line in rest.split(b"\n"):
        if not line.strip():
            continue
        if not line.startswith(b"#"):
            return False
        h = _atx(line.decode("utf-8", errors="replace").rstrip("\r"))
        return bool(h and h[1]) and h[0] <= level
    return True


def _link_holds(top: pathlib.Path, rel: str, link: dict) -> bool:
    """A carried link is still true, by BYTES and without guessing where an entry ends.

    The register passes the gate, its stored raw line occurs exactly once as a whole
    line, and either the file is byte-identical to the linked one, or its first
    `length` bytes are (same SHA-256) and what follows starts, at its first non-blank
    line, with a column-0 heading no deeper than the entry's own: a new entry
    appended after it, allowed only when the linked file ended in a newline outside
    any fence and comment. Any other change asks again."""
    length, digest, line = link.get("length"), link.get("digest"), link.get("line")
    level = link.get("level") if isinstance(link.get("level"), int) else 0
    data = _read_register(top, rel, CARRY_READ_LIMIT)
    if data is None or not isinstance(length, int) or len(data) < length:
        return False
    want = line.encode("utf-8")
    if sum(1 for l in data.split(b"\n") if l == want) != 1:
        return False
    if not hmac.compare_digest(hashlib.sha256(data[:length]).hexdigest(), digest):
        return False
    if len(data) == length:
        return True
    return bool(link.get("appendable")) and _appended_entry(data[length:], level)


def _register_written(added: list[str], text: str) -> bool:
    """Does `text` — what this session's calls wrote INTO this file — carry the
    register's addition? Its entry heading (`_entry_heading`), or, when the addition
    has no heading, most of its added lines. One common line is not enough."""
    entry = _entry_heading(added)
    if entry is not None:
        # The entry's own heading must be in what the session wrote. Not any heading:
        # `### Consequences` is in every MADR entry, so it attributes nothing.
        return entry in text
    if any(l.lstrip(" ").startswith("#") for l in added):
        # Heading syntax the scanner did not accept (inside an unclosed fence, a
        # comment, indented as code): never fall back to matching lines. Ask.
        return False
    lines = [l.strip() for l in added if len(l.strip()) >= MIN_NEEDLE][:REGISTER_LINES]
    return bool(lines) and 2 * sum(1 for l in lines if l in text) > len(lines)


def _tail_lines(top: pathlib.Path, rel: str, limit: int) -> list[str]:
    data = _read_register(top, rel, limit, "tail")
    if data is None:
        return []
    lines = _lines(data.decode("utf-8", errors="replace"))
    return lines[1:] if len(data) >= limit and lines else lines


def rationale_of(top: pathlib.Path, facts: dict, transcript: str | None = None,
                 deadline: float | None = None, since: int = 0) -> dict | None:
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
        ledger_file = _register_path(top, RUN_LEDGER)
        if not candidates and ledger_file is None:
            return None
        calls = session_writes(transcript, top, deadline, since)
        if not calls:
            return None
        subs = _submodules(top) if candidates else []
        for rel in candidates:
            _check(deadline)
            if _vendored(rel, subs):
                continue
            named = _register_path(top, rel)
            if named is None:
                continue                              # a symlink, or not a regular file
            real = str(named)
            text = "\n".join(c["text"] for c in calls if real in c["targets"])
            if not text:
                continue
            added = _added_lines(top, rel, facts, deadline)
            if not added or not _register_written(added, text):
                continue
            # The LAST heading added: a register grows at its end, and a new
            # register's first heading is its title, not a decision.
            anchor = _entry_heading(added)
            if anchor is None:
                continue                    # no heading: nothing a link can hold to
            # The entry is in the register NOW, once, as a heading, and short enough
            # to link; the link stores its raw line and the digest a carry re-checks.
            fields = _heading_link(top, rel, anchor, deadline)
            if fields is None:
                continue
            _check(deadline)
            return {"source": "decision-register", "ref": rel, "anchor": anchor, **fields}
        ledger_text = "\n".join(c["text"] for c in calls if c["ledger"])
        if ledger_text and ledger_file is not None:
            _check(deadline)
            tracked_change = RUN_LEDGER in touched and RUN_LEDGER not in (facts.get("untracked") or ())
            lines = (_added_lines(top, RUN_LEDGER, facts, deadline) if tracked_change
                     else _tail_lines(top, RUN_LEDGER, LEDGER_TAIL))
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
                    fields = _ledger_link(top, line, deadline)
                    if fields is None:
                        return None
                    _check(deadline)
                    return {"source": "task-pipeline", "ref": RUN_LEDGER,
                            "anchor": line.strip()[:ANCHOR_CHARS], **fields}
    except _OutOfTime:
        return None
    except Exception:                                                              # noqa: BLE001
        return None
    return None


def _dropped_since(prior, repo_id: str, top: pathlib.Path, held: list[dict]) -> int:
    """The transcript offset after which a FRESH attribution may look: the highest of
    the offsets stored with links of the latest revision that no longer hold, and of
    the `rationale-dropped` marks earlier revisions carried forward.

    Without this a dropped link came straight back: the session's old write of the
    entry is still in its transcript, so a fresh attribution re-linked an entry that
    another writer has since withdrawn, wrapped or moved. The mark is written forward
    on every later revision (see `main`), so it holds for every turn after the drop,
    not only the next one."""
    if prior is None:
        return 0
    try:
        ev = json.loads(prior["evidence_json"] or "[]")
    except (ValueError, TypeError, IndexError, KeyError):
        return 0
    at = 0
    for e in ev if isinstance(ev, list) else ():
        if not isinstance(e, dict):
            continue
        kind = e.get("kind")
        if (kind == "rationale" and e not in held) or kind == "rationale-dropped":
            v = e.get("transcript_at")
            at = max(at, v if isinstance(v, int) and v >= 0 else 1 << 62)
    return at


def _rationale_links(prior, repo_id: str, top: pathlib.Path) -> list[dict]:
    """The rationale links of the record's latest revision that are STILL TRUE.

    A link is carried only when this hook wrote it — the revision's provenance is
    `observatory-log/Stop` with a `rationale` key, so a link another writer put in
    the evidence is never carried — when it names this repository, and when its
    whole-file bytes still hold (`_link_holds`). A register deleted, reverted, checked out or
    rewritten, or a session that moved to another repository, drops the link, and
    the turn is attributed afresh (and asked about when that finds nothing).
    """
    if prior is None:
        return []
    try:
        ev = json.loads(prior["evidence_json"] or "[]")
        prov = json.loads(prior["provenance_json"] or "[]")
    except (ValueError, TypeError, IndexError, KeyError):
        return []
    if not any(isinstance(x, dict) and x.get("source") == "observatory-log/Stop"
               and x.get("rationale") for x in prov if isinstance(prov, list)):
        return []
    out = []
    for e in ev if isinstance(ev, list) else ():
        if not isinstance(e, dict) or e.get("kind") != "rationale" or e.get("repo") != repo_id:
            continue
        uri, line, digest = str(e.get("uri") or ""), e.get("line"), e.get("digest")
        # A link without its raw line, whole-file length and digest (written before
        # this check existed) is not carried: the turn is attributed afresh.
        if not uri.startswith("file:") or not isinstance(line, str) or not line \
                or not isinstance(digest, str) or len(digest) != 64 \
                or not isinstance(e.get("length"), int):
            continue
        if _link_holds(top, uri[len("file:"):], e):
            out.append(e)
    return out


def _latest_of(conn, own) -> tuple[dict | None, str | None]:
    """The record to revise: `own` (this session's newest `kind='session'` row) as it
    stands at its LATEST revision, whoever wrote that revision. `(prior, foreign)`.

    **The defect this closes.** The ask tells the agent to answer with
    `observatory_record(owner, memory_id, expected_revision, statement, why)`. That
    tool writes the answer as `kind='note'` with no session id, so a lookup keyed on
    `session_id` and `kind='session'` never saw it, the next changed turn revised
    with a stale `expected_revision`, and every later turn of the session ended in
    `RevisionConflict`: answering the ask stopped the session's recording.

    The answer's revision, state, `why` and workflow are taken; the statement,
    evidence and provenance stay this hook's own, so "unchanged since the last turn"
    still compares what this hook wrote, and a rationale link is carried only when
    this hook wrote it. A latest revision that names ANOTHER project is not revised:
    `prior` is None and `foreign` names the record, which the fresh record
    `continues` (an answer that names no project is this session's)."""
    if own is None:
        return None, None
    latest = conn.execute(
        "SELECT revision, why, state, workflow_id, project_id FROM ledger"
        " WHERE memory_id = ? ORDER BY revision DESC LIMIT 1",
        (own["memory_id"],)).fetchone()
    if latest is None or latest["revision"] == own["revision"]:
        return own, None
    if latest["project_id"] is not None and latest["project_id"] != own["project_id"]:
        return None, own["memory_id"]
    return {**{k: own[k] for k in own.keys()},
            "revision": latest["revision"], "why": latest["why"],
            "state": latest["state"], "workflow_id": latest["workflow_id"]}, None


def _transcript_size(transcript: str | None) -> int:
    try:
        return os.path.getsize(transcript) if transcript else 0
    except OSError:
        return 0


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
    global _session_id, _cwd, _git_deadline
    _session_id = args.session_id or ""
    _cwd = args.cwd or ""
    _git_deadline = time.monotonic() + GIT_SECONDS

    cwd = pathlib.Path(args.cwd)
    if not cwd.is_dir():
        return out({"recorded": False, "reason": "cwd does not exist"})
    if not git(cwd, "rev-parse", "--git-dir").strip():
        return out({"recorded": False, "reason": "not a git repository"})

    repo_id, top = repo_id_for(cwd)
    if _git_timed_out:
        return out({"recorded": False, "reason": f"git timed out after {GIT_SECONDS:g} s"})
    if repo_id is None:
        return out({"recorded": False,
                    "reason": "this repository is not in the registry; run "
                              "`project-observatory full local` to add it"})
    top_path = pathlib.Path(top or cwd)
    facts = diff_facts(top_path)
    if _git_timed_out:
        # Nothing is saved either: the next turn compares against the last snapshot
        # this session kept, so what this turn changed is counted then.
        return out({"recorded": False, "reason": f"git timed out after {GIT_SECONDS:g} s"})

    # THIS TURN'S CHANGES, NOT THE TREE'S STATE (`turn_state`). The recorder took
    # every `git status` entry as the turn's work, so standing untracked files made
    # every turn of a session "changed": 35 asks in one session about the same
    # "4 files changed, +0/-0" (measured 2026-10-20). Now the session's files are
    # those that differ from its baseline, a turn counts only when something
    # differs from the previous turn's snapshot, and only a change to TRACKED
    # content (or a moved HEAD) is asked about.
    turn: dict = {"changed": None, "tracked": None}
    keep = None
    if args.session_id:
        current = turn_state.snapshot(top_path, facts["entries"], facts["headFull"])
        held = turn_state.load(args.session_id, top_path)
        if held is None:
            baseline = turn_state.fallback(current)
            turn = turn_state.compare({"head": None, "entries": baseline}, current,
                                      head_moved=bool(facts["unpushed"]))
        else:
            baseline = held["baseline"]
            turn = turn_state.compare(held["last"], current)
        keep = (baseline, current)
        facts["files"] = turn_state.session_paths(baseline, current, facts["entries"])
        mine = [facts["numstat"][f] for f in facts["files"] if f in facts["numstat"]]
        facts["insertions"] = sum(a for a, _ in mine)
        facts["deletions"] = sum(d for _, d in mine)

    def settle() -> None:
        """Keep this turn's snapshot as the next turn's comparison point."""
        if keep is not None:
            turn_state.save(args.session_id, top_path, *keep)

    if not facts["files"] and not facts["unpushed"]:
        settle()
        return out({"recorded": False, "reason": "nothing changed"})
    if turn["changed"] == 0:
        settle()
        return out({"recorded": False, "reason": "nothing changed this turn"})

    from store import db as store_db
    from store import ledger as L
    project_id, ownership = project_for(repo_id)
    if not estate.records_events(ownership):
        settle()
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
        prior, foreign = None, None
        if args.session_id:
            # `NOT IN tombstones`, because a record an erasure has closed takes
            # no further revisions and this hook runs on every turn
            # of every session. Without it the append would raise and the hook
            # would report `recorded: false` for the rest of the session; with
            # it the turn starts a fresh record, which is what an erasure means.
            #
            # THE NEWEST ROW THIS SESSION WROTE, by creation order (`created_at`,
            # then `rowid`, which is append order), never the highest revision
            # number. A record this session starts after a promotion, a rejection
            # or a takeover by another project begins at revision 1, below the
            # record it continues; ordered by revision, the next turn picked the
            # old record again, started yet another one and asked again on every
            # changed turn.
            row = conn.execute(
                "SELECT l.memory_id, l.revision, l.why, l.statement, l.state, l.workflow_id,"
                " l.evidence_json, l.provenance_json, l.project_id"
                " FROM ledger l"
                " WHERE l.session_id = ? AND l.kind = 'session'"
                "   AND l.memory_id NOT IN (SELECT memory_id FROM tombstones)"
                " ORDER BY l.created_at DESC, l.rowid DESC LIMIT 1",
                (args.session_id,)).fetchone()
            prior, foreign = _latest_of(conn, row if row and row["memory_id"] else None)
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
        continues = foreign
        if prior is not None and (prior["state"] or "") != "proposed":
            continues = prior["memory_id"]
            prior = None
        if prior is not None and prior["statement"] == statement \
                and (workflow_id is None or prior["workflow_id"] == workflow_id):
            # Nothing moved since the last turn. Appending here would add a
            # revision that says exactly what the previous one said, and
            # append-only is a reason to be careful about what gets appended.
            settle()
            return out({"recorded": False, "reason": "unchanged since the last turn",
                        "memoryId": prior["memory_id"], "revision": prior["revision"],
                        "hasWhy": bool(prior["why"]), "project": project_id},
                       {"whyAsks": _why_asks(conn)})
        # THE LINK IS CARRIED FORWARD. Once a revision of this record links the
        # rationale this session wrote, every later revision keeps that link and the
        # record stays explained — after the register is committed and pushed, and
        # after the write scrolls out of the transcript's tail.
        carried = _rationale_links(prior, repo_id, pathlib.Path(top or cwd))
        # THE DROP IS REMEMBERED: the high-water mark goes into this revision too, so
        # the next turn still refuses to re-link from writes made before the drop.
        since = _dropped_since(prior, repo_id, pathlib.Path(top or cwd), carried)
        if since:
            evidence = evidence + [{"kind": "rationale-dropped", "transcript_at": since}]
        base_evidence = list(evidence)
        if carried:
            evidence = evidence + [e for e in carried if e not in evidence]
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
                         **({"rationale": "carried"} if carried else {}),
                         **({"continues": continues} if continues else {})}])
        has_why = bool(prior and prior["why"])
        settle()                          # the record holds this turn; the next compares to it
        # THE RECORD FIRST, the attribution after it. Reading the session's transcript
        # is best-effort and bounded (`ATTRIBUTION_SECONDS`); when it ran before the
        # append, a large transcript cost the turn its record — the Stop hook was
        # killed at its timeout with nothing written. Now a slow or failed
        # attribution only means the turn is asked about.
        rationale = None
        # A FRESH LINK WINS. When this turn touched a register (or the run ledger),
        # it is attributed again, so a new decision replaces an older anchor; the
        # carried link stands only when nothing fresh is found.
        touched_register = any(_is_register(r) for r in
                               set(facts.get("files") or ()) | set(facts.get("committed") or ())
                               | set(facts.get("untracked") or ())) \
            or (pathlib.Path(top or cwd) / RUN_LEDGER).is_file()
        if not has_why and (not carried or touched_register):
            rationale = rationale_of(pathlib.Path(top or cwd), facts, args.transcript or None,
                                     time.monotonic() + ATTRIBUTION_SECONDS, since)
        if rationale is not None and carried and any(
                c["uri"] == f"file:{rationale['ref']}" and c.get("line") == rationale.get("line")
                and c.get("digest") == rationale.get("digest")
                and c.get("length") == rationale.get("length") for c in carried):
            rationale = None          # the same link, found again: carried, no new revision
        if rationale is None and carried:
            first = carried[-1]
            rationale = {"source": "carried", "ref": first["uri"][len("file:"):],
                         **{k: first[k] for k in LINK_KEYS if k in first}}
            # (`transcript_at` stays in the carried evidence entry itself.)
        # The follow-up revision only when the link is NEW: a carried link is already
        # in the revision just written, so a changed turn adds one revision, not two.
        if rationale and rationale["source"] != "carried":
            try:
                # A follow-up revision of the SAME record that links the rationale.
                result = L.append(
                    conn, owner=OWNER, kind="session", statement=statement, why=None,
                    memory_id=result["memoryId"], expected_revision=result["revision"],
                    project_id=project_id, session_id=args.session_id or None,
                    workflow_id=workflow_id, function="episodic", scope="project",
                    state="proposed", confidence=None,
                    evidence=base_evidence + [{"uri": f"file:{rationale['ref']}", "kind": "rationale",
                                               "repo": repo_id,
                                               "transcript_at": _transcript_size(args.transcript),
                                               **{k: rationale[k] for k in LINK_KEYS
                                                  if k in rationale}}],
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
        # AND ONLY FOR TRACKED CONTENT THIS TURN CHANGED: a new untracked file is
        # recorded with the facts but not asked about until it is added or committed.
        ask = {"ask": False}
        if not has_why and rationale is None and turn["tracked"]:
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
                    "turnChanged": turn["changed"], "turnTracked": turn["tracked"],
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
