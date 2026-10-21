#!/usr/bin/env python3
"""Look for the estate's own secrets where they do not belong.

    scan_leaks.py                 incremental: only bytes nobody has read yet
    scan_leaks.py --full          forget the offsets and read everything again
    scan_leaks.py --days N        how far back a transcript counts (default 14)

WHY. `tools/check_secrets.py` asks "does this tracked file contain something
SHAPED like a credential" — a good question with a known weakness: it cannot tell
this estate's live key from a plausible-looking string, so it is tuned not to cry
wolf and it only reads the tracked tree. This asks the other question, the one
that actually goes wrong in practice: **is a value we hold sitting somewhere
it should not be.** The typical leak is a value echoed into an agent session
transcript by a database client error or an HTTP library traceback — and
nothing else on the machine would notice it.

WHAT IT KNOWS. Every value the estate holds, gathered at the start of the run and
dropped at the end of it:

  env secrets   the `secret`-class variables `store/raw/env.json` lists, read
                back out of their files
  vault slots   `<vault dir>/<project>/<env>/<NAME>` (`OBSERVATORY_VAULT_DIR`)
  destinations  the files an OpenRouter key is installed into (`DESTINATIONS`)

READING THE VAULT DIRECTLY IS THE ONE EXCEPTION TO `handling-secrets`, and it is
deliberate: that rule addresses AGENTS, whose transcripts are the hazard. This is
a tool — it holds the values in memory for one pass, matches, and reports a NAME
and a FILE. It never prints a value, never writes one, and never quotes the
surrounding line, because a leak report that quotes the leak is a second copy of
it. You cannot detect the leak of a value you have no copy of; the alternative to
this exception is not a safer scanner, it is no scanner.

WHAT IT READS. Agent session transcripts — gigabytes over a few weeks on a busy
machine, which is why this is incremental: a JSONL transcript is append-only, so
a remembered offset makes the steady-state cost the size of what was said since
the last tick. A file that is rewritten in place (JSON, HTML, a tracked file) is
read whole again when its size or modification time moves. The transcript homes
are `paths.transcript_homes()` (I-5):

  claude    the configured `sessions` source, `*.jsonl`; `$CLAUDE_CONFIG_DIR`
            (~/.claude) `history.jsonl`
  codex     `$CODEX_HOME` (~/.codex) `sessions/` and `archived_sessions/`,
            `rollout-*.jsonl` and the compressed `rollout-*.jsonl.zst`;
            `history.jsonl`; and in `$CODEX_SQLITE_HOME` (default the same)
            `state_*`, `logs_*`, `thread_history_*`, `memories*`, `queue_*`,
            `goals_*`, `agent_message_board_*` `.sqlite`
  hermes    `$HERMES_HOME` (~/.hermes + `$HERMES_DATA_DIR_SUFFIX`) and each
            `profiles/<name>/`: the `state.db` SQLite store and
            `sessions/*.json|*.jsonl`
  opencode  `$XDG_DATA_HOME/opencode` (~/.local/share/opencode): `opencode*.db`
            (or `$OPENCODE_DB`) and the legacy `storage/**/*.json`

An absent home is not a degradation; one that exists and will not read is named
in `not_scanned`, and one the deadline cut is `partial` in coverage. Plus this
project's own logs, its built page, its tracked tree,
the session companion's SQLite store, and this engine's own store, where agent
memory lives.

WHAT IT DOES NOT DO. It does not write `leaks.jsonl`. That register is the
operator's debt ledger and a false entry there is expensive to withdraw — so a
hit leaves as a `secret.seen_outside_its_home` finding carrying the exact
`tools/vault.py leak` command, and a person or an agent confirms it. The scanner
is allowed to be noisy; the ledger is not.
"""
from __future__ import annotations
import argparse
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collectors"))
sys.path.insert(0, str(ROOT / "tools"))
import atomic              
import paths              
import safe_git  # noqa: E402  — the engine's one git door
import oslocks  # noqa: E402  — the engine's one door to fcntl (a descriptor's path)
import osprivacy  # noqa: E402  — opens that refuse a link on every OS
import sqlite_scan  # noqa: E402  — when a store may be read from its last mark (PB-131)
import step_budget  # noqa: E402  — the time this tick step has left (OBS-52)

#: Seconds kept back from the step's deadline: the two SQLite stores, then the answer itself. A file is
#: started only while more than FILE_RESERVE_S is left, a store only while more than STORE_RESERVE_S is.
STORE_RESERVE_S = 60.0
FILE_RESERVE_S = 2 * STORE_RESERVE_S
#: Seconds a store being read stops before the deadline, to write the answer. A file being read
#: stops at FILE_RESERVE_S, so a large transcript never takes the stores' time (OBS-52: a pass that
#: overran inside one unit was killed by its watchdog and wrote nothing, ten ticks in a row).
ANSWER_RESERVE_S = 15.0
#: What `scan_file` and `scan_zst` give as their problem when they stopped for the deadline: not a
#: hole (the file was readable), not clean either. The caller keeps what was read and stops.
OUT_OF_TIME = "out of time"


def out_of_time(reserve: float):
    """A stop check for a unit of work: True once fewer than `reserve` seconds are left."""
    return lambda: step_budget.remaining() < reserve

# Direct scanner callers may use names; main uses a name and version pair.
SecretRef = str | tuple[str, str | None]

#: Days a complete pass over the databases may stay pending across ticks before it starts over: a
#: pass spread over a long time reads its first rows long before its last (confirming review of #328).
PENDING_MAX_DAYS = 2

STATE = paths.SCRATCH / "leak-scan-state.json"
OUT = paths.SCRATCH / "leak-scan.json"
VAULT = pathlib.Path(os.environ.get(
    "OBSERVATORY_VAULT_DIR",
    paths.source_path("secret_store", paths.SECRETS) / 'projects'))
SESSIONS = pathlib.Path(os.environ.get(
    "OBSERVATORY_SESSIONS", paths.SESSIONS))

#: Below this a value is not searched for. A twelve-character password produces
#: matches on prose; the rule this protects is that a report nobody can trust is
#: a report nobody reads. Short values are COUNTED and named in `skipped`, never
#: dropped in silence.
MIN_LEN = 12


def searchable(value: str) -> bool:
    """Is a match on this value EVIDENCE, or just a coincidence?

    `scan_env.classify` calls a variable a secret when its NAME says so and the
    value is not obviously a word — which is right for an inventory and wrong
    here. A session-name variable can pass that test while its value is a short
    identifier that appears in ordinary text, and a scan built on it would
    report thousands of "leaks" in one transcript.

    So the leak hunt uses the stricter, NAME-BLIND test: a known issuer prefix, a
    URL carrying credentials, or real randomness. A value that fails it is named
    in `skipped` rather than dropped quietly — it is still a secret, it is just
    not one a text match can prove anything about.
    """
    import scan_env
    return len(value) >= MIN_LEN and scan_env.looks_secret(value)

#: How much of one file to read at a time, with an overlap so a value split
#: across two reads is still found.
CHUNK = 1 << 20

#: The four destinations `tools/install_key.py` owns, plus the observatory's own.
DESTINATIONS = {
    "observatory": paths.STATE / ".openrouter-key",
    # The legacy location, when the state is redirected (PB-091): a key left there
    # is still a key on this machine and must be seen.
    **({"observatory-legacy": paths.STORE / ".openrouter-key"} if paths.STATE != paths.STORE else {}),
    "claude-mem": paths.source_path("companion_home", paths.HOME / "disabled/companion") / ".env",
    "gateway": paths.source_path("secret_store", paths.SECRETS) / 'openrouter',
    "provisioning": paths.source_path("secret_store", paths.SECRETS) / 'openrouter-provisioning',
}


def _fd_path(fd: int) -> str | None:
    """The real, canonical path of an open descriptor (`oslocks.fd_path`: macOS
    `F_GETPATH`, Linux `/proc/self/fd`, Windows `GetFinalPathNameByHandleW`); None
    where the OS cannot say. Review round 14: through the engine's `fcntl` door, not
    a direct import, which the Windows/Linux boundary refuses."""
    return oslocks.fd_path(fd)


def _inside(real: str | None, within: pathlib.Path) -> bool:
    """Does the opened file's real path lie under `within`? Decided by IDENTITY:
    some ancestor of `real` is the same folder (device and inode) as `within`.

    Review round 8: comparing canonical path strings needed `within` opened for
    its path, and a folder this user may traverse but not list (mode 0311) will
    not open on macOS even with O_EVTONLY (measured); `stat` needs only the
    traversal right. A case variant of the folder's name is the same inode, so it
    compares equal (round 7). Anything unknown is False — a hole, never a guess.

    Review round 9: each ANCESTOR is read with `lstat`, never `stat`. `real` is the
    descriptor's canonical path, so every ancestor of it was a real folder when the
    path was reported; were one swapped for a link to `within` after the open,
    `stat` would follow that link and call an outside file inside. `lstat` reads the
    link itself, whose inode is not the folder's. `within` itself is still `stat`ed:
    a data folder reached through a link is that folder."""
    if real is None:
        return False
    try:
        target = os.stat(within)
    except (OSError, ValueError):
        return False
    for anc in pathlib.PurePath(real).parents:
        try:
            st = os.lstat(anc)
        except (OSError, ValueError):
            continue
        if (st.st_dev, st.st_ino) == (target.st_dev, target.st_ino):
            return True
    return False


class _Outside(Exception):
    """The opened file's real path is not under the folder it was meant to be in."""


