"""A secret store the settings name outside the workspace, carried inside the encrypted snapshot (OBS-350).

WHY THIS EXISTS. The vault's slots, its movement journal, its leak register, the estate digest key and
the doors' stashes live in `sources.secret_store` (else `sources.secrets`, else `<home>/secrets`). A
store inside the workspace travels in the workspace snapshot's `data/` as it always did. A store the
settings put outside the workspace did not travel at all: another machine restored from the backups
got every setting naming the store and none of the keys, so the vault step of a machine move waited
for a copy by hand.

WHAT IT DOES. `external_stores(base)` names the stores outside the workspace, by the same rule doctor
uses (`backup_vault._settings_stores`), so the store doctor says is not carried is the store a
snapshot carries. `carry(store, dest)` copies one byte for byte under the vault's own mutation lock,
with digests, sizes and modes, and says why when it does not. `verify(source, manifest)` checks a
snapshot's `external/` folder against its manifest. On the way back (part 2), `restore_plan` decides
per store what the restore's `--secret-store` policy does with the folder the restored settings name,
before anything is written, and `apply_plan` writes it through a sibling stage before the home is
renamed into place (the section "the restore's part" below).

WHERE IT GOES. Only into a snapshot that is about to be encrypted (`workspace_upgrade.snapshot` without
an explicit output and with a passphrase): a plaintext copy of an external store is never written.
Beside `data/`, never inside it: `<snapshot>/external/<name>/`. An older engine verifies and restores
`data/` alone and ignores the folder.

WHAT IS NEVER COPIED. Lock files and temporaries (`*.lock`, `.vault-write-*`, `.passphrase-*`, `*.tmp`);
a link (recorded in the manifest with its target when it stays inside the store, never followed, never
written into the snapshot: the encrypted format refuses links, and an older engine's restore would refuse
the whole snapshot); a socket, FIFO or device; and every slot marked `this_machine_only` with
`full vault-digests keep-here`, which stays on this machine on purpose. A value is never decoded,
logged or returned: the manifest holds relative paths, SHA-256 digests, sizes and modes, as `data/`
already does, and it lives inside the encrypted artifact.
"""
from __future__ import annotations

import contextlib
import errno
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import sys
import time

import backup_vault
import configuration as config
import oslocks
import osprivacy

#: Past this the store is not carried (`too-large`): the store measured on a working machine is a few
#: megabytes, and a store this large is more likely a folder named by mistake than a vault.
SECRET_STORE_MAX_MB = 256
#: How long the copy waits for the vault's mutation lock before it says `changed-during-copy`.
LOCK_WAIT_SECONDS = 30
#: Names never carried: the vault's lock and the doors' locks, atomic-write temporaries.
SKIPPED = ("*.lock", ".vault-write-*", ".passphrase-*", "*.tmp")
#: The folder of carried stores inside a snapshot, beside `data/`.
EXTERNAL = "external"
#: The vault's environments and its NAME rule (`tools/vault.py`): what counts as a slot.
ENVS = ("local", "stage", "prod")
SLOT_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
META_SUFFIX = ".meta.json"
#: The largest metadata file read for a mark; a bigger one is not the vault's and fails closed.
META_LIMIT = 65536
#: The slot metadata key `full vault-digests keep-here` writes (OBS-350, spec 3.2a).
KEEP_HERE = "this_machine_only"
STORE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")

#: Snapshot-side reasons an entry is `not-carried`, stable and kebab-case.
REASONS = ("plaintext-output", "no-passphrase", "too-large", "changed-during-copy", "unreadable", "missing",
           "export-failed", "contains-workspace", "update-snapshot")


