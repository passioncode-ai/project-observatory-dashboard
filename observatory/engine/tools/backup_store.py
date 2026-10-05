#!/usr/bin/env python3
"""Keep more than one copy of the half of this system git cannot hold.

WHY. `store/observatory.db` is gitignored and the README calls it rebuildable,
which is true of the derived half and false of the rest: the events read out of
commit histories, the ledger revisions the agent wrote, and the review
decisions an operator would make against them do not come back from a fresh
clone. Without this, the only backup is whatever snapshot somebody took by
hand before a migration — stale, and no rotation behind it.

HOW, and why not `cp`. SQLite's own backup API copies a database that is being
written to; a filesystem copy of a WAL-mode file mid-write yields a database
whose `-wal` says one thing and whose pages say another. `sqlite3.Connection.
backup` is the supported way and it is one call.

WHAT IT REFUSES TO DO. It never deletes the newest copy, and it never deletes
anything when the new backup failed — a rotation that prunes before it proves
the replacement exists is how a backup set becomes empty.

    backup_store.py            take one, prune to KEEP
    backup_store.py --list     what exists, newest first
"""
from __future__ import annotations
import pathlib, sqlite3, sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths  # noqa: E402

#: Three days of daily copies. Enough to survive a corruption noticed the next
#: morning; not so many that a 24 MB file becomes a gigabyte nobody notices.
KEEP = 3
PREFIX = "observatory.db.backup-"


#: SQLite writes these beside a database, and they are not backups. The first
#: version of this file globbed `PREFIX + "*"` and counted them: one real copy
#: read as three, so the rotation believed it was over the limit and deleted the
#: only other backup on the machine. Caught by running it once.
COMPANIONS = ("-wal", "-shm", "-journal")


def existing(store: pathlib.Path) -> list[pathlib.Path]:
    """Newest first, and BACKUPS only — never the files SQLite writes beside one."""
    return sorted((p for p in store.glob(PREFIX + "*")
                   if not p.name.endswith(COMPANIONS)), reverse=True)


def take(db: pathlib.Path, stamp: str) -> pathlib.Path:
    dest = db.parent / f"{PREFIX}{stamp}"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def encrypted(db: pathlib.Path, secret: str, base: pathlib.Path) -> int:
    """With a passphrase: copy via the SQLite backup API into the store, encrypt
    that copy into the backups root, drop the plaintext. The legacy plaintext
    copies beside the database go only after an encrypted one is proven."""
    import backup_vault
    root = backup_vault.root_info(base)["path"]
    when = backup_vault.stamp()
    dest = root / f"{backup_vault.DB_KIND}-{when}{backup_vault.DB_SUFFIX}"
    if dest.exists():
        print(f"backup {dest.name} already exists")
        return 0
    plain = take(db, "encrypting-" + when)
    try:
        if plain.stat().st_size == 0:
            print("the copy came out empty; nothing was encrypted or pruned", file=sys.stderr)
            return 1
        backup_vault.encrypt_file(plain, dest, secret, kind=backup_vault.DB_KIND)
    finally:
        for suffix in ("",) + COMPANIONS:
            pathlib.Path(str(plain) + suffix).unlink(missing_ok=True)
    dropped = backup_vault.rotate(root, backup_vault.DB_KIND, backup_vault.DB_SUFFIX)
    legacy = existing(db.parent)
    for old in legacy:
        for suffix in ("",) + COMPANIONS:
            pathlib.Path(str(old) + suffix).unlink(missing_ok=True)
    kept = len(backup_vault.artifacts(root, backup_vault.DB_KIND, backup_vault.DB_SUFFIX))
    print(f"backup {dest.name}: encrypted into {root}, {kept} kept"
          + (f", pruned {len(dropped)}" if dropped else "")
          + (f", removed {len(legacy)} unencrypted legacy copies" if legacy else ""))
    return 0