def _read_regular(p: pathlib.Path, within: pathlib.Path | None = None) -> bytes | None:
    """A regular file's bytes, or None — `lstat`, then an `O_NOFOLLOW|O_NONBLOCK`
    open, then `fstat` on the descriptor (review round 6). A FIFO, a device or a
    link is never read: `os.stat` then a blocking open let a file swapped to a
    FIFO between the two calls hold the scan for ever. A path the OS refuses
    outright (a NUL in it: `ValueError`) is None too.

    `within` (review round 7): after the open, the DESCRIPTOR's real path must lie
    under that folder's canonical path. A parent folder swapped for a link
    between any path check and the open is caught here, where it cannot race;
    `_Outside` is raised so the caller can say so."""
    try:
        if not stat.S_ISREG(os.lstat(p).st_mode):
            return None
        fd = osprivacy.open(p, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return None
            # No guess (review round 8): a path the OS will not give is a hole,
            # never a `realpath` that a racing link can satisfy.
            if within is not None and not _inside(_fd_path(fh.fileno()), within):
                raise _Outside(p)
            return fh.read()
    except (OSError, ValueError):
        return None


def _read(p: pathlib.Path, within: pathlib.Path | None = None) -> str | None:
    """A regular file's text, or None (see `_read_regular`; `_Outside` propagates)."""
    raw = _read_regular(p, within)
    return None if raw is None else raw.decode("utf-8", errors="replace")


def vault_slots(notes: list[dict], *, max_entries: int = 100_000):
    """Enumerate only project/env/slot, with explicit discovery boundaries.

    Symlinks and unexpected types are not followed; unreadable ancestors must
    not make the readable sibling slots look like a complete inventory.
    """
    remaining = max_entries

    def hole(path, reason):
        relative = path.relative_to(VAULT).as_posix()
        notes.append({"what": "vault:" + relative, "unreadable": True,
                      "why": reason + "; values in this source are unmeasured"})

    def visit(path, depth):
        nonlocal remaining
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                hole(path, "symbolic link excluded without following")
            elif depth == 3:
                if stat.S_ISREG(info.st_mode):
                    yield path
                else:
                    hole(path, "not a regular slot file; excluded without reading")
            elif not stat.S_ISDIR(info.st_mode):
                hole(path, "not a directory; excluded without reading")
            else:
                with os.scandir(path) as entries:
                    for entry in entries:
                        if remaining <= 0:
                            hole(path, "vault discovery entry limit reached")
                            break
                        remaining -= 1
                        if depth == 2 and (entry.name.startswith(".") or entry.name == "meta.json"):
                            continue
                        yield from visit(pathlib.Path(entry.path), depth + 1)
        except FileNotFoundError:
            if depth != 0:
                hole(path, "disappeared before it could be examined")
        except OSError:
            hole(path, "could not be examined or listed")

    yield from visit(VAULT, 0)


def env_inventory(path: pathlib.Path) -> tuple[list[dict], str | None]:
    """The env inventory's files, validated, or [] and why it is a hole (review round 5).

    Read as a REGULAR FILE ONLY, through `lstat` and an `O_NOFOLLOW|O_NONBLOCK`
    open: `Path.exists()` called a dangling link, a link loop and a link into a
    locked folder "absent", and `read_text` on a FIFO never returned. Absent is
    the only silent answer; every other type, error or wrong shape is a hole.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return [], f"could not be examined ({type(exc).__name__})"
    if not stat.S_ISREG(info.st_mode):
        kind = "a symbolic link" if stat.S_ISLNK(info.st_mode) else "not a regular file"
        return [], f"{kind}; excluded without reading"
    raw = _read_regular(path)
    if raw is None:
        return [], "not a regular file, or could not be read; excluded"
    try:
        # `utf-8-sig`: a byte-order mark is accepted (review round 6).
        doc = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError) as exc:
        # RecursionError: `"[" * 200000` nests past the parser's stack (round 6).
        return [], f"could not be read ({type(exc).__name__})"
    files = doc.get("files") if isinstance(doc, dict) else None

    def text(v: object) -> bool:
        # A string the report can carry: no NUL, and no lone surrogate (`"\udcff"`
        # crashed the report's UTF-8 write, review round 7).
        if not isinstance(v, str) or "\x00" in v:
            return False
        try:
            v.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True

    ok = isinstance(files, list) and all(
        isinstance(f, dict) and text(f.get("path")) and text(f.get("project"))
        and isinstance(f.get("variables"), list)
        and all(isinstance(v, dict) and text(v.get("name")) and isinstance(v.get("class"), str)
                for v in f["variables"])
        for f in files)
    if not ok:
        return [], "its shape is not the env inventory's (files[].path, project, variables[].name, class)"
    return files, None


def known_values() -> tuple[dict[str, str], list[dict], list[dict]]:
    """value -> a name for it, plus the homes to exclude and what was skipped."""
    values: dict[str, str] = {}
    homes: list[dict] = []
    skipped: list[dict] = []
    import scan_env

    # 1. the env inventory's secrets, read back out of their own files
    files, problem = env_inventory(paths.SCRATCH / "env.json")
    if problem:
        # A HOLE, NOT A CRASH OR A HANG (review rounds 4 and 5): an env inventory
        # that is not a regular file, will not read, or has the wrong shape leaves
        # every env value unlooked-for.
        skipped.append({"what": "env inventory (env.json)", "unreadable": True,
                        "why": problem + "; no env value is looked for"})
    for f in files:
            wanted = {v["name"] for v in f["variables"] if v["class"] == "secret"}
            if not wanted:
                continue
            rel = pathlib.PurePosixPath(f["path"])
            if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                # Only a path inside the data folder is read: `/etc/hosts` or
                # `../x` in a damaged inventory names a file this scan must not open.
                skipped.append({"what": f["path"], "unreadable": True,
                                "why": "not a path inside the data folder; not read"})
                continue
            path = paths.DATA / f["path"]
            try:
                # A SYMLINKED FOLDER INSIDE THE DATA FOLDER (rounds 6 and 7): the
                # path's text stays inside, its target need not — and a path check
                # before the open races a folder swapped for a link. The OPEN
                # DESCRIPTOR's canonical path decides. A hard link to an outside
                # file cannot be seen by any path check (COMPATIBILITY).
                text = _read(path, within=paths.DATA)
            except _Outside:
                skipped.append({"what": f["path"], "unreadable": True,
                                "why": "its real path is outside the data folder, or could not be shown "
                                       "to be inside it; not read"})
                continue
            if text is None:
                # A HOLE, not a choice (review round 3): its values cannot be
                # looked for, so the scan is unmeasured for them.
                skipped.append({"what": f["path"], "why": "unreadable now", "unreadable": True})
                continue
            homes.append({"path": str(path)})
            for name, value in scan_env.parse(text)[0]:
                if name not in wanted:
                    continue
                if not searchable(value):
                    skipped.append({"what": f"{f['path']}:{name}",
                                    "why": ("shorter than 12 characters"
                                            if len(value) < MIN_LEN else
                                            "its shape is not distinctive enough for "
                                            "a text match to be evidence")})
                    continue
                # A LABEL THAT SAYS WHERE IT LIVES: `env:` and the project, so a
                # redaction marker or a finding names the home of the value.
                values.setdefault(value, f"env:{f['project']}/{name}")

    # 2. the vault's slots
    for slot in vault_slots(skipped):
        text = _read(slot)
        if text is None:
            skipped.append({"what": f"vault:{slot.parent.parent.name}/{slot.parent.name}/{slot.name}",
                            "why": "could not be read; its value is not looked for", "unreadable": True})
            continue
        value = text.strip()
        homes.append({"path": str(slot)})
        if not searchable(value):
            skipped.append({"what": f"vault:{slot.parent.parent.name}/{slot.name}",
                            "why": ("shorter than 12 characters"
                                    if len(value) < MIN_LEN else
                                    "its shape is not distinctive enough for a "
                                    "text match to be evidence")})
            continue
        # `vault:<project>/<env>/<NAME>`, the address the rest of the
        # engine uses for a slot (`openrouter.py --to`, the keyserver).
        values.setdefault(value, f"vault:{slot.parent.parent.name}/{slot.parent.name}/{slot.name}")

    # 3. the OpenRouter destinations
    for label, path in DESTINATIONS.items():
        text = _read(path)
        if text is None:
            continue
        homes.append({"path": str(path)})
        for raw in text.splitlines():
            raw = raw.strip()
            if "=" in raw and not raw.startswith("#"):
                raw = raw.split("=", 1)[1].strip().strip('"').strip("'")
            if raw.startswith("sk-") and len(raw) >= MIN_LEN:
                values.setdefault(raw, f"openrouter/{label}")
    return values, homes, skipped


def source_stat(path: pathlib.Path, notes: list[dict], *, required: bool = False):
    """Absent optional sources differ from a source whose metadata cannot be read.

    Path.exists/is_file suppress OSError on newer Python versions. Using them
    here turned permission failures into healthy empty surveys.
    """
    try:
        return path.stat()
    except FileNotFoundError:
        if not required:
            return None
        problem = "disappeared before it could be examined"
    except OSError:
        problem = "could not be examined"
    notes.append({"what": _label(path), "unreadable": True,
                  "why": problem + "; this source is unmeasured, not clean"})
    return None


def source_file_stat(path: pathlib.Path, notes: list[dict], *, required: bool = False):
    """Only ordinary files are scan targets; unsupported types remain visible.

    A FIFO must never be opened by this scanner. A directory (including a Git
    submodule entry) is not recursively covered by a file listing either.
    """
    info = source_stat(path, notes, required=required)
    if info is not None and not stat.S_ISREG(info.st_mode):
        notes.append({"what": _label(path), "unreadable": True,
                      "why": "not a regular file; excluded without reading, this source is unmeasured"})
        return None
    return info


def checkout_source(root: pathlib.Path, notes: list[dict]) -> bool:
    """A package without a Git marker has no tracked-tree source to measure.

    A present (including linked-worktree) marker does not prove Git works: its
    listing must still succeed. Inaccessible marker metadata is a coverage hole.
    """
    for folder in (root, *root.parents):
        marker = folder / ".git"
        try:
            marker.lstat()
            return True
        except FileNotFoundError:
            continue
        except OSError:
            notes.append({"what": "the tracked tree", "unreadable": True,
                          "why": "Git marker metadata could not be read; tracked files are unmeasured"})
            return False
    notes.append({"what": "the engine's tracked tree",
                  "why": "the engine directory is not a git checkout (an installed package)"})
    return False


def _walk_transcripts(root: pathlib.Path, suffixes: tuple[str, ...], cutoff: float,
                      notes: list[dict], out: list[pathlib.Path]) -> int:
    """Add the transcripts under `root` modified since `cutoff`; return how many were older.

    An absent root is not a hole (the tool is not installed, or has not written yet);
    a root that exists and will not read is.
    """
    info = source_stat(root, notes)
    if info is None:
        return 0
    if not stat.S_ISDIR(info.st_mode):
        notes.append({"what": _label(root), "unreadable": True,
                      "why": "the sessions source is not a directory; its transcripts are unmeasured"})
        return 0
    old = 0
    # AN EXPLICIT WALK, not `rglob` (review round 3): `rglob` skips a folder
    # it cannot open without a word, so a project folder at mode 000 dropped
    # its transcripts and /health stayed green. Each folder that will not
    # list is a hole now, named `~`-relative.
    unlisted: list[str] = []

    def refused(err: OSError) -> None:
        unlisted.append(_label(getattr(err, "filename", None) or root))

    for folder, dirs, files in os.walk(root, onerror=refused):
        for name in list(dirs):
            child = pathlib.Path(folder) / name
            try:
                linked = stat.S_ISLNK(child.lstat().st_mode)
            except OSError:
                dirs.remove(name)
                notes.append({"what": _label(child), "unreadable": True,
                              "why": "session folder metadata could not be read; its transcripts are unmeasured"})
                continue
            if linked:
                dirs.remove(name)
                notes.append({"what": _label(child), "unreadable": True,
                              "why": "session directory symbolic link excluded without following; its transcripts are unmeasured"})
        for fname in files:
            if not fname.endswith(suffixes):
                continue
            p = pathlib.Path(folder) / fname
            info = source_file_stat(p, notes, required=True)
            if info is None:
                continue
            if info.st_mtime < cutoff:
                old += 1
                continue
            out.append(p)
    for where in unlisted:
        notes.append({"what": f"session folder {where}", "unreadable": True,
                      "why": "could not be listed; the transcripts in it are unmeasured, not clean"})
    return old


def transcript_homes(notes: list[dict] | None = None) -> list[dict]:
    """The agent transcript homes of `paths.transcript_homes`, each profile expanded into a home.

    The Claude row is this module's `SESSIONS` (`OBSERVATORY_SESSIONS` wins), so the scanner
    and its tests keep one place to point it. A profiles folder that exists and will not list
    is a hole when `notes` is given; one that is absent is nothing.
    """
    rows = []
    for row in paths.transcript_homes(sessions=SESSIONS):
        rows.append(row)
        parent, layout = row.get("profiles") or (None, None)
        if parent is None:
            continue
        children, odd = [], []
        try:
            with os.scandir(parent) as entries:
                for e in entries:
                    if e.name.startswith("."):
                        continue
                    # THE SAME BOUNDARY AS A SESSION FOLDER: a profile that is a symbolic link
                    # (a loop among them) is not followed, and neither it nor a stray file is
                    # dropped in silence.
                    try:
                        linked = e.is_symlink()
                        folder = not linked and e.is_dir(follow_symlinks=False)
                    except OSError:
                        linked, folder = False, False
                    if folder:
                        children.append(pathlib.Path(e.path))
                    else:
                        odd.append((pathlib.Path(e.path), linked))
        except FileNotFoundError:
            continue
        except OSError:
            if notes is not None:
                notes.append({"what": _label(parent), "unreadable": True,
                              "why": f"the {row['agent']} profiles folder could not be listed; "
                                     "the transcripts of its profiles are unmeasured, not clean"})
            continue
        if notes is not None:
            for path, linked in sorted(odd):
                notes.append({"what": _label(path), "unreadable": True,
                              "why": (f"{row['agent']} profile symbolic link excluded without following"
                                      if linked else f"not a {row['agent']} profile folder; excluded without reading")
                                     + "; its transcripts are unmeasured"})
        children.sort()
        for child in children:
            rows.append({"agent": row["agent"], "home": child,
                         "trees": [(child / t, s) for t, s in layout["trees"]],
                         "stores": [child / s for s in layout["stores"]],
                         "evidence": row["evidence"]})
    seen, unique = set(), []
    for row in rows:
        if row["home"] not in seen:
            seen.add(row["home"])
            unique.append(row)
    return unique


def transcript_stores(notes: list[dict]) -> list[pathlib.Path]:
    """The SQLite databases agents keep transcripts in (Hermes `state.db`, `opencode.db`).

    Read as databases by `scan_sqlite`, like the companion's store. An absent one is
    nothing; one that exists but cannot be examined, or a folder that will not list,
    is a hole named in `notes`.
    """
    found: list[pathlib.Path] = []
    for row in transcript_homes():
        candidates = list(row.get("stores") or [])
        for folder, patterns in row.get("store_globs") or []:
            try:
                with os.scandir(folder) as entries:
                    candidates += sorted(pathlib.Path(e.path) for e in entries
                                         if any(fnmatch.fnmatch(e.name, p) for p in patterns))
            except FileNotFoundError:
                continue
            except OSError:
                notes.append({"what": _label(folder), "unreadable": True,
                              "why": f"the {row['agent']} data folder could not be listed; "
                                     "its transcript databases are unmeasured, not clean"})
        for path in candidates:
            if path not in found and source_file_stat(path, notes) is not None:
                found.append(path)
    return found


def home_coverage(notes: list[dict], unread: list[str] = ()) -> list[dict]:
    """Per transcript home: `absent`; `unreadable` (a hole is named under it); `partial`
    (the step's deadline left a file or a store under it unread, `unread`); or `scanned`."""
    rows = transcript_homes()

    def within(what: str, home: pathlib.Path) -> bool:
        return _under(what, _label(home)) or _under(what, str(home))

    # A HOLE BELONGS TO ITS NEAREST HOME. A locked folder under `~/.claude/projects` is that
    # home's hole; `~/.claude`, which holds it and whose own history was read, is `partial`.
    holes, outer = set(), set()
    for n in notes:
        if not n.get("unreadable"):
            continue
        what = str(n.get("what", ""))
        holders = [i for i, r in enumerate(rows) if within(what, r["home"])]
        if holders:
            nearest = max(holders, key=lambda i: len(str(rows[i]["home"])))
            holes.add(nearest)
            outer.update(i for i in holders if i != nearest)
    cut = {i for i, r in enumerate(rows) for p in unread if within(p, r["home"])}
    out = []
    for i, row in enumerate(rows):
        home = row["home"]
        try:
            home.lstat()
        except FileNotFoundError:
            out.append({"agent": row["agent"], "home": _label(home), "state": "absent"})
            continue
        except OSError:
            pass
        state = "unreadable" if i in holes else "partial" if i in outer or i in cut else "scanned"
        out.append({"agent": row["agent"], "home": _label(home), "state": state})
    return out


def _under(what: str, home: str) -> bool:
    """Does a `not_scanned` entry name `home` or something inside it (a file, a table)?"""
    what = what.removeprefix("session folder ")
    return what == home or what.startswith((home + "/", home + "#"))


def targets(days: int) -> tuple[list[pathlib.Path], list[dict]]:
    """Where a value must never appear, and what was left out of the window."""
    out: list[pathlib.Path] = []
    notes: list[dict] = []
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    # EVERY AGENT'S TRANSCRIPTS (I-5), not only Claude's: Codex rollouts, Hermes session
    # logs and opencode's message files take the same echoed value a Claude transcript does.
    old = 0
    for row in transcript_homes(notes):
        for root, suffixes in row.get("trees") or []:
            old += _walk_transcripts(root, suffixes, cutoff, notes, out)
        for path in row.get("files") or []:
            # A single transcript file: Codex and Claude Code `history.jsonl`.
            info = source_file_stat(path, notes)
            if info is None:
                continue
            if info.st_mtime < cutoff:
                old += 1
                continue
            out.append(path)
    if old:
        notes.append({"what": f"{old} session transcript(s)",
                      "why": f"older than {days} days; `--days N` widens the "
                             f"window and `--full` re-reads from the start"})
    logs = paths.STATE / "logs"
    logs_info = source_stat(logs, notes)
    if logs_info is not None:
        try:
            with os.scandir(logs) as entries:
                for entry in entries:
                    if not entry.name.endswith(".jsonl"):
                        continue
                    p = pathlib.Path(entry.path)
                    info = source_file_stat(p, notes, required=True)
                    if info is not None and stat.S_ISREG(info.st_mode):
                        out.append(p)
        except OSError:
            notes.append({"what": _label(logs), "unreadable": True,
                          "why": "the log directory could not be listed; its files are unmeasured"})
    dashboard_info = source_file_stat(paths.DASHBOARD_HTML, notes)
    if dashboard_info is not None and stat.S_ISREG(dashboard_info.st_mode):
        out.append(paths.DASHBOARD_HTML)
    if checkout_source(ROOT, notes):
        try:
            tracked = safe_git.run(["ls-files", "-z"], cwd=ROOT, timeout=60)
            if tracked.returncode != 0:
                notes.append({"what": "the tracked tree", "unreadable": True,
                              "why": "git ls-files failed; tracked files are unmeasured, not clean"})
            else:
                for rel in tracked.stdout.split("\0"):
                    if not rel:
                        continue
                    p = ROOT / rel
                    info = source_file_stat(p, notes, required=True)
                    if info is not None and stat.S_ISREG(info.st_mode):
                        out.append(p)
        except (OSError, subprocess.SubprocessError) as exc:
            notes.append({"what": "the tracked tree", "unreadable": True,
                          "why": f"git ls-files failed: {type(exc).__name__}; tracked files are unmeasured"})
    return out, notes


#: Rows per read. Each page is its own statement, finished before the next one starts, so a
#: read-only pass never holds a reader's lock on the database for longer than one page: on a
#: rollback-journal database a long cursor holds SHARED and every writer waits behind it.
PAGE = 500

#: A column a tool bumps when it rewrites a row in place, the finest grain first: Codex
#: `updated_at_ms` before its `updated_at` (seconds); opencode `time_updated` (`part`,
#: `message`, `session`, milliseconds); Hermes `last_activity_at` (`sessions`); Codex
#: `source_updated_at`. A row whose value is above the last pass's maximum is read again;
#: a row AT that maximum is read again when its digest is not the one recorded for it
#: (an edit within the same second), so an edit in place is seen without re-reading the
#: table and without reporting the newest row every tick.
UPDATED_COLUMNS = ("updated_at_ms", "time_updated", "updated_at", "last_activity_at", "source_updated_at")

#: The rows sharing the update column's maximum whose digests are kept one by one; above it,
#: one digest of the whole set is kept, and the set is read again only when that digest changes.
AT_MAX = 64

#: Rowids are summed modulo this prime: the count and the newest row cannot tell
#: {1, 2, 4, 5} from {2, 3, 4, 5}, the sum can, and it never overflows SQLite's integer.
ROWID_MOD = 2147483647

#: And their squares modulo a second prime: {2, 5} and {3, 4} share a sum but not a sum of
#: squares, and a rowid moved by a multiple of ROWID_MOD keeps its residue but not this one.
ROWID_SQ_MOD = 1000000007

#: The table that cannot be read whole leaves its store read only in part: a hole.
PARTIAL = "a SQLite store read only in part; these tables could not be read: "

#: A store that opened and then raised something no table handler knows: its sightings so far
#: stand, the rest of it is a hole.
STOPPED = "a SQLite store stopped while reading "


def _holds_text(declared) -> bool:
    """SQLite's own affinity rules (datatype3 §3.1): a declared type containing CHAR, CLOB or
    TEXT has TEXT affinity (`VARCHAR(255)`, `NCHAR(10)`); one containing BLOB, or no type at
    all, has BLOB affinity. JSON is what some tools declare for text."""
    d = str(declared or "").upper()
    return d == "" or any(k in d for k in ("CHAR", "CLOB", "TEXT", "BLOB", "JSON"))


def _cells(row) -> list[bytes | None]:
    return [None if c is None else (c if isinstance(c, bytes) else str(c).encode("utf-8", "surrogateescape"))
            for c in row]


def _digest(row) -> str:
    """A digest of one whole row, never the row: it tells a reused or rewritten rowid apart."""
    h = hashlib.sha256()
    for cell in _cells(row):
        h.update(b"\1" if cell is None else b"\2" + len(cell).to_bytes(8, "big") + cell)
    return h.hexdigest()[:32]


def _mod(value: int, prime: int) -> int:
    """SQLite's `%` (the sign of the dividend), so a Python-side sum matches the SQL aggregate.
    Anything but an integer counts as 0, as SQLite's `%` makes of text (round 5)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    rest = abs(value) % prime
    return rest if value >= 0 else -rest


def _rowid_alias(declared: list[str]) -> str | None:
    """The name of the real rowid in a table whose own columns may take one of its names: a
    column called `rowid` shadows `rowid`, and its values (NULL, text, duplicates) are not the
    rowid. None when `rowid`, `_rowid_` and `oid` are all declared columns (round 5, F2)."""
    taken = {c.lower() for c in declared}
    return next((a for a in ("rowid", "_rowid_", "oid") if a not in taken), None)


def _strip_sql(sql: str) -> str:
    """A CREATE statement with its comments removed and every string literal and quoted
    identifier emptied, so neither a `)` nor `--`, `/*` or the words WITHOUT ROWID inside one
    can be taken for the statement's own (round 5, F4)."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c in "'\"`":
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            out.append(c + c)
            i = j + 1
        elif c == "[":
            j = sql.find("]", i + 1)
            out.append("[]")
            i = n if j < 0 else j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            out.append(" ")
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            out.append(" ")
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _q(name: str) -> str:
    """An SQL identifier, quoted: a `"` inside a name is doubled. A name that is not UTF-8
    cannot be put in a statement at all; encoding it raises UnicodeEncodeError, a hole."""
    return '"' + name.replace('"', '""') + '"'


def _shown(name: str) -> str:
    """A table or column name as a report can hold it: bytes that were not UTF-8 written as
    `\\udcXX`, never a lone surrogate the state file could not encode."""
    return name.encode("utf-8", "backslashreplace").decode("utf-8")


def _without_rowid(conn: sqlite3.Connection, table: str, sql: str) -> bool:
    """Is the table declared WITHOUT ROWID? SQLite says so itself (`pragma_table_list.wr`,
    3.37+); older SQLite gets its CREATE statement read through `_strip_sql`, so a `)` or the
    words inside a comment, a string or a quoted name cannot decide it."""
    try:
        row = conn.execute("SELECT wr FROM pragma_table_list WHERE schema = 'main' AND name = ?",
                           (table,)).fetchone()
        if row is not None:
            return bool(row[0])
    except sqlite3.Error:
        pass
    bare = _strip_sql(sql)
    return bool(re.search(r"\bWITHOUT\s+ROWID\b", bare[bare.rfind(")") + 1:], re.I))


def _row_digest(conn: sqlite3.Connection, table: str, rowid: int, sel: str, rid: str = "rowid") -> str | None:
    """`sel` is the table's plain columns: never `*`, which evaluates generated columns, whose
    errors quote cells and whose failure blinded the whole table. `rid`: the rowid's name."""
    row = conn.execute(f'SELECT {sel} FROM {_q(table)} WHERE {rid} = ?', (rowid,)).fetchone()
    return None if row is None else _digest(row)


def _plain(value):
    """A signature value the state file can hold: a number or text as is, anything else
    (a BLOB in an update column) as a digest, so the state file always serialises."""
    if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)):
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8")            # text that was not UTF-8 arrives with surrogates
            return value
        except UnicodeEncodeError:
            raw = value.encode("utf-8", "surrogateescape")
    else:
        raw = value if isinstance(value, bytes) else repr(value).encode("utf-8", "surrogateescape")
    return {"sha256": hashlib.sha256(raw).hexdigest()}


