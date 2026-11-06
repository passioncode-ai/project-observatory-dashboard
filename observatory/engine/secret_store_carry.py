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
snapshot's `external/` folder against its manifest.

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
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
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
