"""Encrypted backups outside the workspace: one root, one passphrase, one format.

WHY. Every copy this engine kept lived on the disk it protects: daily database
copies beside the database, workspace snapshots under `<home>/backups`, with no
rotation for the snapshots at all. A lost disk took the backups with it. The
answer is a backups ROOT that can live elsewhere — by default, on macOS,
`~/Documents/Project Observatory/Backups`, which iCloud Desktop & Documents
carries off the machine — and it is only safe to point it there because what
lands there is encrypted: a workspace snapshot holds the `secrets/` directory,
and a synced folder is somebody else's disk.

THE RULE. With a passphrase configured, every artifact written to the root is
encrypted (AES-256-GCM, key derived by scrypt). Without one, NOTHING is written
outside the workspace: copies stay where they always were, plaintext, and
`doctor` says so. A plaintext copy never reaches the root.

THE FORMAT, `OBSENC1`: magic, a length-prefixed JSON header (KDF parameters,
salt, nonce prefix, chunk size — never a secret), then length-prefixed chunks.
Each chunk's nonce is prefix ‖ counter ‖ final-flag and the header is bound as
associated data, so a reordered, truncated, extended or re-headed file fails
authentication rather than decrypting to something shorter.

    root_info(base)          where backups go, and which setting chose it
    passphrase(base)         OBSERVATORY_BACKUP_PASSPHRASE, else secrets/backup-passphrase
    ensure_passphrase(base)  one by default, mirrored outside the workspace (SecretStore)
    candidates(base)         every backup set of a workspace at this path, newest first
    encrypt_file / encrypt_tree / decrypt_to / extract_tree
    after_snapshot(...)      export a finished snapshot, or rotate it locally
    migrate(base)            move the newest legacy copies into the root
"""
from __future__ import annotations

import base64
import contextlib
import datetime
import oslocks
import osprivacy
import hashlib
import io
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import uuid

import configuration as config

MAGIC = b"OBSENC1\n"
FORMAT = 1
#: Plaintext bytes per authenticated chunk. Module-level so a test can make it
#: small enough to exercise many chunks with a few kilobytes.
CHUNK = 1 << 20
#: Per kind, in the root and in the workspace alike: enough to survive a
#: corruption noticed a few days late, not so many that nobody notices the size.
KEEP = 3
MIN_PASSPHRASE = 12
SCRYPT = {"n": 2 ** 17, "r": 8, "p": 1}
ROOT_ENV = "OBSERVATORY_BACKUPS"
PASS_ENV = "OBSERVATORY_BACKUP_PASSPHRASE"
SNAPSHOT_SUFFIX, DB_SUFFIX = ".obsnap", ".obsdb"
#: `daily` is the maintenance job's own (maintenance.SNAPSHOT_KIND), so its rotation never
#: removes a snapshot a person took with `workspace-backup`.
SNAPSHOT_KINDS = ("snapshot", "before-upgrade", "daily")
#: Written into each workspace's folder in the root: the workspace path the copies belong
#: to, so a reinstall at that path finds its own backups and never another's.
OWNER_FILE = ".workspace-path"
DB_KIND = "observatory-db"
LEGACY_DB_PREFIX = "observatory.db.backup-"


class BackupError(config.ConfigurationError):
    """A backup could not be written, proven or read. Never carries a value."""


# --- where ---------------------------------------------------------------

def documents_dir() -> Path:
    return Path.home() / "Documents"


def data_dir() -> Path:
    """Where a per-user program keeps data off macOS (the XDG base directory spec)."""
    value = os.environ.get("XDG_DATA_HOME")
    return Path(value) if value and Path(value).is_absolute() else Path.home() / ".local" / "share"


def _workspace_label(base: Path) -> str:
    marker = config.read_json(base / "workspace.json") if (base / "workspace.json").exists() else {}
    instance = str(marker.get("instance_id", "")).replace("-", "")[:8] or "unknown"
    return f"{base.name}-{instance}"