def _moved(before, now) -> bool | None:
    """Did an update column's maximum move up? None when that cannot be told (mixed types,
    a digest that changed)."""
    if isinstance(before, dict) or isinstance(now, dict):
        return False if before == now else None
    if before is None or now is None:
        return False if before == now else None
    try:
        return now > before
    except TypeError:
        return None


def _set_digest(conn: sqlite3.Connection, table: str, updated: str, value, top: int, sel: str,
                rid: str = "rowid") -> str:
    """One digest of every row whose update column is `value` at or below rowid `top`,
    read a page at a time: the at-max set past AT_MAX rows."""
    h, after = hashlib.sha256(), 0
    while True:
        rows = conn.execute(f'SELECT {rid}, {sel} FROM {_q(table)} WHERE {_q(updated)} = ? AND {rid} > ? '
                            f'AND {rid} <= ? ORDER BY {rid} LIMIT {PAGE}', (value, after, top)).fetchall()
        if not rows:
            return h.hexdigest()[:32]
        for r in rows:
            h.update(f"{r[0]}:{_digest(r[1:])};".encode())
        after = rows[-1][0]


def table_signature(conn: sqlite3.Connection, table: str, updated: str | None, sel: str,
                    rid: str = "rowid") -> dict:
    """{max rowid, row count, rowid sum and sum of squares, max update value, digest of the
    newest row, digests of the rows at the update maximum (or one of the whole set past
    AT_MAX)} — numbers and digests, never row text. `sel`: the plain columns to digest;
    `rid`: the name the real rowid answers to in this table."""
    top, count, total, squares = conn.execute(
        f'SELECT max({rid}), count(*), sum({rid} % {ROWID_MOD}), '
        f'sum((({rid} % {ROWID_SQ_MOD}) * ({rid} % {ROWID_SQ_MOD})) % {ROWID_SQ_MOD}) FROM {_q(table)}').fetchone()
    upd = conn.execute(f'SELECT max({_q(updated)}) FROM {_q(table)}').fetchone()[0] if updated else None
    at_max = at_max_set = None
    if updated and isinstance(_plain(upd), (int, float, str)):
        rows = conn.execute(f'SELECT {rid}, {sel} FROM {_q(table)} WHERE {_q(updated)} = ? LIMIT {AT_MAX + 1}',
                            (upd,)).fetchall()
        if len(rows) <= AT_MAX:
            at_max = {str(r[0]): _digest(r[1:]) for r in rows}
        else:
            at_max_set = _set_digest(conn, table, updated, upd, top or 0, sel, rid)
    return {"max": top or 0, "count": count or 0, "sum": total or 0, "sq": squares or 0, "upd": _plain(upd),
            "at_max": at_max, "at_max_set": at_max_set,
            "tail": _row_digest(conn, table, top, sel, rid) if top else None}