class CarryRefused(config.ConfigurationError):
    """A refusal with a stable code; a `ConfigurationError`, so every existing handler keeps its exit code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _Changed(Exception):
    pass


class _TooLarge(Exception):
    def __init__(self, size: int):
        super().__init__(size)
        self.size = size


class _Unreadable(Exception):
    pass


# --- which stores are external ------------------------------------------------------------------

def _settings(base: Path) -> dict:
    try:
        return config.load(base)
    except config.ConfigurationError:
        return {}


def external_stores(base: Path) -> list[dict]:
    """Every secret store the workspace's settings name outside it: `{name, settings_keys, path, display}`.

    Read from `base`'s own settings, never from the process's default home. A store inside the workspace
    is carried with `data/` and is not listed; two nested settings are one store, named by both."""
    _, stores = backup_vault._settings_stores(base, _settings(base))
    out = []
    for store in stores:
        keys = list(store["settings_keys"])
        out.append({"name": keys[0], "settings_keys": keys, "path": store["path"],
                    "display": backup_vault._display(store["path"])})
    return out


def external_warnings(base: Path) -> list[dict]:
    """`[{"code": "secret-store-env-override"}]` when the environment moves the vault away from the folder the
    settings name: the maintenance job may not share a person's shell, so only the settings' folder is carried."""
    doc = _settings(base)
    effective, _ = backup_vault._settings_stores(base, doc)
    moved, _ = backup_vault._env_override(effective, doc)
    return [{"code": "secret-store-env-override", "names": moved}] if moved else []


def store_roots(stores: list[dict], home: Path | None = None) -> list[tuple[Path, dict]]:
    """(folder, store) pairs a walk compares its paths with: each store resolved and, when it lies under
    `home`, also spelled under `home` as given, so a walk that never resolves its paths (the machine
    bundle's, under a home reached through a link) still recognises the store."""
    roots = []
    real = home.resolve() if home is not None else None
    for store in stores:
        roots.append((store["path"], store))
        if real is not None and home != real:
            try:
                roots.append((home / store["path"].relative_to(real), store))
            except ValueError:
                pass
    return roots


def inside(path: Path, roots: list[tuple[Path, dict]]) -> dict | None:
    """The store `path` lies in (by `store_roots`), or None."""
    for root, store in roots:
        if path == root or root in path.parents:
            return store
    return None


# --- the copy -----------------------------------------------------------------------------------

def _skipped(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SKIPPED)


def _is_slot(rel: PurePosixPath) -> bool:
    parts = rel.parts
    return len(parts) == 4 and parts[0] == "projects" and parts[2] in ENVS and bool(SLOT_NAME.fullmatch(parts[3]))


def held_back(store: Path) -> dict[str, str]:
    """{`projects/<project>/<env>/<NAME>`: why} for every slot no snapshot may carry: `this-machine-only`
    when its metadata says it stays here (`full vault-digests keep-here`), `unreadable` when its metadata
    is not a small, regular, UTF-8 JSON object. FAIL CLOSED: a mark that cannot be read may be a mark, so
    the slot stays here rather than travelling against its owner's word.

    Reads metadata files only (`<NAME>.meta.json`: dates and marks, never a value)."""
    held: dict[str, str] = {}
    projects = store / "projects"
    if projects.is_symlink() or not projects.is_dir():
        return held
    for project in sorted(projects.iterdir()):
        if project.is_symlink() or not project.is_dir():
            continue
        for env in ENVS:
            folder = project / env
            if folder.is_symlink() or not folder.is_dir():
                continue
            for meta in sorted(folder.iterdir()):
                if not meta.name.endswith(META_SUFFIX):
                    continue
                name = meta.name[:-len(META_SUFFIX)]
                if not SLOT_NAME.fullmatch(name):
                    continue
                key = f"projects/{project.name}/{env}/{name}"
                try:
                    if meta.is_symlink():
                        raise ValueError("a link")
                    doc = json.loads(osprivacy.read_regular(meta, META_LIMIT).decode("utf-8"))
                    if not isinstance(doc, dict):
                        raise ValueError("not an object")
                except (OSError, ValueError, RecursionError):
                    held[key] = "unreadable"
                    continue
                if isinstance(doc.get(KEEP_HERE), dict):
                    held[key] = "this-machine-only"
    return held


def kept_here(store: Path) -> set[str]:
    """The slots marked to stay on this machine (`held_back` with the reason `this-machine-only`)."""
    return {slot for slot, why in held_back(store).items() if why == "this-machine-only"}


def _held(rel: str, held: dict[str, str]) -> str | None:
    """Why a file is held back, when it is a held slot or one of its siblings: `<NAME>`, its metadata
    `<NAME>.meta.json` and every archive `vault.py rotate` leaves (`<NAME>.retired-<stamp>`). A slot NAME
    holds no dot, so everything from its first dot on is the sibling's suffix."""
    parts = rel.split("/")
    if len(parts) != 4 or parts[0] != "projects" or parts[2] not in ENVS:
        return None
    return held.get("/".join((*parts[:3], parts[3].split(".", 1)[0])))


@contextlib.contextmanager
def vault_lock(store: Path, wait: float = LOCK_WAIT_SECONDS):
    """Hold the vault's mutation lock (`<store>/projects/.vault.lock`, the lock every `vault.py` write takes)
    for the whole copy, so no slot, journal line or digest key changes under it. Raises `_Changed` when it
    cannot be had within `wait` seconds; a store with no `projects/` has no vault to lock."""
    projects = store / "projects"
    if projects.is_symlink() or not projects.is_dir():
        yield
        return
    lock = projects / ".vault.lock"
    if lock.is_symlink():
        raise _Unreadable("the vault lock is a link")
    try:
        fd = osprivacy.open(lock, os.O_CREAT | os.O_RDWR | osprivacy.NONBLOCK | osprivacy.NOFOLLOW, 0o600)
    except OSError as exc:
        raise _Unreadable(type(exc).__name__) from None
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _Changed("the vault lock was held") from None
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def _digest_file(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    fd = osprivacy.open(path, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise _Unreadable("not a regular file")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
            size += len(block)
    return size, h.hexdigest()


def _copy_file(source: Path, target: Path) -> tuple[int, str]:
    """Copy byte for byte into a new owner-only file; (bytes, SHA-256 of what was written)."""
    h = hashlib.sha256()
    size = 0
    fd = osprivacy.open(source, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
    with os.fdopen(fd, "rb") as inp:
        if not stat.S_ISREG(os.fstat(inp.fileno()).st_mode):
            raise _Unreadable("not a regular file")
        out_fd = osprivacy.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | osprivacy.NOFOLLOW, 0o600)
        with os.fdopen(out_fd, "wb") as out:
            osprivacy.make_private(out.fileno())
            for block in iter(lambda: inp.read(1024 * 1024), b""):
                out.write(block)
                h.update(block)
                size += len(block)
            out.flush()
            os.fsync(out.fileno())
    return size, h.hexdigest()


def _make_dir(path: Path) -> None:
    path.mkdir(mode=0o700)
    osprivacy.make_private(path)


def _inside(target: str, store_real: str) -> bool:
    return target == store_real or target.startswith(store_real.rstrip(os.sep) + os.sep)


def _copy_once(store: Path, dest: Path, cap: int) -> dict:
    """One pass: walk, copy, then prove nothing carried changed. Raises `_Changed` or `_TooLarge`."""
    store_real = os.path.realpath(store)
    held = held_back(store)
    files: list[dict] = []
    dirs: list[dict] = []
    links: list[dict] = []
    not_carried: list[dict] = []
    total = 0
    slots = 0
    _make_dir(dest)

    def unreadable(error: OSError) -> None:
        name = Path(error.filename) if error.filename else store
        try:
            rel = name.relative_to(store).as_posix()
        except ValueError:
            rel = "."
        if rel == ".":
            raise _Unreadable("the store cannot be listed")
        not_carried.append({"path": rel, "reason": "unreadable"})

    def link(path: Path, rel: str) -> None:
        try:
            raw = os.readlink(path)
        except OSError:
            not_carried.append({"path": rel, "reason": "unreadable"})
            return
        resolved = os.path.realpath(path.parent / raw)
        if not _inside(resolved, store_real):
            not_carried.append({"path": rel, "reason": "link-outside-store"})
            return
        relative = os.path.relpath(resolved, os.path.realpath(path.parent))
        links.append({"path": rel, "target": PurePosixPath(*Path(relative).parts).as_posix()})

    for current, children, names in os.walk(store, followlinks=False, onerror=unreadable):
        here = Path(current)
        keep = []
        for name in sorted(children):
            path = here / name
            rel = path.relative_to(store).as_posix()
            if _skipped(name):
                continue
            why = _held(rel, held)
            if why:
                not_carried.append({"path": rel, "reason": why})
                continue
            if path.is_symlink():
                link(path, rel)
                continue
            try:
                mode = stat.S_IMODE(path.lstat().st_mode)
            except OSError:
                not_carried.append({"path": rel, "reason": "unreadable"})
                continue
            _make_dir(dest / rel)
            dirs.append({"path": rel, "mode": mode})
            keep.append(name)
        children[:] = keep
        for name in sorted(names):
            path = here / name
            rel = PurePosixPath(path.relative_to(store).as_posix())
            if _skipped(name):
                continue
            why = _held(rel.as_posix(), held)
            if why:
                not_carried.append({"path": rel.as_posix(), "reason": why})
                continue
            try:
                info = path.lstat()
            except OSError:
                not_carried.append({"path": rel.as_posix(), "reason": "unreadable"})
                continue
            if stat.S_ISLNK(info.st_mode):
                link(path, rel.as_posix())
                continue
            if not stat.S_ISREG(info.st_mode):
                not_carried.append({"path": rel.as_posix(), "reason": "special-file"})
                continue
            if total + info.st_size > cap:
                raise _TooLarge(total + info.st_size)
            try:
                size, sha = _copy_file(path, dest / rel.as_posix())
            except (OSError, _Unreadable):
                (dest / rel.as_posix()).unlink(missing_ok=True)
                not_carried.append({"path": rel.as_posix(), "reason": "unreadable"})
                continue
            total += size
            if total > cap:
                raise _TooLarge(total)
            files.append({"path": rel.as_posix(), "sha256": sha, "size": size, "mode": stat.S_IMODE(info.st_mode)})
            slots += _is_slot(rel)
    # STILLNESS: what was carried must be what is there now. Under the vault lock no slot moves; the doors
    # keep their own locks outside `projects/`, so a stash written mid-copy is caught here. An append-only
    # journal (`*.jsonl`) may grow: the copy holds what was read, and a later line is in the next snapshot.
    if held_back(store) != held:
        raise _Changed("a keep-here mark changed")
    seen_files = {f["path"] for f in files}
    seen_dirs = {d["path"] for d in dirs}
    after_files, after_dirs = set(), set()
    for current, children, names in os.walk(store, followlinks=False):
        here = Path(current)
        children[:] = [c for c in children if not _skipped(c) and not (here / c).is_symlink()
                       and not _held((here / c).relative_to(store).as_posix(), held)]
        after_dirs |= {(here / c).relative_to(store).as_posix() for c in children}
        for name in names:
            path = here / name
            rel = path.relative_to(store).as_posix()
            if _skipped(name) or path.is_symlink() or not path.is_file():
                continue
            if not _held(rel, held):
                after_files.add(rel)
    # A folder that could not be read is named in not_carried and missing from both walks alike.
    unread = {n["path"] for n in not_carried if n["reason"] == "unreadable" and not _held(n["path"], held)}
    if after_files - unread != seen_files - unread or after_dirs - unread != seen_dirs - unread:
        raise _Changed("files appeared or vanished")
    for row in files:
        path = store / row["path"]
        try:
            size, sha = _digest_file(path)
        except (OSError, _Unreadable):
            raise _Changed(row["path"]) from None
        if row["path"].endswith(".jsonl"):
            if size < row["size"]:
                raise _Changed(row["path"])
        elif size != row["size"] or sha != row["sha256"]:
            raise _Changed(row["path"])
    return {"state": "carried", "reason": None, "files": files, "directories": dirs, "links": links,
            "not_carried": not_carried, "size": total, "vault_slots": slots,
            "kept_here": sum(1 for why in held.values() if why == "this-machine-only")}


def not_carried(reason: str, size: int = 0) -> dict:
    return {"state": "not-carried", "reason": reason, "files": [], "directories": [], "links": [],
            "not_carried": [], "size": size, "vault_slots": 0, "kept_here": 0}


def carry(store: Path, dest: Path, cap: int | None = None, *, lock_wait: float = LOCK_WAIT_SECONDS) -> dict:
    """Copy `store` into the new folder `dest` and describe it, or describe why it was not copied.

    Never raises for the store's sake: a snapshot never fails because of the store. On any outcome but
    `carried`, `dest` does not exist afterwards."""
    cap = SECRET_STORE_MAX_MB * 1024 * 1024 if cap is None else cap
    if store.is_symlink() or not store.is_dir():
        return not_carried("missing")
    try:
        with vault_lock(store, lock_wait):
            for attempt in range(2):
                try:
                    return _copy_once(store, dest, cap)
                except _Changed:
                    shutil.rmtree(dest, ignore_errors=True)
                    if attempt:
                        return not_carried("changed-during-copy")
                except _TooLarge as exc:
                    shutil.rmtree(dest, ignore_errors=True)
                    return not_carried("too-large", exc.size)
                except BaseException:
                    shutil.rmtree(dest, ignore_errors=True)
                    raise
    except _Changed:
        return not_carried("changed-during-copy")
    except (_Unreadable, OSError):
        shutil.rmtree(dest, ignore_errors=True)
        return not_carried("unreadable")
    return not_carried("changed-during-copy")  # pragma: no cover - the loop always returns


# --- verification -------------------------------------------------------------------------------

def _mismatch(message: str) -> CarryRefused:
    return CarryRefused("secret-store-manifest-mismatch", f"Snapshot secret store does not match its manifest: {message}")


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise _mismatch("an invalid path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"..", "."} for part in value.split("/")) or path.as_posix() != value:
        raise _mismatch("a path that is not normalized and relative")
    return value


def verify(source: Path, manifest: dict) -> None:
    """Check `<source>/external/` against the manifest's `externals`: every listed file present with its size
    and digest, every folder present, nothing unlisted, no link or special file, a link listed only with a
    target that stays inside its store, and no folder for a store that was not carried."""
    folder = source / EXTERNAL
    entries = manifest.get("externals")
    if entries is None:
        if os.path.lexists(folder):
            raise _mismatch("an external folder the manifest does not list")
        return
    if not isinstance(entries, list):
        raise _mismatch("externals is not a list")
    if os.path.lexists(folder) and (folder.is_symlink() or not folder.is_dir()):
        raise _mismatch("the external folder is a link or not a folder")
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) \
                or not STORE_NAME.fullmatch(entry["name"]) or entry["name"] in names:
            raise _mismatch("an invalid or repeated store name")
        name = entry["name"]
        names.add(name)
        root = folder / name
        if entry.get("state") != "carried":
            if entry.get("state") != "not-carried" or os.path.lexists(root):
                raise _mismatch(f"{name} was not carried but has a folder")
            continue
        if root.is_symlink() or not root.is_dir():
            raise _mismatch(f"{name} is missing")
        # Links and special files first, before any file is opened for its digest.
        actual_files, actual_dirs = set(), set()
        for path in root.rglob("*"):
            rel = path.relative_to(root).as_posix()
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise _mismatch(f"{name}: a link or a special file")
            (actual_files if path.is_file() else actual_dirs).add(rel)
        listed_files: set[str] = set()
        for row in entry.get("files") or []:
            if not isinstance(row, dict):
                raise _mismatch("an invalid file record")
            rel = _relative(row.get("path"))
            if rel in listed_files or type(row.get("size")) is not int or row["size"] < 0:
                raise _mismatch("a repeated path or an invalid size")
            listed_files.add(rel)
            file = root / rel
            if file.is_symlink() or not file.is_file() or file.stat().st_size != row["size"]:
                raise _mismatch(f"{name}: a file is missing or its size differs")
            if _digest_file(file)[1] != row.get("sha256"):
                raise _mismatch(f"{name}: a file's SHA-256 differs")
        listed_dirs = set()
        for row in entry.get("directories") or []:
            if not isinstance(row, dict):
                raise _mismatch("an invalid folder record")
            listed_dirs.add(_relative(row.get("path")))
        link_paths = {row.get("path") for row in entry.get("links") or [] if isinstance(row, dict)}
        for row in entry.get("links") or []:
            if not isinstance(row, dict):
                raise _mismatch("an invalid link record")
            rel = _relative(row.get("path"))
            target = row.get("target")
            if not isinstance(target, str) or not target or target.startswith("/") or "\\" in target \
                    or "\x00" in target:
                raise _mismatch("a link whose target is not relative")
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
            if joined == ".." or joined.startswith("../") or joined.startswith("/"):
                raise _mismatch("a link whose target leaves its store")
            if rel in listed_files or rel in listed_dirs:
                raise _mismatch("a link that is also a file or a folder")
            # A chain (`s -> .`, `t -> s/..`) normalises inside the store lexically but resolves outside it:
            # the copy records each link's target already resolved, so no carried link passes through another.
            walked: list[str] = []
            for part in [*posixpath.dirname(rel).split("/"), *target.split("/")]:
                if part in ("", "."):
                    continue
                if part == "..":
                    if not walked:
                        raise _mismatch("a link whose target leaves its store")
                    walked.pop()
                else:
                    walked.append(part)
                if "/".join(walked) in link_paths:
                    raise _mismatch("a link whose target passes through another link")
        if actual_files != listed_files or actual_dirs != listed_dirs:
            raise _mismatch(f"{name}: missing or unlisted files or folders")
    if os.path.lexists(folder):
        present = {p.name for p in folder.iterdir()}
        carried = {e["name"] for e in entries if e.get("state") == "carried"}
        if present != carried:
            raise _mismatch("a folder under external/ that no carried store names")


# --- the snapshot's part ------------------------------------------------------------------------

def plan(base: Path, *, carry_now: bool, why_not: str | None) -> list[dict]:
    """The stores to carry, each with the reason it will not be when `carry_now` is false.

    A folder that holds the workspace itself (a setting naming the home folder, say) is never carried:
    its copy would take the workspace and the snapshot being written with it."""
    home = base.resolve()
    rows = []
    for store in external_stores(base):
        if backup_vault._within(home, store["path"]):
            reason = "contains-workspace"
        else:
            reason = None if carry_now else why_not
        rows.append({**store, "reason": reason})
    return rows


def carry_into(stage: Path, stores: list[dict], cap: int | None = None) -> list[dict]:
    """Carry each store into `<stage>/external/<name>/` (or say why not); the manifest's `externals` rows."""
    rows = []
    for store in stores:
        if store.get("reason"):
            entry = not_carried(store["reason"])
        else:
            (stage / EXTERNAL).mkdir(mode=0o700, exist_ok=True)
            entry = carry(store["path"], stage / EXTERNAL / store["name"], cap)
        rows.append({"name": store["name"], "settings_keys": store["settings_keys"], "path": store["display"],
                     **entry})
    if (stage / EXTERNAL).is_dir() and not any((stage / EXTERNAL).iterdir()):
        (stage / EXTERNAL).rmdir()
    return rows


def strip(snapshot: Path, reason: str) -> bool:
    """Remove every carried store from a snapshot that stays plaintext (its export failed or was
    interrupted) and say why in its manifest: a copy kept under the workspace's backups never holds an
    external store. The store goes first, whatever the manifest says or whether it reads; True when a
    store folder was there."""
    folder = snapshot / EXTERNAL
    had = os.path.lexists(folder)
    try:
        if had:
            if folder.is_symlink() or not folder.is_dir():
                folder.unlink()
            else:
                shutil.rmtree(folder)
    finally:
        manifest_file = snapshot / "manifest.json"
        try:
            manifest = config.read_json(manifest_file)
        except (OSError, ValueError, config.ConfigurationError):
            manifest = None
        rows = (manifest or {}).get("externals")
        if isinstance(rows, list):
            changed = False
            for row in rows:
                if isinstance(row, dict) and row.get("state") == "carried":
                    row.update(not_carried(reason))
                    changed = True
            if changed:
                manifest["external_sources_included"] = False
                import workspace
                workspace.write_json(manifest_file, manifest)
    return had


def _is_snapshot(folder: Path) -> bool:
    """A folder the engine wrote as a snapshot: named `<kind>-...` for a snapshot kind (`daily-`,
    `snapshot-`, `before-update-`, `before-upgrade-`), or holding a manifest with `externals`. Anything else
    under the backups folder is a person's own and is never touched."""
    if any(folder.name.startswith(kind + "-") for kind in backup_vault.SNAPSHOT_KINDS):
        return True
    manifest = folder / "manifest.json"
    try:
        doc = json.loads(osprivacy.read_regular(manifest, 64 * 1024 * 1024).decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return False
    return isinstance(doc, dict) and "externals" in doc


def sweep(backups: Path, *, stage_age_seconds: float = 3600) -> dict:
    """Before a snapshot: no plaintext copy under the workspace's backups keeps an external store. Every
    snapshot folder there loses its `external/` (a copy an interrupted export left, or one an older run
    of this release left after a kill), and every `.snapshot-*` stage older than `stage_age_seconds` is
    removed whole (a stage is renamed or removed by the run that made it, so an old one is a dead run's).
    Runs under the workspace's operation lock, so no snapshot of this workspace is being written.

    Never raises for a folder it cannot clean: one undeletable entry must not stop the daily backup or an
    update's snapshot. Each folder it could not clean is named in `failed` (folder name and error class),
    and the next snapshot tries again."""
    stripped, stages = 0, 0
    failed: list[dict] = []
    if backups.is_symlink() or not backups.is_dir():
        return {"stripped": 0, "stages_removed": 0, "failed": []}
    now = time.time()
    try:
        children = sorted(backups.iterdir())
    except OSError as exc:
        return {"stripped": 0, "stages_removed": 0, "failed": [{"path": ".", "error": type(exc).__name__}]}
    for child in children:
        try:
            if child.is_symlink() or not child.is_dir():
                continue
            if child.name.startswith(".snapshot-"):
                if now - child.lstat().st_mtime > stage_age_seconds:
                    shutil.rmtree(child)
                    stages += 1
                continue
            if os.path.lexists(child / EXTERNAL) and _is_snapshot(child):
                strip(child, "export-failed")
                stripped += 1
        except OSError as exc:
            failed.append({"path": child.name, "error": type(exc).__name__})
    return {"stripped": stripped, "stages_removed": stages, "failed": failed}


def summary(rows: list[dict]) -> dict:
    """The answer's `secret_store`: the first store (the one the vault's tools read), or not-applicable."""
    if not rows:
        return {"state": "not-applicable"}
    first = rows[0]
    return {"state": first["state"], "reason": first["reason"], "files": len(first["files"]),
            "size": first["size"], "kept_here": first.get("kept_here", 0),
            "not_carried": len(first.get("not_carried") or [])}


# --- the restore's part (OBS-350 part 2) ----------------------------------------------------------
#
# WHERE IT GOES. The target of a carried store is the folder the restored settings name for it (the
# first of its `settings_keys` present, after the home rewrite), or, with `--secret-store-only`, the
# folder the CURRENT workspace's settings name. A target inside the restored home is refused: the home
# would then hold the store twice. So is the home itself and a sensitive folder (`refused_location`), and a
# snapshot from another machine or home writes only where this machine confirms the folder.
#
# WHAT IS DECIDED FIRST. `restore_plan` reads the target and decides what the policy does with it before
# anything is written; a refusal there changes nothing. `apply_plan` writes, and is called before the
# restored home is renamed into place, so a store that could not be written leaves no home behind.
#
# HOW IT IS WRITTEN. A whole store goes through a sibling stage `.<name>.restore-<hex12>` (folders 0700,
# files 0600 through osprivacy, every digest checked again, fsync), and only then is whatever sits at the
# target (an empty folder, or the store a replace swaps out) moved aside to
# `<target>.before-restore-<stamp>-<hex6>` and the stage renamed in; a failure puts it back. No folder at the
# target is ever deleted. A merge writes each missing file in full beside itself and links it in only while
# the name is still free. The target vault's own lock is held throughout.

#: The restore policies for a target that already exists (`full restore --secret-store`).
POLICIES = ("auto", "keep", "merge", "replace", "skip")
#: A leftover restore stage older than this is a dead run's and the next restore removes it.
RESTORE_STAGE_AGE_SECONDS = 3600
RESTORE_STAGE = re.compile(r"\.(?P<name>.+)\.restore-[0-9a-f]{12}")


class CarryFailed(RuntimeError):
    """A failure while writing a store (exit 1); the target is left as it was."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _write_failed(target_display: str, why: str) -> CarryFailed:
    return CarryFailed("secret-store-write-failed",
                       f"The secret store at {target_display} could not be written ({why}); it was left as it was "
                       "and the home was not restored")


def _within_either(a: Path, b: Path) -> bool:
    return backup_vault._within(a, b) or backup_vault._within(b, a)


def resolve_target(entry: dict, sources: dict, home: Path) -> tuple[Path | None, str, str | None]:
    """(target, the setting that named it, a problem code or None) for one carried store.

    `sources` are the settings' `sources` after the home rewrite; the value is expanded under this
    home. A value absent, relative or unexpandable is `secret-store-unresolved`; one inside the home
    (or holding it) is `secret-store-target-inside-home`."""
    keys = [k for k in (entry.get("settings_keys") or [entry.get("name")]) if isinstance(k, str)]
    for key in keys:
        value = sources.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            path = Path(value).expanduser()
        except (RuntimeError, KeyError):
            return None, key, "secret-store-unresolved"
        if not path.is_absolute():
            return None, key, "secret-store-unresolved"
        path = Path(os.path.abspath(path))
        try:
            inside = _within_either(path.resolve(), home.resolve())
        except (OSError, RuntimeError):
            # A link loop (RuntimeError on Python 3.11) or a folder that cannot be read: refused, never written.
            return path, key, "secret-store-target-refused"
        if inside:
            return path, key, "secret-store-target-inside-home"
        return path, key, None
    return None, (keys[0] if keys else "secret_store"), "secret-store-unresolved"


def _digest_prefix(path: Path, limit: int) -> str:
    h = hashlib.sha256()
    left = limit
    fd = osprivacy.open(path, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        while left > 0:
            block = stream.read(min(left, 1024 * 1024))
            if not block:
                break
            h.update(block)
            left -= len(block)
    return h.hexdigest()


def _through_link(target: Path, rel: str) -> bool:
    """Whether an ancestor of `rel` under `target` is a link or not a folder: nothing is written through it."""
    current = target
    for part in rel.split("/")[:-1]:
        current = current / part
        if not os.path.lexists(current):
            return False
        if current.is_symlink() or not current.is_dir():
            return True
    return False


def _file_state(target: Path, row: dict) -> str:
    """`same`, `missing` or `conflict` for one carried file against the target. A journal (`*.jsonl`) that
    grew since the snapshot reads the same: the copy holds what was read, and a journal only appends
    (the restore's own movement line among them, so a rerun reads `same`)."""
    rel = row["path"]
    if _through_link(target, rel):
        return "conflict"
    path = target / rel
    if not os.path.lexists(path):
        return "missing"
    if path.is_symlink() or not path.is_file():
        return "conflict"
    try:
        size = path.lstat().st_size
        if rel.endswith(".jsonl"):
            same = size >= row["size"] and _digest_prefix(path, row["size"]) == row["sha256"]
        else:
            same = size == row["size"] and _digest_file(path)[1] == row["sha256"]
    except (OSError, _Unreadable):
        return "conflict"
    return "same" if same else "conflict"


def _link_state(target: Path, row: dict) -> str:
    if _through_link(target, row["path"]):
        return "conflict"
    path = target / row["path"]
    if not os.path.lexists(path):
        return "missing"
    try:
        return "same" if path.is_symlink() and os.readlink(path) == row["target"] else "conflict"
    except OSError:
        return "conflict"


def _present(target: Path) -> tuple[set[str], bool]:
    """(the relative paths of every file and link under the target, whether anything else is there).

    Only a REGULAR FILE whose name the copy skips (`*.lock`, temporaries) is ignorable. Every folder is
    entered whatever its name, and a folder named like a skipped file (`keys.tmp/`, `x.lock/`), a socket or
    any other special file makes the target non-empty: a folder is never judged empty by its names."""
    found: set[str] = set()
    other = False
    for current, children, names in os.walk(target, followlinks=False):
        here = Path(current)
        keep = []
        for name in children:
            path = here / name
            if path.is_symlink():
                found.add(path.relative_to(target).as_posix())
                continue
            if _skipped(name):
                other = True
            keep.append(name)
        children[:] = keep
        for name in names:
            path = here / name
            rel = path.relative_to(target).as_posix()
            try:
                info = path.lstat()
            except OSError:
                other = True
                continue
            if stat.S_ISLNK(info.st_mode):
                found.add(rel)
            elif not stat.S_ISREG(info.st_mode):
                other = True
            elif not _skipped(name):
                found.add(rel)
    return found, other


def classify(target: Path, entry: dict) -> dict:
    """The target's state against a carried store: `absent`, `empty`, `same`, `differs` or `not-a-folder`,
    with counts of `missing`, `same`, `conflict` and `only_here`, and the conflicting paths."""
    counts = {"missing": 0, "same": 0, "conflict": 0, "only_here": 0}
    if not os.path.lexists(target):
        return {"state": "absent", "counts": counts, "conflict_paths": []}
    if target.is_symlink() or not target.is_dir():
        return {"state": "not-a-folder", "counts": counts, "conflict_paths": []}
    present, other = _present(target)
    if not present and not other:
        return {"state": "empty", "counts": counts, "conflict_paths": []}
    conflicts = []
    carried = set()
    rows = [(row, _file_state) for row in entry.get("files") or []]
    if not osprivacy.WINDOWS:
        rows += [(row, _link_state) for row in entry.get("links") or []]
    for row, state_of in rows:
        carried.add(row["path"])
        state = state_of(target, row)
        counts[state] += 1
        if state == "conflict":
            conflicts.append(row["path"])
    counts["only_here"] = len(present - carried)
    state = "same" if not counts["missing"] and not counts["conflict"] and not other else "differs"
    return {"state": state, "counts": counts, "conflict_paths": sorted(conflicts)}


# --- where a store may never be written -------------------------------------------------------------

#: Folders under the user's home a store is never written into, nor over: keys, sign-in state and what
#: starts at login. A snapshot's settings decide the target, and a crafted or foreign snapshot must not be
#: able to put files there.
SENSITIVE = (".ssh", ".gnupg", ".aws", ".config/gh", "Library/LaunchAgents", "Library/Keychains")
#: The home's own start-up files: a target with one of these names directly in the home is refused.
STARTUP_FILES = (".bashrc", ".bash_profile", ".profile", ".zshrc", ".zprofile", ".zshenv", ".zlogin",
                 ".gitconfig", ".netrc")


def _user_home() -> Path | None:
    try:
        return Path.home()
    except (RuntimeError, KeyError, OSError):
        return None


def _fold(path: Path) -> tuple[str, ...]:
    """A path's parts as the file system compares them: case-folded where names are case-insensitive by
    default (macOS, Windows), so `~/.SSH` is `~/.ssh` there."""
    parts = Path(os.path.normcase(str(path))).parts
    if sys.platform == "darwin" or osprivacy.WINDOWS:
        parts = tuple(part.casefold() for part in parts)
    return parts


def _inside_folded(path: Path, folder: Path) -> bool:
    a, b = _fold(path), _fold(folder)
    return a[:len(b)] == b


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _nearest_existing(path: Path) -> Path | None:
    current = path
    while not os.path.lexists(current):
        if current.parent == current:
            return None
        current = current.parent
    return current


def refused_location(target: Path) -> str | None:
    """Why a target is a place no store is written to (the home itself or a folder holding it, a sensitive
    folder or a folder inside one, a start-up file of the home), or None.

    Compared twice: by name, case-folded where the file system folds case by default, and by identity, the
    nearest existing folder of the target and each of its parents against each sensitive folder that
    exists (`os.path.samefile`), so another spelling or a link never reaches `~/.ssh`."""
    home = _user_home()
    if home is None:
        return None
    try:
        return _refused_location(home, target)
    except (OSError, RuntimeError) as exc:
        # Python 3.11 raises from `exists()` and `is_dir()` under a folder this account may not read, and
        # `resolve()` raises RuntimeError on a link loop there: a place
        # that cannot be checked is refused, never written, and under keep it is a skipped row.
        return f"a place that could not be checked ({type(exc).__name__})"


def _refused_location(home: Path, target: Path) -> str | None:
    h, t = home.resolve(), target.resolve()
    if _inside_folded(h, t):
        return "the home folder itself, or a folder holding it"
    for rel in SENSITIVE:
        if _inside_folded(t, h / rel):
            return f"~/{rel}, or a folder inside it"
    folded_startup = {_fold(Path(name))[-1] for name in STARTUP_FILES}
    if _fold(t.parent) == _fold(h) and _fold(Path(t.name))[-1] in folded_startup:
        return f"~/{t.name}, a start-up file of the home"
    existing = _nearest_existing(t)
    if existing is not None:
        chain = [existing, *existing.parents]
        for rel in SENSITIVE:
            folder = h / rel
            if folder.is_dir() and any(_same_file(x, folder) for x in chain):
                return f"~/{rel}, or a folder inside it"
        if existing == t and any(_same_file(t, x) for x in (h, *h.parents)):
            return "the home folder itself, or a folder holding it"
        if t.parent.exists() and _same_file(t.parent, h) and _fold(Path(t.name))[-1] in folded_startup:
            return f"~/{t.name}, a start-up file of the home"
    return None


class _Problem(Exception):
    def __init__(self, code: str, counts: dict | None = None):
        super().__init__(code)
        self.code = code
        self.counts = counts


#: The code a problem is said as, in a refusal and in a `skipped` row's `reason`.
_SAID_AS = {"secret-store-not-a-folder": "secret-store-unresolved",
            "secret-store-not-a-vault": "secret-store-target-refused"}


def _refusal(code: str, name: str, setting: str, display: str | None, counts: dict | None = None,
             target: Path | None = None) -> CarryRefused:
    exact = str(target) if target is not None else display
    then = (f"Restore with --secret-store skip, then point the setting elsewhere (project-observatory full "
            f"configure sources {setting} PATH) and run project-observatory full restore SNAPSHOT --secret-store-only")
    if code == "secret-store-not-empty":
        c = counts or {}
        return CarryRefused(code, (
            f"The secret store at {display} already holds a store that differs from the snapshot's "
            f"({c.get('missing', 0)} missing, {c.get('conflict', 0)} different, {c.get('only_here', 0)} only there); "
            "nothing was written. Choose with --secret-store keep (leave it), merge (add the missing files, "
            "keep the rest) or replace (move it aside, write the snapshot's), or skip"))
    if code == "secret-store-target-inside-home":
        return CarryRefused(code, (
            f"The secret store setting sources.{setting} names {display}, which lies inside the restored home "
            f"(or holds it); nothing was written. {then}"))
    if code == "secret-store-target-refused":
        why = refused_location(target) if target is not None else None
        return CarryRefused(code, (
            f"The secret store setting sources.{setting} names {exact}, which is {why or 'a protected place'}; "
            f"a store is never written there and nothing was written. {then}"))
    if code == "secret-store-not-a-vault":
        return CarryRefused("secret-store-target-refused", (
            f"The folder {exact} that sources.{setting} names holds files but no projects/ folder, so it does not "
            "look like a vault store; merge was refused and nothing was written. Use --secret-store replace "
            f"(moves it aside, never deletes it) or skip. {then}"))
    if code == "secret-store-target-unconfirmed":
        return CarryRefused(code, (
            f"The snapshot was taken on another machine or under another home, and its settings put the secret "
            f"store {name} at {exact}; nothing was written. To write it there, run the restore again with "
            f"--secret-store-target '{exact}'; or restore with --secret-store skip and write it later with "
            "project-observatory full restore SNAPSHOT --secret-store-only, into the folder this workspace's "
            "settings name"))
    if code == "secret-store-not-a-folder":
        return CarryRefused("secret-store-unresolved", (
            f"The secret store setting sources.{setting} names {display}, which is a link or not a folder; "
            f"nothing was written. {then}"))
    return CarryRefused("secret-store-unresolved", (
        f"The restored settings do not name a folder for the carried secret store {name} (sources.{setting}); "
        "nothing was written. Restore with --secret-store skip, then name one "
        f"(project-observatory full configure sources {setting} PATH) and run "
        "project-observatory full restore SNAPSHOT --secret-store-only"))


def _decide(state: str, policy: str) -> str:
    if state in ("absent", "empty"):
        return "write"
    if state == "same":
        return "none"
    return {"auto": "refuse", "keep": "keep", "merge": "merge", "replace": "replace"}[policy]


def _same_place(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except (OSError, RuntimeError):
        return os.path.abspath(a) == os.path.abspath(b)


def _confirmed(entry: dict, target: Path, home: Path, *, foreign: bool, confirm: str | None,
               current_sources: dict | None) -> bool:
    """A snapshot from another machine or home writes only where this machine says: its own settings name the
    same folder, or the caller named it with `--secret-store-target`."""
    if not foreign:
        return True
    if confirm:
        try:
            given = Path(os.path.abspath(Path(confirm).expanduser()))
        except (RuntimeError, KeyError):
            given = None
        if given is not None and _same_place(given, target):
            return True
    if current_sources:
        here, _, problem = resolve_target(entry, current_sources, home)
        if problem is None and here is not None and _same_place(here, target):
            return True
    return False


def _gate(row: dict, found: dict, policy: str) -> str:
    """The action for a target in the state `found`, or `_Problem`."""
    state = found["state"]
    if state == "not-a-folder":
        raise _Problem("secret-store-not-a-folder")
    action = _decide(state, policy)
    if action == "refuse":
        raise _Problem("secret-store-not-empty", found["counts"])
    if action == "merge" and state == "differs" and not (row["target"] / "projects").is_dir():
        raise _Problem("secret-store-not-a-vault")
    if action in ("write", "merge", "replace") and not row["confirmed"]:
        raise _Problem("secret-store-target-unconfirmed")
    return action


def restore_plan(source: Path, manifest: dict, sources: dict, home: Path, policy: str, *, foreign: bool = False,
                 confirm: str | None = None, current_sources: dict | None = None) -> dict:
    """What a restore does with every store the snapshot carried, decided before anything is written.

    Raises `CarryRefused` (nothing written) for a refusal the policy gives; under `keep` the same problem is
    a `skipped` row with its code as `reason`. `foreign` (a snapshot from another machine or home) writes only
    where `confirm` (`--secret-store-target`) or `current_sources` (this machine's settings) name the same
    folder. `{"present": bool, "rows": [...]}`; `present` is false for a snapshot whose manifest has no
    `externals` (an older engine's)."""
    if policy not in POLICIES:
        raise config.ConfigurationError(f"--secret-store takes one of {', '.join(POLICIES)}")
    entries = manifest.get("externals")
    if not isinstance(entries, list):
        return {"present": False, "rows": []}
    rows = []
    for entry in entries:
        row = {"entry": entry, "name": entry.get("name"), "target": None, "display": None, "setting": None,
               "state": None, "action": None, "reason": None, "confirmed": True,
               "counts": {"missing": 0, "same": 0, "conflict": 0, "only_here": 0}, "conflict_paths": []}
        rows.append(row)
        if entry.get("state") != "carried":
            row.update(action="none", state="not-in-snapshot", reason=entry.get("reason"))
            continue
        if policy == "skip":
            row.update(action="skip", state="skipped")
            continue
        target, setting, problem = resolve_target(entry, sources, home)
        row.update(target=target, setting=setting,
                   display=backup_vault._display(target) if target is not None else None)
        try:
            if problem is None and refused_location(target):
                problem = "secret-store-target-refused"
            if problem is not None:
                raise _Problem(problem)
            row["confirmed"] = _confirmed(entry, target, home, foreign=foreign, confirm=confirm,
                                          current_sources=current_sources)
            found = classify(target, entry)
            row.update(state=found["state"], counts=found["counts"], conflict_paths=found["conflict_paths"])
            row["action"] = _gate(row, found, policy)
        except _Problem as exc:
            if policy == "keep":
                row.update(action="skip", state="skipped", reason=_SAID_AS.get(exc.code, exc.code))
                continue
            raise _refusal(exc.code, row["name"], setting, row["display"], exc.counts, target) from None
    return {"present": True, "rows": rows}


# --- writing --------------------------------------------------------------------------------------

def _sync_dir(path: Path) -> None:
    if osprivacy.WINDOWS:
        return  # a folder cannot be opened for fsync there; the files are synced one by one
    fd = osprivacy.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _make_parents(folder: Path) -> None:
    """Create the missing parents of a target 0700; an existing parent is never changed."""
    missing = []
    current = folder
    while not os.path.lexists(current):
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for path in reversed(missing):
        _make_dir(path)


def _link(target: str, at: Path) -> None:
    os.symlink(target, at)


def _write_tree(src: Path, entry: dict, into: Path, *, made: bool = False) -> list[dict]:
    """Write a carried store into the new folder `into`: folders 0700, files 0600 with their digests checked
    as written and again after, links recreated (POSIX). Returns the entries not restored here. `made`: the
    caller has just made `into` itself (a verification, which must know the folder is its own)."""
    not_restored: list[dict] = []
    if not made:
        _make_dir(into)
    for row in sorted(entry.get("directories") or [], key=lambda r: r["path"]):
        _make_dir(into / row["path"])
    for row in entry.get("files") or []:
        dest = into / row["path"]
        if not dest.parent.is_dir():
            _make_parents(dest.parent)
        _, sha = _copy_file(src / row["path"], dest)
        if sha != row["sha256"]:
            raise _Unreadable("a written file's digest differs")
    for row in entry.get("links") or []:
        if osprivacy.WINDOWS:
            not_restored.append({"path": row["path"], "reason": "link-not-supported"})
            continue
        at = into / row["path"]
        if not at.parent.is_dir():
            _make_parents(at.parent)
        _link(row["target"], at)
    for row in entry.get("files") or []:
        if _digest_file(into / row["path"])[1] != row["sha256"]:
            raise _Unreadable("a written file's digest differs")
    folders = [into]
    for current, children, _ in os.walk(into, followlinks=False):
        folders += [Path(current) / c for c in children if not (Path(current) / c).is_symlink()]
    for folder in reversed(folders):
        _sync_dir(folder)
    return not_restored


def _stage_for(target: Path) -> Path:
    import secrets
    return target.parent / f".{target.name}.restore-{secrets.token_hex(6)}"


def _aside_for(target: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return target.parent / f"{target.name}.before-restore-{stamp}-{os.urandom(3).hex()}"


def sweep_stages(target: Path, *, age_seconds: float = RESTORE_STAGE_AGE_SECONDS) -> int:
    """Remove this target's leftover restore stages older than `age_seconds` (a run killed mid-write; a stage
    is only ever written by a restore); a recent one may be a live restore's and is left. Never raises."""
    removed = 0
    parent = target.parent
    try:
        children = list(parent.iterdir())
    except OSError:
        return 0
    now = time.time()
    for child in children:
        match = RESTORE_STAGE.fullmatch(child.name)
        if not match or match.group("name") != target.name:
            continue
        try:
            if child.is_symlink() or not child.is_dir() or now - child.lstat().st_mtime <= age_seconds:
                continue
            shutil.rmtree(child)
            removed += 1
        except OSError:
            continue
    return removed


def _install(src: Path, entry: dict, target: Path, display: str) -> tuple[list[dict], Path | None, list[str]]:
    """Write the store whole through a sibling stage, verified, then put it in place with two renames back to
    back: whatever is at the target (an empty folder, or the store `replace` moves) goes aside to
    `<target>.before-restore-<stamp>-<hex6>` and is never deleted; a failure puts it back. Until the stage
    is written and checked, the target is not touched. Returns (not restored, the folder set aside, notes)."""
    stage = _stage_for(target)
    aside = None
    try:
        try:
            _make_parents(target.parent)
            not_restored = _write_tree(src, entry, stage)
        except (OSError, _Unreadable) as exc:
            raise _write_failed(display, type(exc).__name__) from None
        try:
            if os.path.lexists(target):
                aside = _aside_for(target)
                os.rename(target, aside)
            try:
                os.rename(stage, target)
            except BaseException:
                if aside is not None:
                    os.rename(aside, target)
                    aside = None
                raise
        except OSError as exc:
            raise _write_failed(display, type(exc).__name__) from None
    finally:
        if os.path.lexists(stage):
            shutil.rmtree(stage, ignore_errors=True)
    notes = []
    if aside is not None:
        try:
            osprivacy.private_folder(aside)
        except OSError as exc:
            notes.append(f"the folder set aside could not be made owner-only ({type(exc).__name__})")
    try:
        _sync_dir(target.parent)
    except OSError as exc:
        notes.append(f"the store is in place, but the folder holding it could not be synced "
                     f"({type(exc).__name__}); a power loss now could lose the rename")
    return not_restored, aside, notes


def _link_new(temporary: str, dest: Path) -> None:
    """Put a finished temporary at `dest` only if nothing is there: a hard link fails on an existing name."""
    os.link(temporary, dest)


#: `os.link` errors that mean "this file system has no hard links" (exFAT, some SMB or FUSE mounts, Windows
#: volumes without them): the file is then put in place by an exclusive create instead.
NO_HARD_LINKS = {getattr(errno, name) for name in ("EPERM", "ENOTSUP", "EOPNOTSUPP", "EMLINK", "EXDEV", "ENOSYS",
                                                   "EACCES") if hasattr(errno, name)}


def _exclusive_copy(temporary: str, dest: Path) -> None:
    """Where a hard link is not available: an O_EXCL create of `dest` (an existing name fails), its bytes
    copied from the finished temporary. A copy that fails removes the partial `dest` it created."""
    fd = osprivacy.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | osprivacy.NOFOLLOW | osprivacy.BINARY, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            osprivacy.make_private(out.fileno())
            with open(temporary, "rb") as inp:
                shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(dest)
        raise


def _write_one(source: Path, dest: Path, expected: str) -> str | None:
    """One missing file, written in full beside itself, its digest checked against `expected`, and only
    then put in place, and only if `dest` is still absent. None when another writer created it meanwhile
    (a conflict, never overwritten). A digest that differs raises before anything reaches `dest`."""
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix=".restore-", suffix=".tmp", dir=dest.parent)
    try:
        osprivacy.make_private(fd)
        h = hashlib.sha256()
        with os.fdopen(fd, "wb") as out:
            inp_fd = osprivacy.open(source, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
            with os.fdopen(inp_fd, "rb") as inp:
                for block in iter(lambda: inp.read(1024 * 1024), b""):
                    out.write(block)
                    h.update(block)
            out.flush()
            os.fsync(out.fileno())
        if h.hexdigest() != expected or _digest_file(Path(temporary))[1] != expected:
            raise _Unreadable("a written file's digest differs")
        try:
            _link_new(temporary, dest)
        except FileExistsError:
            return None
        except (OSError, AttributeError, NotImplementedError) as exc:
            if isinstance(exc, OSError) and exc.errno not in NO_HARD_LINKS and not osprivacy.WINDOWS:
                raise
            try:
                _exclusive_copy(temporary, dest)
            except FileExistsError:
                return None
        return expected
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def _merge(src: Path, entry: dict, target: Path, display: str) -> tuple[int, list[dict], list[str]]:
    """Write every carried file the target lacks; conflicts, files only there and a file another writer
    creates meanwhile are left alone. Returns (written, not restored, conflicts found while writing)."""
    written = 0
    not_restored: list[dict] = []
    raced: list[str] = []
    try:
        for row in sorted(entry.get("directories") or [], key=lambda r: r["path"]):
            folder = target / row["path"]
            if not os.path.lexists(folder) and not _through_link(target, row["path"] + "/x"):
                _make_dir(folder)
        for row in entry.get("files") or []:
            if _file_state(target, row) != "missing":
                continue
            dest = target / row["path"]
            if not dest.parent.is_dir():
                _make_parents(dest.parent)
            if _write_one(src / row["path"], dest, row["sha256"]) is None:
                raced.append(row["path"])
                continue
            _sync_dir(dest.parent)
            written += 1
        for row in entry.get("links") or []:
            if osprivacy.WINDOWS:
                not_restored.append({"path": row["path"], "reason": "link-not-supported"})
                continue
            if _link_state(target, row) != "missing":
                continue
            at = target / row["path"]
            if not at.parent.is_dir():
                _make_parents(at.parent)
            try:
                _link(row["target"], at)
            except FileExistsError:
                raced.append(row["path"])
                continue
            written += 1
    except (OSError, _Unreadable) as exc:
        raise CarryFailed("secret-store-write-failed",
                          f"The secret store at {display} could not be merged ({type(exc).__name__}); "
                          f"{written} missing files were added before it stopped (each checked before it was "
                          "put in place), nothing was overwritten, and the home was not restored") from None
    return written, not_restored, raced


@contextlib.contextmanager
def _target_lock(target: Path, display: str):
    """Hold the target vault's own mutation lock (`projects/.vault.lock`, as `tools/vault.py` takes it) while
    the store is read and written, so a vault write running beside the restore is never lost.

    Under `replace`, or a write over an empty folder, the held descriptor's file moves with the old folder
    into `<target>.before-restore-*`: a vault command waiting on it gets the lock when the restore ends and
    then opens its files by path, in the store just written; a command starting after the rename takes
    the new store's own lock."""
    if target.is_symlink() or not target.is_dir():
        yield
        return
    held = vault_lock(target)
    try:
        held.__enter__()
    except (_Changed, _Unreadable, OSError) as exc:
        raise CarryFailed("secret-store-write-failed",
                          f"The vault at {display} is busy or its lock cannot be taken ({type(exc).__name__}); "
                          "nothing was written and the home was not restored") from None
    try:
        yield
    finally:
        held.__exit__(None, None, None)


def _journal(target: Path, **detail) -> str:
    """One line in the restored vault's movement journal, through `vault.journal` (never a value)."""
    tools = str(Path(__file__).resolve().parent / "tools")
    folder = target / "projects"
    saved_env = os.environ.get("OBSERVATORY_VAULT_DIR")
    os.environ["OBSERVATORY_VAULT_DIR"] = str(folder)
    try:
        import sys
        if tools not in sys.path:
            sys.path.insert(0, tools)
        import vault
        saved = vault.STORE, vault.MOVES, vault.LEAKS
        vault.STORE, vault.MOVES, vault.LEAKS = folder, folder / "movements.jsonl", folder / "leaks.jsonl"
        try:
            vault.journal("restore-store", "store/*", tool="workspace_upgrade.py", **detail)
        finally:
            vault.STORE, vault.MOVES, vault.LEAKS = saved
        return "written"
    except Exception as exc:  # noqa: BLE001: the store is written; the answer says the line is not
        return f"failed: {type(exc).__name__}"
    finally:
        if saved_env is None:
            os.environ.pop("OBSERVATORY_VAULT_DIR", None)
        else:
            os.environ["OBSERVATORY_VAULT_DIR"] = saved_env


def _write(row: dict, out: dict, src: Path, entry: dict, target: Path, display: str, policy: str) -> None:
    """Read the target again under its vault lock, decide, write; fills `out`. Raises `_Problem` or
    `CarryFailed`."""
    with _target_lock(target, display):
        found = classify(target, entry)
        action = _gate(row, found, policy)
        out.update(same=found["counts"]["same"], conflicts=found["counts"]["conflict"],
                   only_here=found["counts"]["only_here"], conflict_paths=found["conflict_paths"])
        if action == "none":
            out["state"] = "same"
            return
        if action == "keep":
            out["state"] = "kept-existing"
            return
        if action in ("write", "replace"):
            skipped, aside, notes = _install(src, entry, target, display)
            out["not_restored"] += skipped
            out["notes"] += notes
            out.update(state="written" if action == "write" else "replaced",
                       files=len(entry.get("files") or []) + (0 if osprivacy.WINDOWS else len(entry.get("links") or [])),
                       aside=backup_vault._display(aside) if aside is not None else None)
            if action == "replace":
                out.update(conflicts=0, only_here=0, conflict_paths=[])
        elif action == "merge":
            written, skipped, raced = _merge(src, entry, target, display)
            out["not_restored"] += skipped
            out.update(state="merged", files=written, conflicts=out["conflicts"] + len(raced),
                       conflict_paths=sorted(out["conflict_paths"] + raced))
        if out["files"]:
            # Journalled while the lock is still held: the line lands in the store just written.
            out["journal"] = _journal(target, **out.pop("_journal"), files=out["files"], state=out["state"])


def apply_plan(source: Path, plan_: dict, policy: str, *, snapshot: str, taken_on: str | None) -> list[dict]:
    """Carry out a plan; one result row per store. The target is read again under its vault lock first, so
    a store that changed since the plan is decided again (a refusal still writes nothing). Raises
    `CarryRefused` or `CarryFailed`; under `keep` either becomes a `skipped` row with its code as `reason`,
    the target left as it was, and the restore goes on."""
    results = []
    for row in plan_["rows"]:
        out = {"name": row["name"], "state": row["state"], "reason": row["reason"], "target": row["display"],
               "setting": row["setting"], "files": 0, "same": row["counts"]["same"],
               "conflicts": row["counts"]["conflict"], "only_here": row["counts"]["only_here"],
               "not_restored": [], "conflict_paths": row["conflict_paths"], "aside": None, "journal": None,
               "stages_removed": 0, "notes": []}
        results.append(out)
        entry = row["entry"]
        if row["action"] in ("none", "skip") and row["state"] in ("not-in-snapshot", "skipped"):
            continue
        target, display = row["target"], row["display"]
        out["stages_removed"] = sweep_stages(target)
        out["not_restored"] = [dict(r, reason=r.get("reason")) for r in entry.get("not_carried") or []
                               if r.get("reason") != "this-machine-only"]
        out["_journal"] = {"snapshot": snapshot, "taken_on": taken_on}
        try:
            _write(row, out, source / EXTERNAL / row["name"], entry, target, display, policy)
        except _Problem as exc:
            if policy != "keep":
                raise _refusal(exc.code, row["name"], row["setting"], display, exc.counts, target) from None
            out.update(state="skipped", reason=_SAID_AS.get(exc.code, exc.code))
        except CarryFailed as exc:
            if policy != "keep":
                raise
            out.update(state="skipped", reason=exc.code, files=0)
        finally:
            journal = out.pop("_journal", None)
            if journal is not None and out["files"] and out["journal"] is None:
                out["journal"] = _journal(target, **journal, files=out["files"], state=out["state"])
    return results


def restore_summary(plan_: dict | None, results: list[dict], *, names: bool = False) -> dict:
    """The answer's `secret_store`: counts by default; relative paths only when asked (`names`)."""
    if plan_ is None or not plan_["present"]:
        return {"state": "not-in-snapshot"}
    if not results:
        return {"state": "not-applicable"}

    def one(row: dict) -> dict:
        out = {k: row[k] for k in ("name", "state", "reason", "target", "setting", "files", "same", "conflicts",
                                   "only_here", "journal", "stages_removed")}
        out["not_restored"] = len(row["not_restored"])
        if row.get("aside"):
            out["aside"] = row["aside"]
        if row.get("notes"):
            out["notes"] = list(row["notes"])
        if names:
            out["conflict_paths"] = list(row["conflict_paths"])
            out["not_restored_paths"] = [{"path": r["path"], "reason": r["reason"]} for r in row["not_restored"]]
        return out

    first = one(results[0])
    if len(results) > 1:
        first["stores"] = [one(r) for r in results]
    return first


# --- verifying a carried store without putting it back (OBS-394) ---------------------------------------
#
# WHY. A weekly restore check restores the newest backup into a temporary home to prove it opens. The carried
# store could only be checked by writing it where the snapshot's settings name it, which on the machine the
# backup came from is the live store, and on another machine needs `--secret-store-target` for that same
# folder. `full restore --secret-store-verify-into DIR` writes each carried store into `DIR/<name>/` instead,
# checks every file against the manifest's SHA-256 and size, every folder and link, and nothing more, and
# answers `verified` with counts. The folder the settings name is never written, locked, swept or recorded:
# nothing live changes, so no confirmation is asked even for a snapshot from another machine.
#
# WHAT IS LEFT. The verified copy stays at `DIR/<name>/` (folders 0700, files 0600 through osprivacy) and the
# caller removes it; a copy that fails is removed at once. The restore record doctor reads
# (`store/raw/secret-store-restore.json`) is not written: a verification is not a restore, and it must neither
# clear nor raise `backups-secret-store-not-restored`. The answer is the only report.

#: The refusals of `--secret-store-verify-into` (exit 2, nothing written) and its failure (exit 1).
VERIFY_NOT_EMPTY = "secret-store-verify-into-not-empty"
VERIFY_REFUSED = "secret-store-verify-into-refused"
VERIFY_CONFLICT = "secret-store-verify-into-conflict"
VERIFY_FAILED = "secret-store-verify-failed"
VERIFY_UNCHECKED = "secret-store-verify-into-unchecked"
_VERIFY_STEP = ("Choose an empty or absent folder outside the workspace and outside every secret store, and run "
                "project-observatory full restore SNAPSHOT --secret-store-verify-into DIR")


def verify_conflict(policy: str, target: str | None) -> None:
    """Refuse a verification combined with a choice about the live folder: a policy other than the default
    (`--secret-store keep|merge|replace|skip`) or `--secret-store-target`, which only mean something when the
    store is written there."""
    given = [f"--secret-store {policy}"] if policy != "auto" else []
    given += ["--secret-store-target"] if target else []
    if given:
        raise CarryRefused(VERIFY_CONFLICT, (
            f"--secret-store-verify-into writes the carried store only into the folder it names and never where the "
            f"settings point, so {' and '.join(given)} has nothing to act on; nothing was written. Give "
            "--secret-store-verify-into DIR alone to verify, or leave it out to restore the store"))


def _overlaps(a: Path, b: Path) -> bool:
    """Whether one folder is the other or lies inside it, compared resolved and case-folded where the file
    system folds case; a path that cannot be resolved is compared as written."""
    def real(path: Path) -> Path:
        try:
            return path.resolve()
        except (OSError, RuntimeError):
            return Path(os.path.abspath(path))
    ra, rb = real(a), real(b)
    return _inside_folded(ra, rb) or _inside_folded(rb, ra) or _same_identity_overlap(a, b)


def _identities(path: Path) -> list[tuple]:
    """Keys for `path` (first) and each folder holding it, by identity rather than by name: an existing folder
    is its (device, inode), following links; a missing one is its nearest existing folder's identity and the
    missing names below it, case-folded where the file system folds case. Two spellings of one place (a
    linked parent, macOS's `/System/Volumes/Data/...` view of the same folder) get the same keys, which
    `resolve()` alone does not give a firmlink."""
    path = Path(os.path.abspath(path))
    try:
        existing = _nearest_existing(path)
        if existing is None:
            return []
        info = os.stat(existing)
    except (OSError, RuntimeError, ValueError):
        return []
    if not info.st_ino:
        # A volume with no inode numbers (FAT, exFAT, some network shares on Windows) gives every folder the
        # same identity: no identity is read there, and the resolved-name compare decides alone.
        return []
    rest = path.parts[len(existing.parts):]
    base = (info.st_dev, info.st_ino)
    keys = [(base, _fold(Path(*rest[:i]))) for i in range(len(rest), 0, -1)]
    for folder in [existing, *existing.parents]:
        try:
            held = os.stat(folder)
        except OSError:
            continue
        if held.st_ino:
            keys.append(((held.st_dev, held.st_ino), ()))
    return keys


def _same_identity_overlap(a: Path, b: Path) -> bool:
    """Whether `a` is `b`, lies inside it or holds it, compared by identity (`_identities`)."""
    ka, kb = _identities(a), _identities(b)
    if not ka or not kb:
        return False
    return ka[0] in kb or kb[0] in ka


def check_verify_into(into: str | os.PathLike, avoid: list[tuple[Path | None, str]], *,
                      require_empty: bool = True) -> Path:
    """The absolute folder a verification writes into, or `CarryRefused`.

    Refused, in this order: a place no store is ever written to (`refused_location`), and a folder that is,
    holds or lies inside one of `avoid`: the new home, the snapshot, and every folder a setting names for a
    secret store (`secret-store-verify-into-refused`); then a folder that holds anything, or a link or file at
    that path (`secret-store-verify-into-not-empty`). `require_empty` false checks the place alone: the restore
    does so before a passphrase is asked, when the settings' folders are not known yet."""
    try:
        path = Path(os.path.abspath(Path(into).expanduser()))
    except (RuntimeError, KeyError):
        raise CarryRefused(VERIFY_REFUSED, f"--secret-store-verify-into {into} cannot be expanded; nothing was "
                                           f"written. {_VERIFY_STEP}") from None
    shown = backup_vault._display(path)
    why = refused_location(path)
    if why is None:
        for folder, what in avoid:
            if folder is not None and _overlaps(path, folder):
                why = f"{what} ({backup_vault._display(folder)}), or a folder holding it or inside it"
                break
    if why:
        raise CarryRefused(VERIFY_REFUSED, (
            f"--secret-store-verify-into {shown} is {why}; a verified copy is never written there, so the live "
            f"store and the workspace stay as they are, and nothing was written. {_VERIFY_STEP}"))
    if require_empty and os.path.lexists(path):
        try:
            busy = path.is_symlink() or not path.is_dir() or any(path.iterdir())
        except OSError:
            busy = True
        if busy:
            raise CarryRefused(VERIFY_NOT_EMPTY, (
                f"--secret-store-verify-into {shown} is not an empty folder (it holds something, or is a link or a "
                "file); a verified copy is only written where nothing is, and nothing was written. "
                f"{_VERIFY_STEP}"))
    return path


def setting_folders(manifest: dict, sources_list: list[tuple[dict, str]],
                    home: Path) -> list[tuple[Path | None, str]]:
    """Every folder a setting names for a secret store, in each of `sources_list` (the settings' `sources` and
    what they are: the snapshot's after the home rewrite, this workspace's, the default workspace's): every
    non-empty `secret_store` and `secrets`, and every key a carried store was recorded under, expanded and
    absolute (a relative value names no place and is left out). Then the folders the environment names."""
    keys = ["secret_store", "secrets"]
    for entry in manifest.get("externals") or []:
        if isinstance(entry, dict):
            keys += [k for k in entry.get("settings_keys") or [] if isinstance(k, str) and k not in keys]
    out: list[tuple[Path | None, str]] = []
    for sources, what in sources_list:
        for key in keys:
            value = (sources or {}).get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                path = Path(value).expanduser()
            except (RuntimeError, KeyError):
                continue
            if path.is_absolute():
                out.append((Path(os.path.abspath(path)), f"the folder {what} sources.{key} names"))
    for name in (backup_vault.SECRETS_ENV, backup_vault.VAULT_DIR_ENV):
        value = os.environ.get(name)
        if value and value.strip():
            try:
                out.append((Path(os.path.abspath(Path(value).expanduser())), f"the folder {name} names"))
            except (RuntimeError, KeyError):
                continue
    return out


def _verify_copy(root: Path, entry: dict) -> None:
    """Read the copy back: every listed file a regular file of its size and SHA-256, every listed folder a
    folder, every link (POSIX) its recorded target, and nothing unlisted. Raises `_Unreadable`."""
    files = {row["path"]: row for row in entry.get("files") or []}
    dirs = {row["path"] for row in entry.get("directories") or []}
    links = {} if osprivacy.WINDOWS else {row["path"]: row["target"] for row in entry.get("links") or []}
    seen_files, seen_dirs, seen_links = set(), set(), set()
    for current, children, names in os.walk(root, followlinks=False):
        here = Path(current)
        for name in [*children, *names]:
            path = here / name
            rel = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                seen_links.add(rel)
                if links.get(rel) != os.readlink(path):
                    raise _Unreadable("a link's target differs")
            elif stat.S_ISDIR(info.st_mode):
                seen_dirs.add(rel)
            elif stat.S_ISREG(info.st_mode):
                seen_files.add(rel)
                row = files.get(rel)
                if row is None or info.st_size != row["size"] or _digest_file(path)[1] != row["sha256"]:
                    raise _Unreadable("a file's size or SHA-256 differs from the manifest")
            else:
                raise _Unreadable("a special file")
    if seen_files != set(files) or seen_links != set(links) or seen_dirs != dirs:
        raise _Unreadable("missing or unlisted files, folders or links")


def _open_into(into: Path, avoid: list[tuple[Path | None, str]]) -> list[Path]:
    """Make `into` ready right before the first write, checked again (a link or a file put there since the
    plan is refused): an existing folder must still be a real, empty folder and still clear of `avoid`; a
    missing one is made with a plain mkdir (0700), each missing parent too, so a name taken meanwhile fails
    instead of being followed. Returns the folders made, outermost first."""
    check_verify_into(into, avoid)
    made: list[Path] = []
    missing = []
    current = into
    while not os.path.lexists(current):
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    try:
        for folder in reversed(missing):
            os.mkdir(folder, 0o700)
            made.append(folder)
            osprivacy.make_private(folder)
        info = os.lstat(into)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or any(into.iterdir()):
            raise _Unreadable("the folder changed after it was checked")
    except BaseException:
        _remove_made(made)
        raise
    return made


def _remove_made(made: list[Path]) -> None:
    """Remove the folders a verification made, innermost first, only while empty."""
    for folder in reversed(made):
        with contextlib.suppress(OSError):
            os.rmdir(folder)


def discard_verified(result: dict | None) -> None:
    """Remove what a verification wrote: each store's copy, then the folders it made for `into` (OBS-394). For a
    restore that fails after the copy was verified, so it leaves `into` as it found it. Never raises."""
    if not result:
        return
    for root in result.get("written") or []:
        if os.path.lexists(root) and not root.is_symlink():
            shutil.rmtree(root, ignore_errors=True)
    _remove_made(result.get("made") or [])


def verify_into(source: Path, manifest: dict, into: Path, avoid: list[tuple[Path | None, str]] = ()) -> dict:
    """Write each carried store of a verified snapshot into `into/<name>/` and read it back against the
    manifest; `{"present", "into", "rows", "written", "made"}`. Nothing is written when no store was carried.
    `into` is checked again against `avoid` right before the first write (`_open_into`). Raises `CarryFailed`
    (`secret-store-verify-failed`, or `secret-store-write-failed` for a write that failed) after removing every
    copy and folder this run made; an interrupt removes them too."""
    entries = manifest.get("externals")
    result = {"present": isinstance(entries, list), "into": into, "rows": [], "written": [], "made": []}
    if not result["present"]:
        return result
    carried = [e for e in entries if isinstance(e, dict) and e.get("state") == "carried"]
    for entry in entries:
        name = entry.get("name")
        if entry.get("state") != "carried":
            result["rows"].append({"name": name, "state": "not-in-snapshot", "reason": entry.get("reason")})
            continue
        root = into / name
        shown = backup_vault._display(root)
        try:
            if entry is carried[0]:
                result["made"] = _open_into(into, list(avoid))
            try:
                os.mkdir(root, 0o700)
            except FileExistsError:
                # Not this run's folder: refused, and never removed by the cleanup below.
                raise CarryRefused(VERIFY_NOT_EMPTY, (
                    f"--secret-store-verify-into: {shown} appeared while the store was being verified; it is not "
                    f"this run's and was left as it is, and the copies this run made were removed. {_VERIFY_STEP}")
                ) from None
            result["written"].append(root)
            osprivacy.make_private(root)
            not_written = _write_tree(source / EXTERNAL / name, entry, root, made=True)
            _verify_copy(root, entry)
        except (OSError, _Unreadable) as exc:
            # Every copy this run made goes: a half-verified set of secrets is not left behind.
            discard_verified(result)
            code = VERIFY_FAILED if isinstance(exc, _Unreadable) else "secret-store-write-failed"
            what = str(exc) if isinstance(exc, _Unreadable) else type(exc).__name__
            raise CarryFailed(code, (
                f"The carried secret store {name} did not verify at {shown} ({what}); the copies this run made were "
                "removed, the folder the settings name was not touched, and no home was restored")) from None
        except BaseException:
            discard_verified(result)
            raise
        files = entry.get("files") or []
        result["rows"].append({"name": name, "state": "verified", "left_at": shown, "files": len(files),
                               "bytes": sum(row["size"] for row in files),
                               "links": len(entry.get("links") or []) - len(not_written),
                               "directories": len(entry.get("directories") or []),
                               "skipped": len(entry.get("not_carried") or []) + len(not_written)})
    return result


def verify_summary(result: dict) -> dict:
    """The answer's `secret_store` for a verification: the first store's row at the top, `verified` (how many
    stores were verified, so a first store that was not carried does not read as nothing verified), `into`,
    and `stores` when there are several."""
    count = sum(1 for row in result["rows"] if row["state"] == "verified")
    if not result["present"]:
        return {"state": "not-in-snapshot", "verified": 0}
    if not result["rows"]:
        return {"state": "not-applicable", "verified": 0}
    first = {**dict(result["rows"][0]), "verified": count}
    if count:
        first["into"] = backup_vault._display(result["into"])
    if len(result["rows"]) > 1:
        first["stores"] = [dict(row) for row in result["rows"]]
    return first
