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
the last tick. Plus this project's own logs, its built page, its tracked tree,
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

# Direct scanner callers may use names; main uses a name and version pair.
SecretRef = str | tuple[str, str | None]

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


def targets(days: int) -> tuple[list[pathlib.Path], list[dict]]:
    """Where a value must never appear, and what was left out of the window."""
    out: list[pathlib.Path] = []
    notes: list[dict] = []
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    sessions_info = source_stat(SESSIONS, notes)
    if sessions_info is not None and not stat.S_ISDIR(sessions_info.st_mode):
        notes.append({"what": _label(SESSIONS), "unreadable": True,
                      "why": "the sessions source is not a directory; its transcripts are unmeasured"})
    if sessions_info is not None and stat.S_ISDIR(sessions_info.st_mode):
        old = 0
        # AN EXPLICIT WALK, not `rglob` (review round 3): `rglob` skips a folder
        # it cannot open without a word, so a project folder at mode 000 dropped
        # its transcripts and /health stayed green. Each folder that will not
        # list is a hole now, named `~`-relative.
        unlisted: list[str] = []

        def refused(err: OSError) -> None:
            unlisted.append(_label(getattr(err, "filename", None) or SESSIONS))

        for folder, dirs, files in os.walk(SESSIONS, onerror=refused):
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
                if not fname.endswith(".jsonl"):
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