def root_info(base: Path | None = None) -> dict:
    """The directory encrypted artifacts go to, and which rule chose it.

    Precedence: OBSERVATORY_BACKUPS, then `storage.backups` in settings.json, then
    the platform default. A chosen root always gets a per-workspace subfolder, so
    two workspaces sharing one Documents folder never rotate each other's files.
    """
    base = base or config.home()
    value = os.environ.get(ROOT_ENV)
    if value:
        chosen = Path(value).expanduser()
        if not chosen.is_absolute():
            raise config.ConfigurationError(f"{ROOT_ENV} must be an absolute path")
        source = "environment"
    elif config.load(base).get("storage", {}).get("backups"):
        chosen, source = Path(config.load(base)["storage"]["backups"]).expanduser(), "settings"
    elif sys.platform == "darwin" and documents_dir().is_dir():
        chosen, source = documents_dir() / "Project Observatory" / "Backups", "default-documents"
    else:
        # Off macOS, or with no ~/Documents: beside the workspace, never inside it, so
        # deleting the workspace (or reinstalling into it) leaves the copies behind.
        chosen, source = data_dir() / "project-observatory-backups", "default-data"
    path = chosen / _workspace_label(base)
    inside = path == base or base in path.parents
    return {"path": path, "source": source, "inside_workspace": inside}


def candidates(base: Path) -> list[dict]:
    """Every snapshot this machine holds for a workspace at `base`'s path, newest first.

    A reinstall writes a new `instance_id`, so the label of the set a lost workspace left
    behind differs from the new one; the folder name `<home name>-<id>` is what ties them.
    Each candidate is one snapshot (daily, taken by a person, or taken before an update)."""
    roots, seen = [], set()
    try:
        roots.append(root_info(base)["path"].parent)
    except config.ConfigurationError:
        pass
    if sys.platform == "darwin":
        roots.append(documents_dir() / "Project Observatory" / "Backups")
    roots.append(data_dir() / "project-observatory-backups")
    found = []
    for root in roots:
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        exact = re.compile(rf"{re.escape(base.name)}-(?:[0-9a-f]{{8}}|unknown)")
        for folder in root.iterdir():
            if not exact.fullmatch(folder.name) or not folder.is_dir() or folder.is_symlink():
                continue
            owner = folder / OWNER_FILE
            if owner.is_file():
                try:
                    if owner.read_text(encoding="utf-8").strip() != str(base.resolve()):
                        continue  # the same name at another path is another workspace
                except OSError:
                    continue
            snaps = sorted([f for kind in ("snapshot", "before-upgrade", "before-update", "daily")
                            for f in artifacts(folder, kind, SNAPSHOT_SUFFIX)],
                           key=lambda f: _artifact_stamp(f.name))
            # Every snapshot, so a damaged newest one still leaves an older one to restore.
            found += [{"label": folder.name, "folder": folder, "snapshot": snap,
                       "stamp": _artifact_stamp(snap.name)} for snap in snaps]
    return sorted(found, key=lambda c: c["stamp"], reverse=True)


def _artifact_stamp(name: str) -> str:
    """`<kind>-<YYYYmmddTHHMMSSZ>-…` → the stamp, so kinds sort together by time."""
    for part in name.split("-"):
        if len(part) == 16 and part[8] == "T" and part.endswith("Z"):
            return part
    return ""


# --- the passphrase ------------------------------------------------------

def passphrase_file(base: Path | None = None) -> Path:
    return (base or config.home()) / "secrets" / "backup-passphrase"


def passphrase(base: Path | None = None) -> str | None:
    """The configured passphrase, or None. A readable-by-others file is refused."""
    value = os.environ.get(PASS_ENV)
    if value:
        return value
    file = passphrase_file(base)
    if file.is_symlink():
        raise BackupError("The backup passphrase file must not be a symbolic link")
    if not file.exists():
        return None
    if not osprivacy.private(file):
        raise BackupError(f"{file} is readable by group or others; chmod 600 it")
    text = file.read_text(encoding="utf-8").rstrip("\n")
    return text or None


def set_passphrase(base: Path, value: str) -> Path:
    if len(value) < MIN_PASSPHRASE:
        raise BackupError(f"A backup passphrase needs at least {MIN_PASSPHRASE} characters")
    file = passphrase_file(base)
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=".backup-passphrase-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(value + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, file)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return file