def scan_sqlite(db: pathlib.Path, pattern: list[bytes], by_value: dict[bytes, SecretRef],
                since: dict[str, int] | None = None, signature: dict | None = None,
                hits: dict | None = None, tally: dict | None = None, stop=None,
                resume: dict[str, int] | None = None, progress: dict[str, int] | None = None
                ) -> tuple[dict[tuple[SecretRef, str], int], str | None, dict[str, int], int, dict | None]:
    """Occurrences by (secret name, `table.column`) across every text column
    of a SQLite store, read-only; the reason when it could not be read; the
    highest rowid read per table; the number of rows read; and the store's new
    signature (`table_signature` per table, plus the file's inode).

    A session companion's store holds session summaries, which is exactly the
    shape a leak takes, and a text scan of a SQLite file reads page headers and
    misses the rows — so the store is read as a database instead of being named
    as unread. A page is tested against every value at once and examined cell by
    cell only when it holds one.

    `since` maps a table to the highest rowid a previous pass read, and
    `signature` is what that pass saw. Rows above the mark, up to the highest
    rowid now, are new. The rows at or below it are read again WHOLE when the
    store is another file (inode), when the count or the rowid sum says the set
    of rowids below the mark changed, or when the previous newest row's digest
    changed (its rowid was reused, or it was rewritten). Rows whose update column
    (`UPDATED_COLUMNS`) moved past the last maximum are read again, and so are
    rows at that maximum whose digest is not the recorded one. Without a
    signature for a marked table the old rows are read once whole. A table that
    cannot be addressed by rowid is read whole on every pass and gets no mark.
    NOT SEEN BEFORE THE WEEKLY COMPLETE PASS (tools/sqlite_scan.py): a row
    replaced under its own rowid below the newest row in a table with no update
    column; an edit that does not move the update column at all; an edit below
    a maximum already in the future; an update column holding mixed types (its
    maximum is compared within one type only); and a change of rowids whose
    count, sum and sum of squares all collide (impractical, not impossible).

    `stop`, when given, is asked before every page; once it says so the read raises
    `step_budget.OutOfTime` and the caller keeps the store's old mark (OBS-52).

    A COMPLETE PASS RESUMES (review of #328, M1). In a complete pass a table is read from
    `resume[table]` rather than from rowid 0: the rows at or below it were read earlier in the
    same complete pass, under the same value set. `progress`, when given, is filled as the read
    goes: the highest rowid read so far per table, so a pass that `stop` ends keeps it.

    A table that cannot be read — a damaged page, a missing module — is a hole:
    the store comes back read in part (`PARTIAL`), its other tables still read,
    and its marks do not move. Text that is not UTF-8 is read as its bytes.

    READ-ONLY IS NOT SIDE-EFFECT FREE: SQLite opens a WAL database's `-shm`
    (and may create `-wal`/`-shm` beside it) even with `mode=ro`. Each page of
    rows is its own statement, but the signature's aggregates (count, sums,
    maximum) each scan the whole table in one statement: on a rollback-journal
    database a writer can wait for one such scan. Codex, opencode and Hermes run
    their stores in WAL mode by default, where a reader does not block a writer.
    """
    since = since or {}
    old_sig = signature or {}
    # The caller may hand its own dict: what is counted before a store stops stays counted.
    hits: dict[tuple[SecretRef, str], int] = {} if hits is None else hits
    high: dict[str, int] = {}
    read = 0
    try:
        info = db.stat()
    except FileNotFoundError:
        return hits, None, high, read, None
    except OSError as exc:
        return hits, type(exc).__name__, high, read, None
    if not stat.S_ISREG(info.st_mode):
        return hits, None, high, read, None
    same_file = old_sig.get("inode") == info.st_ino
    new_sig: dict = {"inode": info.st_ino, "tables": {}}
    try:
        conn = sqlite3.connect(paths.sqlite_uri(db), uri=True)
    except sqlite3.Error as exc:
        # The type only, here as everywhere (round 5, F1): SQLite's message can quote text.
        return hits, type(exc).__name__, high, read, None
    # TEXT THAT IS NOT UTF-8 IS STILL READ. The default decoder raised on it, and the error
    # message carried the cell's text; bytes kept as surrogates match as the bytes they are.
    conn.text_factory = lambda b: b.decode("utf-8", "surrogateescape")
    damaged: list[str] = []

    def in_time() -> None:
        if stop is not None and stop():
            raise step_budget.OutOfTime("the step's deadline is near")

    def scan_rows(table: str, cols: list[str], rows) -> None:
        nonlocal read
        read += len(rows)
        if tally is not None:
            # The caller's count too: a store that stops midway still says what it read (L5).
            tally["rows"] = tally.get("rows", 0) + len(rows)
        blobs = [_cells(row[1:]) for row in rows]
        joined = b"\0".join(b for cells in blobs for b in cells if b)
        present = [n for n in pattern if n in joined]
        if not present:
            return
        for cells in blobs:
            for col, blob in zip(cols, cells):
                if not blob:
                    continue
                for needle in present:
                    n = blob.count(needle)
                    if n:
                        key = (by_value[needle], f"{table}.{col}")
                        hits[key] = hits.get(key, 0) + n

    def pages(table: str, cols: list[str], low: int, top: int, extra: str = "", args: tuple = (),
              rid: str = "rowid", track: bool = False) -> tuple[int, int, int]:
        """Read rowid in (low, top] page by page, each page a finished statement;
        (the rows read, their rowid sum modulo ROWID_MOD, their square sum modulo ROWID_SQ_MOD).
        `rid` is the name the real rowid answers to (`_rowid_alias`)."""
        sel = ", ".join(_q(c) for c in cols)
        n, total, squares, after = 0, 0, 0, low
        while True:
            in_time()
            rows = conn.execute(f'SELECT {rid}, {sel} FROM {_q(table)} WHERE {rid} > ? AND {rid} <= ?{extra} '
                                f'ORDER BY {rid} LIMIT {PAGE}', (after, top, *args)).fetchall()
            if not rows:
                return n, total, squares
            n += len(rows)
            total += sum(_mod(r[0], ROWID_MOD) for r in rows)
            squares += sum(_mod(_mod(r[0], ROWID_SQ_MOD) ** 2, ROWID_SQ_MOD) for r in rows)
            after = rows[-1][0]
            scan_rows(table, cols, rows)
            if track and progress is not None:
                progress[table] = after

    def at_old_max(table: str, cols: list[str], plain: list[str], updated: str, mark: int, before: dict,
                   rid: str = "rowid") -> None:
        """The rows at the old maximum of the update column whose digest is not the recorded
        one: edited within the same tick of the clock. Read a page at a time, by rowid. Past
        AT_MAX rows the set is compared as one digest and read again only when it changed."""
        sel = ", ".join(_q(c) for c in plain)
        known = before.get("at_max")
        if known is None and before.get("at_max_set") is not None and \
                _set_digest(conn, table, updated, before["upd"], mark, sel, rid) == before["at_max_set"]:
            return
        index = [i for i, c in enumerate(plain) if c in cols]
        after = 0
        while True:
            in_time()
            rows = conn.execute(f'SELECT {rid}, {sel} FROM {_q(table)} WHERE {_q(updated)} = ? AND {rid} > ? '
                                f'AND {rid} <= ? ORDER BY {rid} LIMIT {PAGE}',
                                (before["upd"], after, mark)).fetchall()
            if not rows:
                return
            after = rows[-1][0]
            changed = [r for r in rows if known is None or known.get(str(r[0])) != _digest(r[1:])]
            if changed:
                scan_rows(table, cols, [(r[0], *(r[1 + i] for i in index)) for r in changed])

    def read_table(table: str, sql: str) -> None:
        # THE STORED COLUMNS ONLY (`table_xinfo` hidden 0, and 3 for a STORED generated column,
        # whose value is on disk). A VIRTUAL generated column (2) is computed on read, and
        # evaluating it can fail with an error that quotes a cell (rounds 4 and 5).
        info_rows = conn.execute(f"PRAGMA table_xinfo({_q(table)})").fetchall()
        plain, unnamed = [], []
        for r in info_rows:
            if r[6] not in (0, 3):
                continue
            try:
                r[1].encode("utf-8")
                plain.append(r)
            except UnicodeEncodeError:
                # A column whose name is not UTF-8 cannot be put in a statement: the rest of
                # the table is still read, and this column is named as a hole.
                unnamed.append(r[1])
        for name in unnamed:
            damaged.append(f"{_shown(table)}.{_shown(name)} (column name is not UTF-8)")
        cols = [r[1] for r in plain if _holds_text(r[2])]
        if not cols:
            return
        rid = _rowid_alias([r[1] for r in info_rows])
        if rid is None or _without_rowid(conn, table, sql):
            # A REAL WITHOUT ROWID TABLE, so SQLite says, or one whose own columns take all
            # three of the rowid's names: no rowid to mark, so it is read whole, still a page
            # at a time. Any other error is a hole.
            sel = ", ".join(_q(c) for c in cols)
            offset = 0
            while True:
                in_time()
                rows = conn.execute(f'SELECT NULL, {sel} FROM {_q(table)} LIMIT {PAGE} OFFSET ?',
                                    (offset,)).fetchall()
                if not rows:
                    return
                offset += len(rows)
                scan_rows(table, cols, rows)
        names = [r[1] for r in plain]
        every = ", ".join(_q(c) for c in names)
        updated = next((c for c in UPDATED_COLUMNS if c in names), None)
        now = table_signature(conn, table, updated, every, rid)
        mark = since.get(table, 0) or 0
        if isinstance(mark, bool) or not isinstance(mark, int):
            # A mark that is not a rowid (an old or edited state file) is no mark: the table is
            # read from the start, never left unread until the weekly pass (round 6, L2).
            mark = 0
        before = (old_sig.get("tables") or {}).get(table) if same_file else None
        if before is not None and before.get("max") != mark:
            before = None
        if not mark or now["max"] < mark:
            # First pass, a complete pass, or a table emptied or recreated. A complete pass resumes
            # from what it already read of this table (`resume`), unless the table shrank below it.
            start = (resume or {}).get(table, 0)
            start = start if isinstance(start, int) and not isinstance(start, bool) and 0 < start <= now["max"] else 0
            pages(table, cols, start, now["max"], rid=rid, track=True)
        else:
            fresh, fresh_sum, fresh_sq = pages(table, cols, mark, now["max"], rid=rid)
            below_changed = (before is None
                             or now["count"] != before["count"] + fresh
                             or before.get("sum") is None or before.get("sq") is None
                             or now["sum"] != before["sum"] + fresh_sum
                             or now["sq"] != before["sq"] + fresh_sq
                             or _row_digest(conn, table, mark, every, rid) != before.get("tail"))
            moved = _moved(before.get("upd"), now["upd"]) if updated and before else False
            if below_changed or moved is None:
                pages(table, cols, 0, mark, rid=rid)
            elif updated and isinstance(before.get("upd"), (int, float, str)):
                if moved:
                    pages(table, cols, 0, mark, f" AND {_q(updated)} > ?", (before["upd"],), rid=rid)
                at_old_max(table, cols, names, updated, mark, before, rid)
        high[table] = now["max"]
        if progress is not None:
            progress[table] = now["max"]
        new_sig["tables"][table] = now

    try:
        tables = conn.execute("SELECT name, coalesce(sql, '') FROM sqlite_master "
                              "WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        # FTS mirrors are the same text again: `observations_fts` and its
        # `_fts_*` shadow tables doubled every count on the first run.
        for table, sql in tables:
            if re.search(r"_fts(_|$)", table):
                continue
            try:
                read_table(table, sql)
            except (sqlite3.Error, UnicodeError) as exc:
                # A DAMAGED TABLE IS A HOLE, NEVER A CLEAN TABLE (round 3, N1): a damaged page,
                # a missing module, a name that is not UTF-8 (round 4). Only the error's type
                # is kept: SQLite's message can quote a cell. The other tables are still read,
                # and their sightings stand.
                damaged.append(f"{_shown(table)} ({type(exc).__name__})")
    except UnicodeError as exc:
        return hits, type(exc).__name__, {}, read, None
    except sqlite3.Error as exc:
        # The type only (round 5, F1): `malformed database schema (t) - unknown table option:
        # '<text>'` quotes the stored CREATE statement, and that text can be anything.
        return hits, type(exc).__name__, {}, read, None
    finally:
        conn.close()
    if damaged:
        return hits, PARTIAL + ", ".join(damaged), high, read, None
    return hits, None, high, read, new_sig


#: The operator's decisions that a sighting is known and accepted, each with a
#: reason and an expiry (PB-032). Optional; absent means nothing is suppressed.
SUPPRESSIONS = paths.config_file("leak_suppressions.json")


def load_suppressions(path: pathlib.Path, today) -> tuple[list[dict], list[dict]]:
    """(rules in force, problems). A rule needs `secret`, `where`, `reason` and
    `expires_on` (YYYY-MM-DD) and the sighting's opaque `version_id`.
    An expired or incomplete rule is never applied;
    it is reported, so a suppression cannot outlive the decision behind it."""
    if not path.is_file():
        return [], []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], [{"rule": None, "problem": f"leak_suppressions.json is unreadable ({type(exc).__name__})"}]
    if not isinstance(doc, dict) or not isinstance(doc.get("suppressions"), list):
        return [], [{"rule": None, "problem": "expected an object with a suppressions list; not applied"}]
    rules, problems = [], []
    for i, r in enumerate(doc["suppressions"]):
        if not isinstance(r, dict):
            problems.append({"rule": i, "problem": "expected an object; not applied"})
            continue
        missing = [k for k in ("secret", "where", "reason", "expires_on", "version_id")
                   if not isinstance(r.get(k), str) or not r[k].strip()]
        if missing:
            problems.append({"rule": i, "problem": f"missing {', '.join(missing)}; not applied"})
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", r["version_id"]):
            problems.append({"rule": i, "problem": "version_id must be copied from the sighting; not applied"})
            continue
        try:
            until = datetime.strptime(r["expires_on"], "%Y-%m-%d").date()
        except ValueError:
            problems.append({"rule": i, "problem": "expires_on is not YYYY-MM-DD; not applied"})
            continue
        if until < today:
            problems.append({"rule": i, "secret": r["secret"], "problem": f"expired on {r['expires_on']}; not applied"})
            continue
        rules.append(r)
    return rules, problems