def scan_sqlite(db: pathlib.Path, pattern: list[bytes], by_value: dict[bytes, SecretRef],
                since: dict[str, int] | None = None
                ) -> tuple[dict[tuple[SecretRef, str], int], str | None, dict[str, int], int]:
    """Occurrences by (secret name, `table.column`) across every text column
    of a SQLite store, read-only; the reason when it could not be read; the
    highest rowid read per table; and the number of rows read.

    A session companion's store holds session summaries, which is exactly the
    shape a leak takes, and a text scan of a SQLite file reads page headers and
    misses the rows — so the store is read as a database instead of being named
    as unread. Rows are read per table in pages of 500 so a store of hundreds of
    megabytes does not sit in memory.

    `since` maps a table to the highest rowid a previous pass read; only rows
    above it are read, the same way transcripts are read from their last
    offset, so a sighting is reported by the pass that first reads its row
    (tools/sqlite_scan.py decides when a pass must be complete). A page is
    tested against every value at once and examined cell by cell only when it
    holds one. A table that cannot be addressed by rowid is read whole on
    every pass and gets no mark.
    """
    since = since or {}
    hits: dict[tuple[SecretRef, str], int] = {}
    high: dict[str, int] = {}
    read = 0
    if not db.is_file():
        return hits, None, high, read
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return hits, f"{type(exc).__name__}: {exc}", high, read
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        # FTS mirrors are the same text again: `observations_fts` and its
        # `_fts_*` shadow tables doubled every count on the first run.
        tables = [t for t in tables if not re.search(r"_fts(_|$)", t)]
        for table in tables:
            try:
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')
                        if str(r[2] or "").upper() in ("TEXT", "", "BLOB", "CLOB", "VARCHAR", "JSON")]
            except sqlite3.Error:
                continue
            if not cols:
                continue
            sel = ", ".join(f'"{c}"' for c in cols)
            by_rowid = True
            try:
                floor = sqlite_scan.floor_for(conn, table, int(since.get(table, 0)))
                cur = conn.execute(f'SELECT rowid, {sel} FROM "{table}" WHERE rowid > ? ORDER BY rowid', (floor,))
            except sqlite3.Error:
                by_rowid, floor = False, 0
                try:
                    cur = conn.execute(f'SELECT NULL, {sel} FROM "{table}"')
                except sqlite3.Error:
                    continue
            top = floor
            while True:
                rows = cur.fetchmany(500)
                if not rows:
                    break
                read += len(rows)
                if by_rowid:
                    top = max(top, rows[-1][0])
                blobs = [[None if c is None else (c if isinstance(c, bytes) else str(c).encode("utf-8", "surrogateescape"))
                          for c in row[1:]] for row in rows]
                joined = b"\0".join(b for cells in blobs for b in cells if b)
                present = [n for n in pattern if n in joined]
                if not present:
                    continue
                for cells in blobs:
                    for col, blob in zip(cols, cells):
                        if not blob:
                            continue
                        for needle in present:
                            n = blob.count(needle)
                            if n:
                                key = (by_value[needle], f"{table}.{col}")
                                hits[key] = hits.get(key, 0) + n
            if by_rowid:
                high[table] = top
    except sqlite3.Error as exc:
        return hits, f"{type(exc).__name__}: {exc}", {}, read
    finally:
        conn.close()
    return hits, None, high, read


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
              start: int, longest: int) -> tuple[dict[SecretRef, int], int, str | None]:
    """Occurrences by secret name, the offset reached, and why it could not be read.

    PERMISSION DENIED IS NOT CLEAN (PB-032). A file this process may not open
    used to return no hits, and the report counted it among the targets it had
    read. The reason now comes back, and the caller lists the file as unread.
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
            carry = b""
            pos = start
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                buf = carry + chunk
                for needle in pattern:
                    at = buf.find(needle)
                    while at >= 0:
                        name = by_value[needle]
                        hits[name] = hits.get(name, 0) + 1
                        at = buf.find(needle, at + 1)
                keep = min(longest, len(buf))
                carry = buf[len(buf) - keep:]
                pos += len(chunk)
    except OSError as exc:
        return hits, start, type(exc).__name__
    return hits, size, None


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

    state, sqlite_mark, saved = {}, {}, {}
    if STATE.is_file():
        try:
            saved = json.loads(STATE.read_text(encoding="utf-8"))
            sqlite_mark = saved.get("sqlite") or {}
            if not a.full:
                state = saved.get("offsets", {})
        except ValueError:
            state = {}

    # Revisit existing transcripts when values or decisions change, including
    # expiry. Old offsets cannot prove a new value absent from an old file.
    decisions_changed = saved.get("suppressions_digest") != suppression_mark
    files_full = a.full or digest is None or saved.get("values_digest") != digest or decisions_changed
    if files_full:
        state = {}

    files, notes = targets(a.days)
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
        start = int(state.get(key, 0))
        found, reached, problem = scan_file(p, pattern, by_value, start, longest)
        if problem:
            unreadable.append({"what": key, "why": f"could not be read ({problem}); not counted as clean",
                               "unreadable": True})
            continue
        read_bytes += max(0, reached - start)
        state[key] = reached
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
    results = {}
    stores_cut = []
    for store in (mem, own):
        if step_budget.remaining() < STORE_RESERVE_S:
            # Unread, not clean: the mark stays where it was, so the next pass reads what this one did not.
            results[store] = ({}, None, None, 0)
            stores_cut.append(str(store))
            continue
        since = {} if full else marks.get(str(store))
        results[store] = scan_sqlite(store, pattern, by_value, since)
    new_mark = sqlite_scan.next_mark(
        sqlite_mark, digest, full, today,
        {str(store): r[2] for store, r in results.items() if not r[1] and str(store) not in stores_cut},
        not any(r[1] for r in results.values()) and not stores_cut)
    for store, (store_hits, problem, _high, _rows) in results.items():
        for (name, where_in), n in store_hits.items():
            hits[(name, f"{store}#{where_in}")] = hits.get((name, f"{store}#{where_in}"), 0) + n
        if problem:
            notes.append({"what": str(store), "why": f"a SQLite store that would not open: {problem}"})
    _hits, mem_problem, _high, mem_rows = results[mem]
    companion = {"path": str(mem), "read": mem.is_file() and not mem_problem and str(mem) not in stores_cut,
                 "mode": "full" if full else "incremental", "why": why, "rows_read": mem_rows}
    own_problem, own_rows = results[own][1], results[own][3]
    own_store = {"path": str(own), "read": own.is_file() and not own_problem and str(own) not in stores_cut,
                 "mode": "full" if full else "incremental", "rows_read": own_rows}

    _write_json(STATE, {"updated_at": now, "offsets": state,
                              "values_digest": digest, "suppressions_digest": suppression_mark,
                              **({"sqlite": new_mark} if new_mark else {})})
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
    # unreadable source `targets()` named.
    holes = (sum(1 for n in notes if n.get("unreadable")) + sum(1 for k in skipped if k.get("unreadable"))
             + sum(1 for r in results.values() if r[1]))
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
                     "observatory_store": own_store["mode"] if own_store["read"] else "unread"},
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