def due(db: pathlib.Path, base: pathlib.Path, hours: int = 24) -> bool:
    """No copy — encrypted in the root, or legacy beside the database — younger than `hours`."""
    import backup_vault, time
    newest = [p.stat().st_mtime for p in existing(db.parent)]
    try:
        if db.resolve() != (base / "store" / "observatory.db").resolve():
            raise ValueError("not this workspace's store")  # its root is not its own
        root = backup_vault.root_info(base)["path"]
        newest += [p.stat().st_mtime for p in backup_vault.artifacts(root, backup_vault.DB_KIND, backup_vault.DB_SUFFIX)]
    except Exception:  # noqa: BLE001 — an unreadable root means: take one
        pass
    return not newest or time.time() - max(newest) > hours * 3600


def main(argv: list[str]) -> int:
    db = paths.DB
    store = db.parent
    import backup_vault
    import configuration
    base = configuration.home()
    if "--due" in argv:  # the tick's question: exit 0 when a copy is due, 1 when not
        return 0 if (db.is_file() and due(db, base)) else 1
    if "--if-due" in argv and db.is_file() and not due(db, base):
        print("backup not due: a copy younger than 24 hours exists")
        return 0
    # ONLY THE WORKSPACE'S OWN STORE goes to its backups root. A database named by
    # OBSERVATORY_DB elsewhere (a test, another tool) is copied beside itself: three tiny
    # copies of such a database, under this workspace's label, once pushed the real daily
    # copies out of the rotation (seen on a maintainer's machine, 2026-10-05).
    try:
        own = db.resolve() == (base / "store" / "observatory.db").resolve()
    except OSError:
        own = False
    if "--list" not in argv and db.is_file() and not own:
        print(f"{db} is not this workspace's store; its copy stays beside it", file=sys.stderr)
        return local(db)
    if "--list" not in argv and db.is_file():
        secret = backup_vault.passphrase(base)
        if secret:
            try:
                return encrypted(db, secret, base)
            except (OSError, backup_vault.BackupError) as exc:
                # The root is unreachable — on macOS a background job is often
                # refused ~/Documents by privacy controls (TCC). A day without any
                # copy is worse than a day with a local one, so take the local
                # copy, and still exit non-zero: the off-disk backup did NOT happen.
                print(f"encrypted backup failed ({type(exc).__name__}: {exc}); taking a local "
                      "unencrypted copy instead. If this is a privacy refusal, grant the Python "
                      "that runs the tick access to the folder, or choose another root with "
                      "`full configure storage backups PATH`", file=sys.stderr)
                local(db)
                return 1
    if "--list" in argv:
        rows = existing(store)
        if not rows:
            print("no backup exists — the store's history lives on one disk in one file")
            return 0
        for p in rows:
            print(f"  {p.name}  {p.stat().st_size / 1e6:.1f} MB")
        return 0
    if not db.is_file():
        # Nothing to copy is not a failure: a fresh clone has no store, and a
        # backup step that fails there would make the tick red on a machine
        # where nothing is wrong.
        print("no store to back up", file=sys.stderr)
        return 0
    return local(db)


def local(db: pathlib.Path) -> int:
    """The unencrypted copy beside the database: no passphrase, or a root that failed."""
    store = db.parent
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%MZ")
    # SAME STAMP, SAME DAY: a second run inside one minute would otherwise take
    # a second copy and push a good one out of the window.
    if (store / f"{PREFIX}{stamp}").exists():
        print(f"backup {stamp} already exists")
        return 0
    dest = take(db, stamp)
    size = dest.stat().st_size
    if size == 0:
        dest.unlink()
        print("the backup came out empty and was removed; nothing was pruned",
              file=sys.stderr)
        return 1
    # PRUNE ONLY AFTER THE NEW ONE IS PROVEN. Its size is read above, so the
    # replacement exists before anything is removed.
    dropped = []
    for old in existing(store)[KEEP:]:
        # `-wal` and `-shm` beside an old copy are its own, and leaving them
        # behind is how a directory fills with orphans.
        for suffix in ("",) + COMPANIONS:
            companion = old.with_name(old.name + suffix)
            if companion.exists():
                companion.unlink()
        dropped.append(old.name)
    print(f"backup {dest.name}: {size / 1e6:.1f} MB, "
          f"{len(existing(store))} kept" + (f", pruned {len(dropped)}" if dropped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