def apply_suppressions(rows: list[dict], rules: list[dict]) -> tuple[list[dict], list[dict]]:
    """(sightings still reported, sightings suppressed with their reason).
    Both the complete location and the salted value identity must match."""
    kept, gone = [], []
    for row in rows:
        rule = next((r for r in rules if r["secret"] == row["secret"]
                     and r["where"] == row["where"] and row.get("version_id")
                     and r["version_id"] == row["version_id"]), None)
        if rule:
            gone.append({**row, "reason": rule["reason"], "expires_on": rule["expires_on"]})
        else:
            kept.append(row)
    return kept, gone


NO_VALUES = "nothing to look for: no env scan, no vault and no installed key on this machine"


def _clean(obj):
    """The report as UTF-8 can carry it (review round 8): on Linux a non-UTF-8 file
    or folder name decodes to a lone surrogate, which `json` writes and UTF-8
    refuses. Such a string is escaped (`\\udcff`), so the name stays visible."""
    if isinstance(obj, str):
        try:
            obj.encode("utf-8")
            return obj
        except UnicodeEncodeError:
            return obj.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(obj, dict):
        return {_clean(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def _write_json(path, doc) -> None:
    """Every report and state write goes through `_clean` (review round 8)."""
    atomic.write_json(path, _clean(doc))


def _label(path) -> str:
    """A store's name for the report: `~`-relative under the home, never a value."""
    p = pathlib.Path(path)
    try:
        return "~/" + str(p.relative_to(pathlib.Path.home()))
    except ValueError:
        return str(p)


def scan_file(path: pathlib.Path, pattern: list[bytes], by_value: dict[bytes, SecretRef],
              start: int, longest: int, stop=None) -> tuple[dict[SecretRef, int], int, str | None]:
    """Occurrences by secret name, the offset reached, and why it could not be read.

    PERMISSION DENIED IS NOT CLEAN (PB-032). A file this process may not open
    used to return no hits, and the report counted it among the targets it had
    read. The reason now comes back, and the caller lists the file as unread.

    `stop`, when given, is asked before each chunk; once it says so the read ends there and the
    problem is OUT_OF_TIME, with the offset reached moved back by the overlap kept for a value
    split across two chunks, so the next pass starts where this one stopped (OBS-52). A value
    inside that overlap can be counted by both passes; none is missed.
    """
    hits: dict[SecretRef, int] = {}
    try:
        size = path.stat().st_size
    except OSError as exc:
        return hits, start, type(exc).__name__
    if size < start:
        # TRUNCATED OR REPLACED. A log that rotated is a new file under an old
        # name, and resuming at the old offset would skip its whole beginning.
        start = 0
    try:
        with path.open("rb") as fh:
            fh.seek(start)
            read, carried, stopped = _scan_stream(fh, pattern, by_value, longest, hits, stop)
    except OSError as exc:
        return hits, start, type(exc).__name__
    if stopped:
        return hits, start + read - carried, OUT_OF_TIME
    return hits, size, None


def _scan_stream(fh, pattern: list[bytes], by_value: dict[bytes, SecretRef], longest: int,
                 hits: dict[SecretRef, int], stop=None) -> tuple[int, int, bool]:
    """Count every value in a binary stream, chunk by chunk, with an overlap so a value
    split across two reads is still found. (bytes read, bytes of overlap carried at the end,
    whether `stop` ended it before the end of the stream)."""
    carry = b""
    read = 0
    while True:
        if stop is not None and stop():
            return read, len(carry), True
        chunk = fh.read(CHUNK)
        if not chunk:
            return read, len(carry), False
        read += len(chunk)
        buf = carry + chunk
        for needle in pattern:
            at = buf.find(needle)
            while at >= 0:
                name = by_value[needle]
                hits[name] = hits.get(name, 0) + 1
                at = buf.find(needle, at + 1)
        keep = min(longest, len(buf))
        carry = buf[len(buf) - keep:]


#: Codex compresses a finished rollout to `rollout-….jsonl.zst` (openai/codex
#: codex-rs/rollout/src/compression.rs). Python reads zstd from 3.14 on.
ZSTD_MISSING = "compressed with zstd, and this Python has no zstd decoder (3.14+ reads .zst)"


def scan_zst(path: pathlib.Path, pattern: list[bytes], by_value: dict[bytes, SecretRef],
             longest: int, stop=None) -> tuple[dict[SecretRef, int], int, str | None]:
    """`scan_file` for a zstd-compressed transcript, read whole: a frame has no offset to resume.

    A file this Python cannot decode is a hole with that reason, never a clean file. One that
    `stop` ended is OUT_OF_TIME, and is read whole again by the next pass.
    """
    hits: dict[SecretRef, int] = {}
    try:
        from compression import zstd
    except ImportError:
        return hits, 0, ZSTD_MISSING
    failures = (OSError, EOFError, ValueError, getattr(zstd, "ZstdError", OSError))
    try:
        size = path.stat().st_size
        with zstd.open(path, "rb") as fh:
            stopped = _scan_stream(fh, pattern, by_value, longest, hits, stop)[2]
    except failures as exc:
        return {}, 0, type(exc).__name__
    if stopped:
        return hits, 0, OUT_OF_TIME
    return hits, size, None


def _signature(path: pathlib.Path) -> list[int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return [info.st_size, info.st_mtime_ns]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true",
                    help="forget remembered offsets and read every target whole")
    ap.add_argument("--days", type=int, default=14,
                    help="how far back a session transcript counts (default 14)")
    a = ap.parse_args(argv[1:])

    values, homes, skipped = known_values()
    home_paths = {h["path"] for h in homes}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not values:
        holes = sum(1 for k in skipped if k.get("unreadable"))
        unsearchable = sum(1 for k in skipped if not k.get("unreadable"))
        # SAID FROM WHAT WAS RECORDED (review rounds 5 and 6): "no env scan, no
        # vault" was false when a source existed and would not read, or when
        # values were found and none was distinctive enough to search for.
        reasons = []
        if holes:
            reasons.append(f"{holes} source(s) of values could not be read")
        if unsearchable:
            reasons.append(f"{unsearchable} value(s) were found but none is searchable "
                           f"(too short or not distinctive)")
        if not reasons:
            # SAID FROM WHAT WAS FOUND (review round 7): a valid inventory with no
            # secret class, or a vault that exists and holds no slot, is not "no
            # env scan, no vault".
            inv_files, inv_problem = env_inventory(paths.SCRATCH / "env.json")
            inv_exists = os.path.lexists(paths.SCRATCH / "env.json")
            reasons.append("no env inventory" if not inv_exists else
                           f"the env inventory lists {len(inv_files)} file(s) and no secret value")
            reasons.append("no vault" if not os.path.lexists(VAULT) else "the vault holds no slot")
            reasons.append("no installed key")
        why = "nothing to look for: " + "; ".join(reasons) + \
            (" (each is named in skipped)" if holes or unsearchable else "")
        _write_json(OUT, {
            "scanned_at": now, "hits": [], "known": 0, "skipped": skipped,
            "degraded": [{"code": "no-values", "source": "leak-scan:no-values",
                          "why": why, "reason": why,
                          "effect": "a clean report here means UNMEASURED, not clean"}]})
        print(f"leaks: {why}")
        return 0

    # Keep versions separate even when two different values share one name.
    # The workspace-keyed HMAC cannot identify a value outside this workspace;
    # without the salt it is None and no suppression may match.
    by_value = {v.encode("utf-8", "surrogateescape"):
                (n, sqlite_scan.values_digest({v: n})) for v, n in values.items()}
    rules, rule_problems = load_suppressions(SUPPRESSIONS, datetime.now(timezone.utc).date())
    suppression_mark = hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()
    digest = sqlite_scan.values_digest(values)
    longest = max(len(b) for b in by_value)
    # `bytes.find` PER PATTERN, not one compiled alternation. The alternation was
    # the obvious thing and it read 956 MB in 2m58s: `re` walks the buffer in its
    # own VM, while `find` is `memmem` in C and 199 passes of that over the same
    # buffer still win by an order of magnitude. Measured, not assumed.
    pattern = sorted(by_value, key=len, reverse=True)

    state, signatures, sqlite_mark, saved = {}, {}, {}, {}
    if STATE.is_file():
        try:
            saved = json.loads(STATE.read_text(encoding="utf-8"))
            sqlite_mark = saved.get("sqlite") or {}
            if not a.full:
                state = saved.get("offsets", {})
                signatures = saved.get("signatures", {})
        except ValueError:
            state = {}

    # Revisit existing transcripts when values or decisions change, including
    # expiry. Old offsets cannot prove a new value absent from an old file.
    decisions_changed = saved.get("suppressions_digest") != suppression_mark
    files_full = a.full or digest is None or saved.get("values_digest") != digest or decisions_changed
    if files_full:
        state, signatures = {}, {}

    files, notes = targets(a.days)
    stores = transcript_stores(notes)
    unreadable: list[dict] = []
    hits: dict[tuple[SecretRef, str], int] = {}
    read_bytes = 0
    cut_at = None                    # the index of the first target the deadline left unread (OBS-52)
    for i, p in enumerate(files):
        if step_budget.remaining() < FILE_RESERVE_S:
            cut_at = i
            break
        key = str(p)
        if key in home_paths:
            continue
        # ONLY A `.jsonl` IS APPEND-ONLY (I-5). A Hermes session log, an opencode
        # message file, the built page or a tracked file is rewritten in place, so an
        # offset would skip a value written before the old end. Such a file is read
        # whole again whenever its size or modification time moves.
        # A FILE STOPS BETWEEN ITS CHUNKS when the deadline nears (OBS-52), keeping the stores' time.
        stop = out_of_time(FILE_RESERVE_S)
        if key.endswith(".jsonl"):
            start = int(state.get(key, 0))
            found, reached, problem = scan_file(p, pattern, by_value, start, longest, stop)
        else:
            start, sig = 0, _signature(p)
            if sig is not None and signatures.get(key) == sig:
                continue
            found, reached, problem = (scan_zst(p, pattern, by_value, longest, stop) if key.endswith(".zst")
                                       else scan_file(p, pattern, by_value, 0, longest, stop))
        if problem == OUT_OF_TIME:
            # What was read counts, and an append-only file resumes from where it stopped; a file
            # read whole has no signature recorded, so the next pass reads it again from the start.
            if key.endswith(".jsonl"):
                read_bytes += max(0, reached - start)
                state[key] = reached
            for name, n in found.items():
                hits[(name, key)] = hits.get((name, key), 0) + n
            cut_at = i
            break
        if problem:
            unreadable.append({"what": key, "why": f"could not be read ({problem}); not counted as clean",
                               "unreadable": True})
            continue
        read_bytes += max(0, reached - start)
        if key.endswith(".jsonl"):
            state[key] = reached
        elif sig is not None:
            signatures[key] = sig
        for name, n in found.items():
            hits[(name, key)] = hits.get((name, key), 0) + n

    # THE COMPANION'S STORE, read as a database rather than named as unread.
    # Whole every time — SQLite has no offset to resume from and the store is
    # rewritten in place — so it is not part of `read_bytes`.
    mem = paths.COMPANION_DB
    today = datetime.now(timezone.utc).date()
    full, why = sqlite_scan.plan(sqlite_mark, digest, a.full, today)
    if decisions_changed and not a.full:
        full, why = True, "the effective suppression rules changed"
    # AND THE OBSERVATORY'S OWN STORE, which holds agent memory. Every value an
    # agent writes there is redacted on the way in (`memory_redact`), so a
    # sighting here means the redactor missed one, or a door that does not
    # redact wrote it — the alarm for the safety net itself.
    own = paths.DB
    marks = sqlite_mark.get("stores") or {}
    # WHAT EACH STORE LOOKED LIKE when its mark was taken (I-5): a store replaced by
    # another file, a row deleted under the mark, a reused rowid or a row rewritten
    # in place is read again rather than hidden behind the mark.
    store_sigs = {} if full else dict(saved.get("sqlite_signatures") or {})
    results = {}
    stores_cut = []
    # AND EVERY AGENT'S TRANSCRIPT DATABASE (I-5): Hermes `state.db`, opencode's `opencode.db`,
    # Codex's `state_*`, `logs_*`, `thread_history_*`, `memories*`, `queue_*`, `goals_*`,
    # `agent_message_board_*`.
    # A COMPLETE PASS THAT A DEADLINE CUT RESUMES on the next tick (review of #328, M1): how far it
    # read each table of each store, under this value set, and which stores it finished. The
    # stores it has not finished go first. A different value set, or --full, starts it again.
    # A database FINISHED earlier in the pass is read incrementally from what the pass read it to, with
    # the signature taken then, so a row updated in place, a replaced file or a renumbered table is
    # caught by the same checks as any incremental pass (confirming review of #328). An unfinished one
    # resumes only if it is still the same file. A pass pending longer than PENDING_MAX_DAYS starts over.
    pending = saved.get("sqlite_pending") if full and not a.full else None
    try:
        age = (today - datetime.fromisoformat(str(pending.get("started_on"))).date()).days
    except (AttributeError, TypeError, ValueError):
        age = None
    if not (isinstance(pending, dict) and pending.get("values") == digest and digest is not None
            and isinstance(pending.get("stores"), dict) and age is not None and 0 <= age <= PENDING_MAX_DAYS):
        pending = {"values": digest, "started_on": today.isoformat(), "stores": {}, "complete": [],
                   "signatures": {}, "inodes": {}}
    for field in ("signatures", "inodes"):
        if not isinstance(pending.get(field), dict):
            pending[field] = {}
    done = set(pending.get("complete") or []) if isinstance(pending.get("complete"), list) else set()
    order = list(dict.fromkeys((mem, own, *stores)))
    if full:
        order.sort(key=lambda st: str(st) in done)
    for store in order:
        if step_budget.remaining() < STORE_RESERVE_S:
            # Unread, not clean: the mark stays where it was, so the next pass reads what this one did not.
            results[store] = ({}, None, None, 0, None)
            stores_cut.append(str(store))
            continue
        key = str(store)
        since = {} if full else marks.get(key)
        signature = store_sigs.get(key)
        found: dict = {}
        tally: dict = {}
        resume = None
        progress: dict = {}
        if full and key in done:
            since, signature = pending["stores"].get(key) or {}, pending["signatures"].get(key)
        elif full:
            try:
                inode = store.stat().st_ino
            except OSError:
                inode = None
            resume = pending["stores"].get(key) if pending["inodes"].get(key) == inode else None
            progress = dict(resume) if isinstance(resume, dict) else {}
            pending["inodes"][key] = inode
        try:
            results[store] = scan_sqlite(store, pattern, by_value, since, signature, found, tally,
                                         stop=out_of_time(ANSWER_RESERVE_S),
                                         resume=resume if isinstance(resume, dict) else None,
                                         progress=progress if full and key not in done else None)
        except step_budget.OutOfTime:
            # STOPPED BETWEEN PAGES (OBS-52): unread, not clean, its mark where it was; what it saw
            # before stopping is still reported.
            results[store] = (found, None, None, tally.get("rows", 0), None)
            stores_cut.append(str(store))
        except Exception as exc:  # noqa: BLE001 — one store is a hole, never the end of the scan
            # WHATEVER ONE STORE RAISES (round 4): the other homes are still measured and the
            # report is still written; the sightings counted before it stopped stand (round 5).
            # The type only; a message can quote what was read.
            results[store] = (found, f"{STOPPED}({type(exc).__name__})", None, tally.get("rows", 0), None)
        if full and digest is not None:
            finished = key not in stores_cut and not results[store][1]
            if finished:
                # What it was read to and the signature that describes it: the high-water rowids of an
                # incremental read, or the progress of the read that finished it.
                pending["stores"][key] = results[store][2] if key in done else progress
                pending["signatures"][key] = results[store][4]
                done.add(key)
            elif key not in done:
                # A store not yet finished keeps how far it got. One finished earlier in this pass
                # keeps the mark its signature describes; the rows it read now are read again.
                pending["stores"][key] = progress
    succeeded = {str(store): r for store, r in results.items() if not r[1] and str(store) not in stores_cut}
    pending["complete"] = sorted(done)
    if full and digest is not None:
        # The complete pass ends when every store has been finished in it, over however many ticks
        # (review of #328, M1); the marks are what each store was read to.
        # A store that would not open (or read only in part) is named as a hole in `degraded` and
        # does not hold the pass open: it is read again from the start on the next pass.
        holes = {str(st) for st, r in results.items() if r[1]}
        complete = all(str(st) in done or str(st) in holes for st in order)
        keep_pending = not complete
        new_mark = sqlite_scan.next_mark(
            sqlite_mark, digest, full, today, {st: pending["stores"].get(st) or {} for st in done - holes},
            complete)
        for st in done:
            if pending["signatures"].get(st) is not None:
                store_sigs[st] = pending["signatures"][st]
    else:
        keep_pending = False
        new_mark = sqlite_scan.next_mark(
            sqlite_mark, digest, full, today, {s: r[2] for s, r in succeeded.items()},
            not any(r[1] for r in results.values()) and not stores_cut)
        for s, r in succeeded.items():
            if r[4] is not None:
                store_sigs[s] = r[4]
            else:
                store_sigs.pop(s, None)
    for store, (store_hits, problem, _high, _rows, _sig) in results.items():
        for (name, where_in), n in store_hits.items():
            hits[(name, f"{store}#{where_in}")] = hits.get((name, f"{store}#{where_in}"), 0) + n
        if problem:
            notes.append({"what": str(store),
                          "why": (problem if problem.startswith((PARTIAL, STOPPED))
                                  else f"a SQLite store that would not open: {problem}"),
                          "unreadable": True})
    _hits, mem_problem, _high, mem_rows, _sig = results[mem]
    companion = {"path": str(mem), "read": mem.is_file() and not mem_problem and str(mem) not in stores_cut,
                 "mode": "full" if full else "incremental", "why": why, "rows_read": mem_rows}
    own_problem, own_rows = results[own][1], results[own][3]
    own_store = {"path": str(own), "read": own.is_file() and not own_problem and str(own) not in stores_cut,
                 "mode": "full" if full else "incremental", "rows_read": own_rows}

    _write_json(STATE, {"updated_at": now, "offsets": state, "signatures": signatures,
                        "values_digest": digest, "suppressions_digest": suppression_mark,
                        **({"sqlite": new_mark, "sqlite_signatures": store_sigs} if new_mark else {}),
                        **({"sqlite_pending": pending} if keep_pending else {})})
    transcript_store_rows = [
        {"store": _label(s), "read": str(s) not in stores_cut and not results[s][1],
         "rows_read": results[s][3]} for s in stores]
    rows = [{"secret": name, "where": where, "occurrences": n,
             **({"version_id": version} if version else {})}
            for ((name, version), where), n in sorted(hits.items(), key=lambda kv: -kv[1])]
    rows, suppressed = apply_suppressions(rows, rules)
    notes = notes + unreadable
    # EACH ENTRY CARRIES A CODE (ENG-001). One unlabelled `degraded` used to mean
    # both "nothing to look for" and "the deadline cut this pass", and the
    # findings read any entry as the first: a pass the deadline cut after it had
    # already seen a credential reported only that it had nothing to look for.
    # The codes are `no-values` (above), `deadline-targets`, `deadline-store`
    # and `source-unreadable`; the prose beside them is for a person.
    degraded = []
    if cut_at is not None:
        degraded.append({"code": "deadline-targets",
                         "why": f"the step's time ran out after {cut_at} of {len(files)} targets; the other "
                                f"{len(files) - cut_at} resume from their offsets on the next pass",
                         "effect": "those targets are UNMEASURED by this pass, not clean"})
    for store in stores_cut:
        degraded.append({"code": "deadline-store",
                         "why": f"the step's time ran out before {_label(store)} was read; its mark did not move",
                         "effect": "that store is UNMEASURED by this pass, not clean"})
    stores_unread = [_label(s) for s in stores_cut] + [
        _label(store) for store, r in results.items() if r[1] and str(store) not in stores_cut]
    # EVERY UNREAD SOURCE IS A HOLE, and a hole holds /health as degraded
    # (decision, review round 2): the scan finished, but what it could not open
    # is unmeasured, not clean. `notes` already holds `unreadable` and any
    # unreadable source `targets()` named, and each store that would not open.
    holes = sum(1 for n in notes if n.get("unreadable")) + sum(1 for k in skipped if k.get("unreadable"))
    if holes:
        degraded.append({"code": "source-unreadable",
                         "why": f"{holes} source(s) could not be opened; each is named in not_scanned or skipped",
                         "effect": "those sources are UNMEASURED, not clean; the rest of the pass counts"})
    for d in degraded:
        # `source` and `reason` are what /health and `collector.degraded` read;
        # without them every entry printed "no reason given".
        d.setdefault("source", "leak-scan:" + d["code"])
        d.setdefault("reason", d["why"])
    partial = any(d["code"].startswith("deadline-") for d in degraded)
    targets_read = len(files) if cut_at is None else cut_at
    _write_json(OUT, {
        "scanned_at": now,
        "known": len(values),
        "targets": len(files),
        "read_bytes": read_bytes,
        "incremental": not files_full,
        "hits": rows,
        "suppressed": suppressed,
        "suppression_problems": rule_problems,
        "coverage": {"known_values": len(values), "targets": len(files),
                     "targets_read": targets_read, "stores_unread": stores_unread,
                     "targets_unreadable": len(unreadable), "read_bytes": read_bytes,
                     "window_days": a.days, "companion_store": companion["mode"] if companion["read"] else "unread",
                     "observatory_store": own_store["mode"] if own_store["read"] else "unread",
                     "transcript_stores": transcript_store_rows,
                     "transcript_homes": home_coverage(
                         notes, [str(p) for p in (files[cut_at:] if cut_at is not None else [])] + stores_cut)},
        "skipped": skipped,
        "not_scanned": notes,
        "companion_store": companion,
        "observatory_store": own_store,
        "partial": partial,
        "degraded": degraded,
    })
    print(f"leaks: {len(values)} known value(s) against {len(files)} target(s), "
          f"{read_bytes/1e6:.1f} MB read, {len(rows)} sighting(s); companion store "
          f"{companion['mode']} ({why}), {mem_rows} row(s) read; observatory store "
          f"{own_rows} row(s) read"
          + (f"; partial: read {targets_read}/{len(files)} targets, stores unread: {len(stores_unread)}"
             if partial else ""))
    for r in rows[:8]:
        # THE NAME AND THE FILE, never the line. A report that quotes the leak is
        # a second copy of it, in a file that is easier to read than the first.
        print(f"  {r['secret']} seen in {r['where']} ×{r['occurrences']}")
    for n in notes:
        print(f"  not scanned: {n['what']} — {n['why']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