class SecretStore:
    """Where a workspace's backup passphrase is kept OUTSIDE the workspace, so a deleted
    workspace or a reinstall can still open the backups it left (decision D5,
    docs/runs/2026-10-05-auto-update).

    macOS: the login Keychain, item service "Project Observatory backups", account = the
    workspace label. Elsewhere: the Secret Service through `secret-tool`, or, without it,
    an owner-only file under ~/.config/project-observatory/backup-passphrases/.
    A value travels on stdin and is never an argument: `security -i` reads its command
    from stdin, `secret-tool store` reads the secret from stdin. The Keychain copy is
    hex-encoded, so no character of a person's passphrase meets `security`'s parser."""

    SERVICE = "Project Observatory backups"
    TOOL_SERVICE = "project-observatory-backups"

    def __init__(self, runner=subprocess.run, platform: str | None = None, which=shutil.which):
        self.runner, self.which = runner, which
        self.platform = platform or sys.platform

    def kind(self) -> str:
        if self.platform == "darwin" and self.which("security"):
            return "keychain"
        if self.platform != "darwin" and self.which("secret-tool"):
            return "secret-service"
        return "file"

    @staticmethod
    def _safe(label: str) -> str:
        if not label or not all(c.isalnum() or c in "._- " for c in label):
            raise BackupError("A workspace label for the passphrase store must be letters, digits, '.', '_', '-' or spaces")
        return label

    def _file(self, label: str) -> Path:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base and Path(base).is_absolute() else Path.home() / ".config"
        return root / "project-observatory" / "backup-passphrases" / label

    #: `security find-generic-password` exits 44 when no such item exists; every other
    #: failure (a locked Keychain, interaction not allowed, a timeout) is NOT absence.
    KEYCHAIN_NOT_FOUND = 44

    def _run(self, argv: list[str], data: str | None = None) -> tuple[int, str]:
        code, out, _ = self._run_full(argv, data)
        return code, out

    def _run_full(self, argv: list[str], data: str | None = None) -> tuple[int, str, str]:
        try:
            p = self.runner(argv, input=data, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 125, "", type(exc).__name__
        return p.returncode, p.stdout or "", p.stderr or ""

    def put(self, label: str, value: str, *, replace: bool = False) -> str:
        """Store `value` under `label`. Without `replace` an existing item makes the
        write FAIL rather than be overwritten (audit A01 review): the only path that
        replaces a stored passphrase is one that has just read it and kept it."""
        label, kind = self._safe(label), self.kind()
        if kind == "keychain":
            encoded = "hex:" + value.encode("utf-8").hex()
            code, _ = self._run(["security", "-i"],
                                f'add-generic-password -a "{label}" -s "{self.SERVICE}" -w {encoded}'
                                + (" -U" if replace else "") + "\n")
            if code != 0:
                raise BackupError("the login Keychain refused the backup passphrase")
            return kind
        if kind == "secret-service":
            if not replace:
                code, out, err = self._run_full(["secret-tool", "lookup", "service", self.TOOL_SERVICE,
                                                 "account", label])
                if code == 0 and out.rstrip("\n"):
                    raise BackupError(f"the Secret Service already holds a passphrase for {label}")
            code, _ = self._run(["secret-tool", "store", f"--label={self.SERVICE} ({label})",
                                 "service", self.TOOL_SERVICE, "account", label], value)
            if code == 0:
                return kind
            # `secret-tool` is installed but no keyring answers (a headless machine): the
            # owner-only file keeps the copy outside the workspace instead (review F7).
            kind = "file"
        file = self._file(label)
        if not replace and (file.exists() or file.is_symlink()):
            raise BackupError(f"{file} already exists")
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(file.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=".passphrase-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(value + "\n")
            os.replace(tmp, file)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return kind

    def get(self, label: str) -> str | None:
        """The stored value, or None when the store holds NO item for `label`.

        A store that could not answer raises `BackupError` instead (audit A01 review):
        a Keychain that timed out or is locked looked exactly like "no item", and the
        caller then generated a new passphrase and wrote it over the only copy every
        existing backup opens with."""
        label, kind = self._safe(label), self.kind()
        if kind == "keychain":
            code, out, err = self._run_full(["security", "find-generic-password", "-a", label,
                                             "-s", self.SERVICE, "-w"])
            if code == self.KEYCHAIN_NOT_FOUND:
                return None
            text = out.strip()
            if code != 0 or not text:
                raise BackupError(f"the login Keychain did not answer (exit {code}"
                                  + (f", {err.strip()[:120]}" if err.strip() else "") + ")")
            if text.startswith("hex:"):
                try:
                    return bytes.fromhex(text[4:]).decode("utf-8")
                except ValueError:
                    raise BackupError(f"the Keychain item for {label} is not a passphrase this engine wrote") from None
            return text
        keyring_error = None
        if kind == "secret-service":
            code, out, err = self._run_full(["secret-tool", "lookup", "service", self.TOOL_SERVICE,
                                             "account", label])
            if code == 0 and out.rstrip("\n"):
                return out.rstrip("\n")
            # `secret-tool lookup` exits 1 with nothing on stderr when the item is absent;
            # a message means no keyring answered. `put` then fell back to the owner-only
            # file, so the file is read before that silence is called an error.
            if code != 0 and err.strip():
                keyring_error = f"the Secret Service did not answer: {err.strip()[:120]}"
        file = self._file(label)
        if file.is_symlink() or (file.exists() and not file.is_file()):
            raise BackupError(f"{file} is not a plain file; refusing to read it")
        if file.is_file():
            if not osprivacy.private(file):
                raise BackupError(f"{file} is readable by others; refusing to read it")
            value = file.read_text(encoding="utf-8").rstrip("\n")
            if value:
                return value
        if keyring_error:
            raise BackupError(keyring_error)
        return None


@contextlib.contextmanager
def _passphrase_lock(base: Path):
    folder = base / "secrets"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = osprivacy.open(folder / ".passphrase.lock", os.O_CREAT | os.O_RDWR | osprivacy.NOFOLLOW, 0o600)
    try:
        oslocks.flock(fd, oslocks.LOCK_EX)
        yield
    finally:
        os.close(fd)


PREVIOUS = "-previous"
#: How many earlier passphrases a label keeps. A person changing it more often than
#: this inside the retention window is far outside any measured use; past it the
#: change is refused rather than an old passphrase dropped.
PREVIOUS_LIMIT = 64


def _previous_labels(label: str):
    """`<label>-previous`, then `<label>-previous-2`, `-3`… — append-only, one per change."""
    yield label + PREVIOUS
    for n in range(2, PREVIOUS_LIMIT + 1):
        yield f"{label}{PREVIOUS}-{n}"


def _keep_previous(store: "SecretStore", label: str, value: str) -> None:
    """Append `value` to the label's earlier passphrases unless it is already there.
    Never overwrites one: two changes inside the retention window once left only the
    most recent earlier passphrase, and backups taken under the first stopped opening
    (audit A01 review)."""
    for name in _previous_labels(label):
        held = store.get(name)
        if held == value:
            return
        if held is None:
            store.put(name, value)
            return
    raise BackupError(f"{PREVIOUS_LIMIT} earlier passphrases are already kept for {label}; "
                      "refusing to drop one")


def ensure_passphrase(base: Path, store: SecretStore | None = None) -> dict:
    """A passphrase exists, and a copy of it lives outside the workspace.

    With none configured one is generated, so backups are encrypted and leave the
    workspace by default. An existing one — typed by a person or generated earlier — is
    mirrored to the store. A passphrase given through OBSERVATORY_BACKUP_PASSPHRASE is the
    person's to keep and is not copied. The result names where it is kept, never the value.

    THE STORE IS NEVER OVERWRITTEN WITH A VALUE THAT COULD LOCK BACKUPS OUT (audit A01):
    - the file is gone but the store remembers this workspace's passphrase: the file is
      restored from the store, instead of a new one being generated over it;
    - the file is gone and the store cannot answer (locked, timed out): nothing is
      generated — a new passphrase now could not be told from the old one later — and
      the result carries a warning; backups wait until the store answers;
    - the file and the store disagree (a person ran `backup-passphrase set`): the stored
      value is appended to the earlier passphrases (`<label>-previous`, `-previous-2`…)
      before the new one replaces it, because backups taken before the change open only
      with it. `restore_latest` tries every one."""
    store = store or SecretStore()
    if os.environ.get(PASS_ENV):
        return {"passphrase": "environment", "kept_outside": None}
    label = _workspace_label(base)
    with _passphrase_lock(base):
        current = passphrase(base)
        try:
            stored, store_error = store.get(label), None
        except (BackupError, OSError) as exc:
            stored, store_error = None, exc
        state = "configured"
        if current is None and stored:
            set_passphrase(base, stored)
            current, state = stored, "recovered"
        elif current is None and store_error is not None:
            return {"passphrase": "missing", "kept_outside": None,
                    "warning": f"no passphrase in {passphrase_file(base)} and the store outside the "
                               f"workspace could not be read ({store_error}); none was generated, so "
                               "no backup can be locked out — backups resume once the store answers"}
        elif current is None:
            current, state = secrets.token_urlsafe(32), "generated"
            set_passphrase(base, current)
    if store_error is not None:
        return {"passphrase": state, "kept_outside": None,
                "warning": f"the passphrase is only in {passphrase_file(base)}: {store_error}"}
    try:
        if stored is None:
            kind = store.put(label, current)
        elif stored != current:
            _keep_previous(store, label, stored)
            kind = store.put(label, current, replace=True)
        else:
            kind = store.kind()
    except (BackupError, OSError) as exc:
        return {"passphrase": state, "kept_outside": None,
                "warning": f"the passphrase is only in {passphrase_file(base)}: {exc}"}
    return {"passphrase": state, "kept_outside": kind, "label": label}


def stored_passphrases(store: "SecretStore", label: str) -> list[str]:
    """Every passphrase the store keeps for a label: the current one, then each earlier
    one in the order it was replaced. A store that cannot answer contributes nothing."""
    out = []
    for name in (label, *_previous_labels(label)):
        try:
            value = store.get(name)
        except (BackupError, OSError):
            value = None
        if value is None and name != label:
            break
        if value and value not in out:
            out.append(value)
    return out


def require_passphrase(base: Path | None = None, *, prompt: bool = False) -> str:
    value = passphrase(base)
    if value:
        return value
    if prompt and sys.stdin.isatty():
        import getpass
        value = getpass.getpass("Backup passphrase: ")
        if value:
            return value
    raise BackupError(f"No backup passphrase: set {PASS_ENV}, or run "
                      "`project-observatory full backup-passphrase set` in the workspace")


# --- the format ----------------------------------------------------------

def _key(secret: str, header: dict) -> bytes:
    salt = base64.b64decode(header["salt"])
    return hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=header["n"], r=header["r"],
                          p=header["p"], maxmem=512 * 1024 * 1024, dklen=32)


def _aead(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key)


def _nonce(prefix: bytes, counter: int, final: bool) -> bytes:
    if counter >= 2 ** 32:
        raise BackupError("Backup too large for one file")
    return prefix + struct.pack(">I", counter) + (b"\x01" if final else b"\x00")


class _Writer(io.RawIOBase):
    """Buffers plaintext and emits authenticated chunks, holding one back so the
    last one can be marked final."""

    def __init__(self, out, secret: str, meta: dict):
        header = {"format": FORMAT, "cipher": "AES-256-GCM", "kdf": "scrypt", **SCRYPT,
                  "salt": base64.b64encode(os.urandom(16)).decode(),
                  "nonce_prefix": base64.b64encode(os.urandom(7)).decode(),
                  "chunk": CHUNK, **meta}
        self.raw_header = json.dumps(header, sort_keys=True).encode()
        self.aad = MAGIC + self.raw_header
        self.aes = _aead(_key(secret, header))
        self.prefix = base64.b64decode(header["nonce_prefix"])
        self.chunk = header["chunk"]
        self.out, self.buffer, self.pending, self.counter = out, bytearray(), None, 0
        out.write(MAGIC + struct.pack(">I", len(self.raw_header)) + self.raw_header)

    def writable(self):
        return True

    def _emit(self, data: bytes, final: bool) -> None:
        sealed = self.aes.encrypt(_nonce(self.prefix, self.counter, final), bytes(data), self.aad)
        self.out.write(struct.pack(">I", len(sealed)) + sealed)
        self.counter += 1

    def write(self, data) -> int:
        self.buffer += data
        while len(self.buffer) > self.chunk:
            if self.pending is not None:
                self._emit(self.pending, False)
            self.pending, self.buffer = bytes(self.buffer[:self.chunk]), self.buffer[self.chunk:]
        return len(data)

    def finish(self) -> None:
        if self.pending is not None:
            if self.buffer:
                self._emit(self.pending, False)
                self._emit(self.buffer, True)
            else:
                self._emit(self.pending, True)
        else:
            self._emit(self.buffer, True)


class _Reader(io.RawIOBase):
    """Authenticated chunk stream → plaintext; raises on any mismatch."""

    def __init__(self, stream, secret: str):
        if stream.read(len(MAGIC)) != MAGIC:
            raise BackupError("Not an Observatory encrypted backup")
        size = struct.unpack(">I", self._exact(stream, 4))[0]
        if size > 64 * 1024:
            raise BackupError("Encrypted backup header is implausible")
        raw = self._exact(stream, size)
        try:
            header = json.loads(raw)
        except ValueError:
            raise BackupError("Encrypted backup header is damaged") from None
        if header.get("format") != FORMAT or header.get("cipher") != "AES-256-GCM" or header.get("kdf") != "scrypt":
            raise BackupError("Unsupported encrypted backup format")
        self.header, self.aad, self.stream = header, MAGIC + raw, stream
        self.aes = _aead(_key(secret, header))
        self.prefix = base64.b64decode(header["nonce_prefix"])
        self.counter, self.done, self.left = 0, False, b""
        self.next = self._frame()

    @staticmethod
    def _exact(stream, n: int) -> bytes:
        data = stream.read(n)
        if len(data) != n:
            raise BackupError("Encrypted backup is truncated")
        return data

    def _frame(self) -> bytes | None:
        head = self.stream.read(4)
        if not head:
            return None
        if len(head) != 4:
            raise BackupError("Encrypted backup is truncated")
        size = struct.unpack(">I", head)[0]
        if size > self.header.get("chunk", CHUNK) + 16:
            raise BackupError("Encrypted backup chunk is implausible")
        return self._exact(self.stream, size)

    def _open_next(self) -> bytes:
        from cryptography.exceptions import InvalidTag
        sealed, self.next = self.next, self._frame()
        if sealed is None:
            raise BackupError("Encrypted backup is truncated")
        final = self.next is None
        try:
            plain = self.aes.decrypt(_nonce(self.prefix, self.counter, final), sealed, self.aad)
        except InvalidTag:
            raise BackupError("Encrypted backup failed authentication: wrong passphrase, "
                              "or the file was truncated or changed") from None
        self.counter += 1
        self.done = final
        return plain

    def readable(self):
        return True

    def readinto(self, target) -> int:
        while not self.left and not self.done:
            self.left = self._open_next()
        n = min(len(target), len(self.left))
        target[:n] = self.left[:n]
        self.left = self.left[n:]
        return n


def stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sync_dir(path: Path) -> None:
    fd = osprivacy.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(dest: Path, fill, secret: str, meta: dict) -> Path:
    """Encrypt into a temporary sibling, prove it decrypts, then rename into place."""
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix="." + dest.name + ".", suffix=".tmp")
    tmp = Path(tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as out:
            writer = _Writer(out, secret, {**meta, "application_version": config.VERSION,
                                           "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
            fill(writer)
            writer.finish()
            out.flush()
            os.fsync(out.fileno())
        verify_file(tmp, secret)
        os.replace(tmp, dest)
        _sync_dir(dest.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def encrypt_file(source: Path, dest: Path, secret: str, *, kind: str) -> Path:
    def fill(writer):
        with source.open("rb") as inp:
            shutil.copyfileobj(inp, writer, CHUNK)
    return _publish(dest, fill, secret, {"kind": kind, "content": "file"})


def encrypt_tree(directory: Path, dest: Path, secret: str, *, kind: str) -> Path:
    def fill(writer):
        with tarfile.open(fileobj=writer, mode="w|gz", format=tarfile.PAX_FORMAT) as tar:
            for path in sorted(directory.rglob("*")):
                if path.is_symlink():
                    raise BackupError("Refusing to encrypt a tree containing a symbolic link")
                tar.add(path, arcname=path.relative_to(directory).as_posix(), recursive=False)
    return _publish(dest, fill, secret, {"kind": kind, "content": "tar+gzip"})


def verify_file(path: Path, secret: str) -> dict:
    """Authenticate every chunk without keeping the plaintext."""
    with path.open("rb") as stream:
        reader = _Reader(stream, secret)
        while reader.read(CHUNK):
            pass
        if stream.read(1):
            raise BackupError("Encrypted backup has trailing data")
        return reader.header


def decrypt_to(path: Path, out, secret: str) -> dict:
    with path.open("rb") as stream:
        reader = _Reader(stream, secret)
        shutil.copyfileobj(reader, out, CHUNK)
        if stream.read(1):
            raise BackupError("Encrypted backup has trailing data")
        return reader.header


def extract_tree(path: Path, parent: Path, secret: str) -> Path:
    """Decrypt a tree artifact into a new private directory under `parent`.

    The whole file is authenticated BEFORE extraction, so a damaged or forged
    archive never writes a byte."""
    verify_file(path, secret)
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = Path(tempfile.mkdtemp(prefix=".decrypted-", dir=parent))  # paths-check: allow — removed below on any failure; on success the caller renames or removes it
    try:
        with path.open("rb") as stream:
            reader = _Reader(stream, secret)
            with tarfile.open(fileobj=io.BufferedReader(reader, CHUNK), mode="r|gz") as tar:
                for member in tar:
                    name = Path(member.name)
                    if member.issym() or member.islnk() or not (member.isfile() or member.isdir()) \
                            or name.is_absolute() or ".." in name.parts:
                        raise BackupError("Encrypted snapshot contains an unsafe entry")
                    dest = target / name
                    if member.isdir():
                        dest.mkdir(parents=True, exist_ok=True, mode=0o700)
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    source = tar.extractfile(member)
                    with os.fdopen(osprivacy.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
                        shutil.copyfileobj(source, out, CHUNK)
        return target
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


# --- rotation, export, migration ----------------------------------------

def artifacts(root: Path, kind: str, suffix: str) -> list[Path]:
    """Oldest first: names begin with a UTC stamp, so name order is age order."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob(f"{kind}-*{suffix}") if p.is_file() and not p.name.startswith("."))


def rotate(root: Path, kind: str, suffix: str, keep: int = KEEP) -> list[str]:
    found = artifacts(root, kind, suffix)
    dropped = []
    for old in found[:-keep] if len(found) > keep else []:
        old.unlink()
        dropped.append(old.name)
    return dropped


def local_snapshots(base: Path, kind: str) -> list[Path]:
    folder = base / "backups"
    if not folder.is_dir():
        return []
    found = [p for p in folder.iterdir() if p.is_dir() and not p.is_symlink() and p.name.startswith(kind + "-")]
    return sorted(found, key=lambda p: p.stat().st_mtime_ns)


def rotate_local(base: Path, kind: str, keep: int = KEEP) -> list[str]:
    found = local_snapshots(base, kind)
    dropped = []
    for old in found[:-keep] if len(found) > keep else []:
        shutil.rmtree(old)
        dropped.append(old.name)
    return dropped


def _snapshot_stamp(directory: Path) -> str:
    try:
        created = config.read_json(directory / "manifest.json").get("created_at", "")
        moment = datetime.datetime.fromisoformat(created)
        return moment.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except (OSError, ValueError, TypeError, config.ConfigurationError):
        return datetime.datetime.fromtimestamp(directory.stat().st_mtime, datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _mark_owner(root: Path, base: Path) -> None:
    owner = root / OWNER_FILE
    try:
        if not owner.exists():
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            owner.write_text(str(base.resolve()) + "\n", encoding="utf-8")
    except OSError:
        pass  # an unmarked folder is still found by its exact label


def export_snapshot(base: Path, directory: Path, kind: str, secret: str) -> Path:
    root = root_info(base)["path"]
    _mark_owner(root, base)
    dest = root / f"{kind}-{_snapshot_stamp(directory)}-{uuid.uuid4().hex[:8]}{SNAPSHOT_SUFFIX}"
    encrypt_tree(directory, dest, secret, kind=kind)
    shutil.rmtree(directory)
    rotate(root, kind, SNAPSHOT_SUFFIX)
    return dest


def after_snapshot(base: Path, directory: Path, kind: str) -> dict:
    """What happens to a snapshot once it is proven: exported encrypted, or kept
    locally under rotation. A failed export keeps the local copy and says why."""
    try:
        secret = passphrase(base)
    except BackupError as exc:
        secret, problem = None, str(exc)
    else:
        problem = None
    if secret:
        try:
            dest = export_snapshot(base, directory, kind, secret)
            rotate_local(base, kind)
            return {"snapshot": str(dest), "encrypted": True}
        except (BackupError, OSError, tarfile.TarError) as exc:
            problem = f"{type(exc).__name__}: {exc}"
    rotate_local(base, kind)
    out = {"snapshot": str(directory), "encrypted": False}
    if problem:
        out["export_error"] = problem
    return out


def legacy_db_copies(base: Path) -> list[Path]:
    store = base / "store"
    if not store.is_dir():
        return []
    return sorted(p for p in store.glob(LEGACY_DB_PREFIX + "*")
                  if p.is_file() and not p.name.endswith(("-wal", "-shm", "-journal")))


def migrate(base: Path) -> dict:
    """Move the newest KEEP legacy copies of each kind into the root, encrypted.

    A copy that does not verify is reported and left where it is — deleting a
    backup nobody could read is still deleting the only evidence it existed."""
    import workspace_upgrade
    secret = require_passphrase(base)
    report = {"root": str(root_info(base)["path"]), "exported": 0, "removed": 0, "skipped": []}
    for kind in SNAPSHOT_KINDS:
        found = local_snapshots(base, kind)
        valid = []
        for directory in found:
            try:
                workspace_upgrade.verify_snapshot(directory)
                valid.append(directory)
            except (config.ConfigurationError, OSError, ValueError) as exc:
                report["skipped"].append({"path": str(directory), "reason": f"{type(exc).__name__}: {exc}"})
        for directory in valid[-KEEP:]:
            export_snapshot(base, directory, kind, secret)
            report["exported"] += 1
        for directory in valid[:-KEEP]:
            shutil.rmtree(directory)
            report["removed"] += 1
    copies = legacy_db_copies(base)
    root = root_info(base)["path"]
    for copy in copies[-KEEP:]:
        when = datetime.datetime.fromtimestamp(copy.stat().st_mtime, datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        encrypt_file(copy, root / f"{DB_KIND}-{when}{DB_SUFFIX}", secret, kind=DB_KIND)
        report["exported"] += 1
    # Every legacy copy goes once the newest ones are proven in the root: the
    # older ones are past the rotation window the root now enforces.
    for copy in copies:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(copy) + suffix).unlink(missing_ok=True)
    report["removed"] += max(0, len(copies) - KEEP)
    rotate(root, DB_KIND, DB_SUFFIX)
    return report


def status(base: Path) -> dict:
    """For doctor: where, whether encrypted, what exists. Never a value."""
    warnings = []
    try:
        info = root_info(base)
    except config.ConfigurationError as exc:
        return {"encrypted": False, "warnings": [str(exc)]}
    try:
        configured = passphrase(base) is not None
    except BackupError as exc:
        configured, warnings = False, [str(exc)]
    root = info["path"]
    if not configured:
        warnings.append("no backup passphrase: copies stay inside the workspace, unencrypted, on this "
                        "disk only; run `project-observatory full backup-passphrase set`")
    if configured and info["inside_workspace"]:
        # Encryption protects a copy that leaves this disk; a root inside the
        # workspace never leaves it. The fallback root (<home>/backups, chosen when
        # there is no ~/Documents or off macOS) is the usual way to get here, and
        # it puts the copies in the same tree as the key that opens them.
        beside = (f"beside the passphrase in {passphrase_file(base)}" if os.environ.get(PASS_ENV) is None
                  else f"on the same disk as the workspace ({PASS_ENV} supplies the passphrase)")
        warnings.append(f"the backups root {root} is inside the workspace, {beside}: a lost or stolen disk "
                        "takes the copies and their key together. Point it at another disk or a synced "
                        "folder: `project-observatory full configure storage backups /absolute/path` "
                        f"(or {ROOT_ENV}); new copies go there, and the ones already here stay until moved by hand")
    legacy = local_snapshots(base, "snapshot") + local_snapshots(base, "before-upgrade") + local_snapshots(base, "daily")
    # A rolled-back update keeps the changed workspace beside the restored one: a full,
    # UNENCRYPTED copy, secrets/ included, that nothing removes (audit A18).
    failed = sorted(p for p in base.parent.glob(f"{base.name}.failed-update-*") if p.is_dir())
    if failed:
        warnings.append(f"{len(failed)} unencrypted copy(ies) of this workspace from a rolled-back update "
                        f"remain beside it ({failed[0].name}…); they hold secrets/. Delete them once the "
                        "restored workspace is confirmed")
    if configured and legacy:
        warnings.append(f"{len(legacy)} unencrypted snapshot(s) remain in {base / 'backups'}; "
                        "run `project-observatory full backups migrate`")
    latest = {}
    for kind, suffix in ((DB_KIND, DB_SUFFIX), ("snapshot", SNAPSHOT_SUFFIX), ("before-upgrade", SNAPSHOT_SUFFIX),
                         ("before-update", SNAPSHOT_SUFFIX), ("daily", SNAPSHOT_SUFFIX)):
        found = artifacts(root, kind, suffix)
        latest[kind] = {"count": len(found), "newest": found[-1].name if found else None}
    return {"root": str(root), "root_source": info["source"], "inside_workspace": info["inside_workspace"],
            "encrypted": configured,
            "passphrase": "configured" if configured else "missing", "keep_per_kind": KEEP,
            "artifacts": latest, "local_unencrypted_snapshots": len(legacy), "warnings": warnings}
