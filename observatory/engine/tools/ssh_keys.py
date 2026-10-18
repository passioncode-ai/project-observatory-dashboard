#!/usr/bin/env python3
"""SSH keys on this machine: list them, create one, copy its public line, send it where it goes.
The private key is never shown or moved.

    project-observatory full ssh-keys [list] [--json] [--no-github]
    project-observatory full ssh-keys show NAME
    project-observatory full ssh-keys copy NAME
    project-observatory full ssh-keys create NAME [--comment TEXT] [--no-passphrase] [--no-keychain]
                                                  [--host ALIAS --hostname HOST [--user USER] [--port N]]
    project-observatory full ssh-keys send NAME --to github [--as authentication|signing|both] [--title TEXT]
    project-observatory full ssh-keys send NAME --to server:ALIAS [--verify] [--interactive]
    project-observatory full ssh-keys findings [--json]

The design, and why each step is the way it is: docs/design/SSH-KEYS.md (PB-163, OBS-110).
Scenarios: OSS-62–67 in docs/ux/portable-scenarios.md.

WHY A DOOR AND NOT A HOWTO. Making a key, copying its public line and adding it to GitHub or a
server took several commands, and the moment between them is when a private key gets pasted
into the wrong window. Here every path that shows, copies or sends anything reads `NAME.pub`
and checks it parsed as exactly one public key line (and, when the private file is there, that
it is that key's public half). The private file is opened only to read its clear header: the
cipher name says whether it has a passphrase, and the public key written in clear after it gives
the fingerprint. The read stops there, before the private section (`private_header`).

The system's own tools do the work: `ssh-keygen` (fingerprints from the `.pub`, generation),
`ssh-add` (the agent, the macOS Keychain), `ssh` (the access that already works) and `gh`
(GitHub). `OBSERVATORY_SSH_KEYGEN`, `OBSERVATORY_SSH_ADD`, `OBSERVATORY_SSH`, `OBSERVATORY_GH`
and `OBSERVATORY_CLIPBOARD` name other binaries (the tests' fakes); `OBSERVATORY_CLIPBOARD=none`
says there is no clipboard.

Exit codes: 0 done; 1 the provider or the server refused, or verification failed; 2 refused
before anything changed; 3 done without the clipboard; 4 done, receipt not written.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import glob
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import credential_shape  # noqa: E402
import osprivacy  # noqa: E402  — O_NOFOLLOW and friends on every OS

CMD = "project-observatory full ssh-keys"
#: A key name `create` accepts: one file name in ~/.ssh.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
#: A key name the other commands accept: a file in ~/.ssh or one folder below it.
KEY_REF = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,63}/)?[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
#: `server:ALIAS`: a Host of ~/.ssh/config or [user@]host. Never a leading `-`, never a space
#: or a shell character, and it is passed after `--`, so it cannot become an ssh option.
HOST_ARG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._@%+:\[\]-]{0,252}")
CONFIG_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,252}")
USER_WORD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")
KEY_TYPES = {
    "ssh-ed25519": "ED25519", "ssh-rsa": "RSA", "ssh-dss": "DSA",
    "ecdsa-sha2-nistp256": "ECDSA", "ecdsa-sha2-nistp384": "ECDSA", "ecdsa-sha2-nistp521": "ECDSA",
    "sk-ssh-ed25519@openssh.com": "ED25519-SK", "sk-ecdsa-sha2-nistp256@openssh.com": "ECDSA-SK",
}
#: Composed: the commit gate refuses a literal private-key header in the engine's files.
BEGIN = "-----BEGIN "
PRIVATE_TAIL = "PRIVATE" + " KEY-----"
OPENSSH_MAGIC = b"openssh-key-v1\0"
#: The longest BEGIN line a private key has (the PKCS#8 encrypted one, 37 characters), with room.
FIRST_LINE_MAX = 64
#: An OpenSSH header field (cipher, kdf, kdf options) longer than this is not a key we read.
FIELD_MAX = 64
#: The longest public key of each type, in bytes, as the OpenSSH header stores it. The key's
#: type is read first and its length field is held to this cap BEFORE the key is read, so a
#: length field that lies cannot carry the read into the private section. ed25519 is exact (51);
#: RSA allows a 16384-bit modulus; DSA keys are 1024-bit in OpenSSH; security keys allow a
#: 255-byte application string.
PUBLIC_BLOB_MAX = {
    "ssh-ed25519": 51, "sk-ssh-ed25519@openssh.com": 325,
    "ecdsa-sha2-nistp256": 104, "ecdsa-sha2-nistp384": 136, "ecdsa-sha2-nistp521": 172,
    "sk-ecdsa-sha2-nistp256@openssh.com": 378, "ssh-rsa": 2100, "ssh-dss": 450,
}
#: The longest key type name, "sk-ecdsa-sha2-nistp256@openssh.com" (34).
TYPE_NAME_MAX = 40
PROC_TYPE = b"Proc-Type:"
PUB_MAX_BYTES = 16384
CONFIG_MAX_BYTES = 1 << 20
B64 = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
UNUSED_DAYS = 180
GITHUB_PAGE = 100
NOT_KEYS = {"config", "environment", "rc", "known_hosts", "known_hosts.old", "authorized_keys",
            "authorized_keys2"}


def reserved(name: str) -> bool:
    """A name ssh reads as something else: `create` refuses it, and the inventory skips it."""
    return name in NOT_KEYS or name.startswith("known_hosts") or name.endswith("-cert")
USES = {"authentication": "admin:public_key", "signing": "admin:ssh_signing_key"}
LIST_SCOPES = "read:public_key,read:ssh_signing_key"
#: The remote half of `send --to server:…`, sent on stdin to `sh -s -- LINE`, so the remote
#: login shell (csh, fish) only ever parses that one quoted command: it creates ~/.ssh and
#: authorized_keys owner-only when missing, appends LINE only when no line already holds the
#: same key blob, and never removes or rewrites a line. Exit 0 = added, 10 = already there;
#: anything else is a failure, whatever the words on stdout say.
REMOTE_APPEND = r"""umask 077
line=$1
case "$line" in ssh-*|ecdsa-*|sk-*) ;; *) echo invalid-key; exit 65;; esac
d="$HOME/.ssh"; f="$d/authorized_keys"
if [ ! -d "$d" ]; then mkdir -m 700 "$d" || { echo cannot-create-folder; exit 70; }; fi
if [ ! -e "$f" ]; then { : > "$f" && chmod 600 "$f"; } || { echo cannot-create-file; exit 71; }; fi
blob=$(printf '%s\n' "$line" | awk '{print $2}')
if awk -v b="$blob" '{for (i = 1; i <= NF; i++) if ($i == b) found = 1} END {exit !found}' "$f"; then echo already; exit 10; fi
if [ -s "$f" ] && [ -n "$(tail -c 1 "$f")" ]; then printf '\n' >> "$f" || { echo cannot-write; exit 72; }; fi
printf '%s\n' "$line" >> "$f" || { echo cannot-write; exit 72; }
echo added
exit 0
"""
ADDED, ALREADY = 0, 10


class Refused(Exception):
    """A refusal before anything changed: exit 2, the message a person reads."""


# ── small helpers ─────────────────────────────────────────────────────────────

def home() -> pathlib.Path:
    return pathlib.Path(os.path.expanduser("~"))


def ssh_dir() -> pathlib.Path:
    return home() / ".ssh"


def tilde(path: pathlib.Path) -> str:
    """A path spelled from `~`, so a saved inventory or a page carries no account name."""
    try:
        return "~/" + str(pathlib.Path(path).relative_to(home())).replace(os.sep, "/")
    except ValueError:
        return str(path)


def tool(name: str) -> str:
    return os.environ.get("OBSERVATORY_" + name.upper().replace("-", "_"), name)


def available(program: str) -> bool:
    return shutil.which(program) is not None


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shown(value) -> str:
    """Any name or value printed to a terminal: printable characters only, so a file name or a
    config value carrying an escape sequence cannot drive the terminal."""
    return said(str(value if value is not None else ""), 400)


def said(text: str, limit: int = 300) -> str:
    """What a provider or a server said, made safe to show: controls removed, anything shaped
    like a credential replaced, at most `limit` characters."""
    text = " ".join((text or "").split())
    text = "".join(c for c in text if c.isprintable())
    text = credential_shape.redact(text)
    return text[:limit] + ("…" if len(text) > limit else "")


def run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 60)
    return subprocess.run(argv, **kw)


# ── public keys ───────────────────────────────────────────────────────────────

def _fields(blob: bytes) -> list[bytes]:
    out = []
    while blob:
        if len(blob) < 4:
            raise ValueError("truncated")
        n = int.from_bytes(blob[:4], "big")
        if n > len(blob) - 4:
            raise ValueError("truncated")
        out.append(blob[4:4 + n])
        blob = blob[4 + n:]
    return out


def fingerprint(blob: bytes) -> str:
    """OpenSSH's own spelling: SHA-256 of the decoded key blob, base64 without padding."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def blob_bits(ktype: str, blob: bytes) -> int | None:
    try:
        f = _fields(blob)
        if KEY_TYPES.get(ktype, "").startswith("ED25519"):
            return 256
        if ktype.startswith(("ecdsa-sha2-nistp", "sk-ecdsa-sha2-nistp")):
            return int(re.search(r"nistp(\d+)", ktype).group(1))
        if ktype == "ssh-rsa":
            return int.from_bytes(f[2], "big").bit_length()
        if ktype == "ssh-dss":
            return int.from_bytes(f[1], "big").bit_length()
    except (ValueError, IndexError, AttributeError):
        return None
    return None


def parse_public(text: str) -> dict | None:
    """Exactly one public key line, or None. Nothing of a refused text is returned."""
    if BEGIN in text:
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    parts = lines[0].strip().split(None, 2)
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        return None
    try:
        blob = base64.b64decode(parts[1], validate=True)
        if _fields(blob)[0] != parts[0].encode():
            return None
    except (ValueError, IndexError):
        return None
    comment = parts[2].strip() if len(parts) > 2 else ""
    if any(not c.isprintable() for c in comment):
        return None
    return {"type": parts[0], "blob": blob, "comment": comment,
            "line": f"{parts[0]} {parts[1]}" + (f" {comment}" if comment else ""),
            "fingerprint": fingerprint(blob)}


def read_public(path: pathlib.Path) -> dict:
    """The parsed `.pub` at `path`, or Refused. A link is not followed; content is never echoed."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise Refused(f"{tilde(path)} does not exist") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise Refused(f"{tilde(path)} is not a regular file (a link is not followed)")
    if st.st_size > PUB_MAX_BYTES:
        raise Refused(f"{tilde(path)} is not one public key line; nothing was copied or sent")
    fd = osprivacy.open(path, os.O_RDONLY | osprivacy.NOFOLLOW)
    try:
        raw = os.read(fd, PUB_MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        parsed = parse_public(raw.decode("utf-8"))
    except UnicodeDecodeError:
        parsed = None
    if parsed is None:
        raise Refused(f"{tilde(path)} is not one public key line; nothing was copied or sent")
    return parsed


def keygen_describe(pub: pathlib.Path) -> dict | None:
    """`ssh-keygen -l -E sha256 -f PUB`: bits, fingerprint and type, from the public file only."""
    try:
        p = run([tool("ssh-keygen"), "-l", "-E", "sha256", "-f", str(pub)])
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"^(\d+)\s+(SHA256:\S+)\s+.*\(([A-Z0-9-]+)\)\s*$", p.stdout.strip())
    if p.returncode != 0 or not m:
        return None
    return {"bits": int(m.group(1)), "fingerprint": m.group(2), "type": m.group(3)}


# ── private keys: the header and nothing else ────────────────────────────────

def _first_line(fd: int) -> bytes:
    """The first line, one byte at a time, at most FIRST_LINE_MAX. A file whose first byte is
    not `-` cannot be a PEM block, and costs one byte."""
    out = b""
    while len(out) < FIRST_LINE_MAX:
        c = os.read(fd, 1)
        if not c:
            break
        out += c
        if out[:1] != b"-" or c == b"\n":
            break
    return out


def _line_tail(fd: int) -> bytes:
    """The rest of the current line, at most FIRST_LINE_MAX bytes."""
    out = b""
    while len(out) < FIRST_LINE_MAX:
        c = os.read(fd, 1)
        if not c or c == b"\n":
            break
        out += c
    return out


class _Base64Prefix:
    """The decoded start of a PEM body, read from `fd` exactly as far as asked: each read asks
    for no more characters than are still missing, so nothing past the requested prefix is read."""

    def __init__(self, fd: int):
        self.fd, self.chars = fd, bytearray()

    def bytes(self, n: int) -> bytes | None:
        want = -(-n // 3) * 4
        while len(self.chars) < want:
            data = os.read(self.fd, want - len(self.chars))
            if not data:
                return None
            for b in data:
                if b in b"\r\n":
                    continue
                if b not in B64:
                    return None
                self.chars.append(b)
        try:
            return base64.b64decode(bytes(self.chars[:want]))[:n]
        except ValueError:
            return None


def _u32(data: bytes, at: int) -> int:
    return int.from_bytes(data[at:at + 4], "big")


def _openssh_header(fd: int) -> dict:
    """Cipher (so: passphrase or not) and the public key, from the clear start of an OpenSSH key.

    The container is: magic, cipher, kdf, kdf options, the key count, the public key, then the
    private section. Each field is read only once the one before it says how long it is, and
    the read ends with the public key: the private section is never read."""
    unknown = {"format": "openssh", "passphrase": "unknown"}
    r, at = _Base64Prefix(fd), len(OPENSSH_MAGIC)
    data = r.bytes(at + 4)
    if not data or not data.startswith(OPENSSH_MAGIC):
        return unknown
    fields = []
    for _ in range(3):                       # cipher, kdf, kdf options
        n = _u32(data, at)
        if n > FIELD_MAX:
            return unknown
        data = r.bytes(at + 4 + n + 4)
        if data is None:
            return unknown
        fields.append(data[at + 4:at + 4 + n])
        at += 4 + n
    out = {"format": "openssh", "passphrase": "no" if fields[0] == b"none" else "yes"}
    data = r.bytes(at + 12)                  # the key count, the public key's length, its type's length
    if data is None or _u32(data, at) != 1:
        return out
    length, type_length = _u32(data, at + 4), _u32(data, at + 8)
    if not 0 < type_length <= TYPE_NAME_MAX:
        return out
    data = r.bytes(at + 12 + type_length)    # the type name, then the cap for that type
    if data is None:
        return out
    named = data[at + 12:at + 12 + type_length].decode("ascii", "replace")
    if not 0 < length <= PUBLIC_BLOB_MAX.get(named, 0):
        return out
    data = r.bytes(at + 8 + length)
    if data is None:
        return out
    blob = data[at + 8:at + 8 + length]
    try:
        ktype = _fields(blob)[0].decode("ascii")
    except (ValueError, IndexError, UnicodeDecodeError):
        return out
    if ktype in KEY_TYPES:
        out.update(fingerprint=fingerprint(blob), type=KEY_TYPES[ktype], bits=blob_bits(ktype, blob))
    return out


def private_header(path: pathlib.Path) -> dict | None:
    """{format, passphrase[, fingerprint, type, bits]} when `path` is a private key, else None.

    What is read: the BEGIN line (at most FIRST_LINE_MAX bytes); for the OpenSSH format the clear
    header up to the end of the public key (`_openssh_header`); for a PEM key the next ten bytes,
    to see whether they are `Proc-Type:` (an encrypted PEM key says so there). The private section
    is never read, never decoded and never kept. The file's access time is put back, because it is
    the only local trace of when ssh last used the key."""
    st = os.lstat(path)
    fd = osprivacy.open(path, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        first = _first_line(fd).decode("ascii", "replace").strip()
        if not (first.startswith(BEGIN) and first.endswith(PRIVATE_TAIL)):
            return None
        if first == BEGIN + "OPENSSH " + PRIVATE_TAIL:
            return _openssh_header(fd)
        if first == BEGIN + "ENCRYPTED " + PRIVATE_TAIL:
            return {"format": "pkcs8", "passphrase": "yes"}
        if first == BEGIN + PRIVATE_TAIL:
            return {"format": "pkcs8", "passphrase": "no"}
        if os.read(fd, len(PROC_TYPE)) != PROC_TYPE:
            return {"format": "pem", "passphrase": "no"}
        return {"format": "pem", "passphrase": "yes" if b"ENCRYPTED" in _line_tail(fd) else "unknown"}
    finally:
        os.close(fd)
        try:
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass


def holds_private_key(path: pathlib.Path) -> bool:
    """Whether a file's first line is a private-key header (a private key saved as `.pub`).
    Reads that one line and nothing else."""
    try:
        fd = osprivacy.open(path, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        first = _first_line(fd).decode("ascii", "replace").strip()
    finally:
        os.close(fd)
    return first.startswith(BEGIN) and first.endswith(PRIVATE_TAIL)


# ── ~/.ssh/config ─────────────────────────────────────────────────────────────

CONFIG_LINE = re.compile(r"^\s*(\S+?)\s*(?:=\s*|\s+)(.*?)\s*$")


ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: str) -> tuple[pathlib.Path | None, str]:
    """(the path ssh would open, the spelling to show), or (None, spelling) when the path depends
    on the host (`%h`, `%r`, …) or on an unset `${VAR}`. ssh expands `${VAR}` from the environment
    and `%d`/`%u` itself, so they are expanded here too: an unexpanded `${HOME}/keys/x` read as
    a missing file. The spelling keeps the config's own tokens (`%d` and `${HOME}` shown as `~`,
    `%u` and other variables as written), so neither the home path nor the account name reaches
    a saved inventory."""
    value = value.strip().strip('"')
    spelled = value.replace("%d", "~").replace("${HOME}", "~")
    unset = [m.group(1) for m in ENV_REF.finditer(value) if os.environ.get(m.group(1)) is None]
    if unset:
        return None, spelled
    expanded = ENV_REF.sub(lambda m: os.environ[m.group(1)], value)
    expanded = expanded.replace("%%", "\0").replace("%d", str(home())).replace("%u", os.environ.get("USER", ""))
    if "%" in expanded:
        return None, spelled
    expanded = expanded.replace("\0", "%")
    if expanded.startswith("~/"):
        return home() / expanded[2:], spelled
    if expanded.startswith("~"):   # ~user: that account's home, as ssh resolves it
        user, _, rest = expanded[1:].partition("/")
        try:
            import pwd
            return pathlib.Path(pwd.getpwnam(user).pw_dir) / rest, spelled
        except (ImportError, KeyError):
            return None, spelled
    path = pathlib.Path(expanded)
    return (path if path.is_absolute() else home() / path), spelled


def read_config_file(path: pathlib.Path, degraded: list[dict]) -> str | None:
    """A config file's text, or None. Only a regular file of at most CONFIG_MAX_BYTES is read:
    an `Include` of a FIFO hung the listing, and a huge one was read whole."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        degraded.append({"source": "ssh-config", "reason": f"{tilde(path)} unreadable: {type(exc).__name__}"})
        return None
    if not stat.S_ISREG(st.st_mode):
        degraded.append({"source": "ssh-config", "reason": f"{tilde(path)} is not a regular file; not read"})
        return None
    if st.st_size > CONFIG_MAX_BYTES:
        degraded.append({"source": "ssh-config", "reason": f"{tilde(path)} is larger than 1 MiB; not read"})
        return None
    try:
        fd = osprivacy.open(path, os.O_RDONLY | osprivacy.NONBLOCK)
    except OSError as exc:
        degraded.append({"source": "ssh-config", "reason": f"{tilde(path)} unreadable: {type(exc).__name__}"})
        return None
    chunks, size = [], 0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            degraded.append({"source": "ssh-config", "reason": f"{tilde(path)} is not a regular file; not read"})
            return None
        while size <= CONFIG_MAX_BYTES:
            data = os.read(fd, CONFIG_MAX_BYTES + 1 - size)
            if not data:
                break
            chunks.append(data)
            size += len(data)
    finally:
        os.close(fd)
    if size > CONFIG_MAX_BYTES:
        degraded.append({"source": "ssh-config", "reason": f"{tilde(path)} is larger than 1 MiB; not read"})
        return None
    return b"".join(chunks).decode("utf-8", "replace")


def read_config(degraded: list[dict], agents: list[dict] | None = None) -> list[dict]:
    """[{hosts, path, spelled}] for every IdentityFile of ~/.ssh/config and its Includes; every
    IdentityAgent goes to `agents` as {hosts, value}."""
    out: list[dict] = []
    seen: set[str] = set()

    def walk(path: pathlib.Path, depth: int) -> None:
        if depth > 3:
            degraded.append({"source": "ssh-config", "reason": f"{tilde(path)}: Include deeper than 3 levels not read"})
            return
        if str(path) in seen:
            return
        seen.add(str(path))
        text = read_config_file(path, degraded)
        if text is None:
            return
        hosts = ["*"]
        for raw in text.splitlines():
            line = raw.split("#", 1)[0] if not raw.lstrip().startswith("#") else ""
            m = CONFIG_LINE.match(line)
            if not m:
                continue
            key, value = m.group(1).lower(), m.group(2)
            if key == "host":
                hosts = value.split()
            elif key == "match":
                hosts = ["Match " + value]
            elif key == "include":
                for pattern in value.split():
                    target, _ = _expand(pattern)
                    if target is None:
                        continue
                    if not pattern.startswith(("~", "/", "%")):
                        target = ssh_dir() / pattern
                    for found in sorted(glob.glob(str(target))):
                        walk(pathlib.Path(found), depth + 1)
            elif key == "identityfile":
                target, spelled = _expand(value)
                plain = value.strip().strip('"')
                if target is not None and "%u" not in value and "${" not in value.replace("${HOME}", "") \
                        and not (plain.startswith("~") and not plain.startswith("~/")):
                    spelled = tilde(target)
                out.append({"hosts": list(hosts), "path": target, "spelled": spelled})
            elif key == "identityagent" and agents is not None:
                agents.append({"hosts": list(hosts), "value": value.strip().strip('"')})

    walk(ssh_dir() / "config", 0)
    return out


def config_hosts() -> set[str]:
    hosts: set[str] = set()
    text = read_config_file(ssh_dir() / "config", [])
    for raw in (text or "").splitlines():
        m = CONFIG_LINE.match(raw.split("#", 1)[0])
        if m and m.group(1).lower() == "host":
            hosts.update(m.group(2).split())
    return hosts


# ── the agent and GitHub ──────────────────────────────────────────────────────

#: Seconds one agent may take to answer, and all agents together (round 3: a config naming
#: many sockets that never answer made `list` wait 15 s for each).
AGENT_TIMEOUT = 15
AGENT_BUDGET = 30


def agent_fingerprints(degraded: list[dict], sock: str | None = None, label: str = "ssh-agent",
                       deadline: float | None = None) -> list[dict] | None:
    """The keys an agent holds, by fingerprint: the one SSH_AUTH_SOCK names, or the one at `sock`
    (an IdentityAgent of the config: 1Password, Secretive). None when it could not be read.
    `deadline` (time.monotonic) bounds all agent reads of one inventory together."""
    import time as _time
    env = None if sock is None else dict(os.environ, SSH_AUTH_SOCK=sock)
    left = AGENT_TIMEOUT if deadline is None else min(AGENT_TIMEOUT, deadline - _time.monotonic())
    if left <= 0:
        degraded.append({"source": label, "reason": f"not read: the {AGENT_BUDGET} s for reading agents was spent "
                                                    f"on the ones before it"})
        return None
    try:
        p = run([tool("ssh-add"), "-l", "-E", "sha256"], timeout=left, env=env)
    except subprocess.TimeoutExpired:
        degraded.append({"source": label, "reason": f"did not answer within {left:.0f} s",
                         "fix": "check that the agent (or the application providing it) is responsive"})
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        degraded.append({"source": label, "reason": f"ssh-add could not run ({type(exc).__name__})",
                         "fix": "install OpenSSH"})
        return None
    if p.returncode == 1:
        return []
    if p.returncode != 0:
        fix = ("start ssh-agent (eval \"$(ssh-agent -s)\"), or check SSH_AUTH_SOCK" if sock is None else
               "start the application that provides this agent, then list again")
        degraded.append({"source": label, "reason": "not running or not reachable: " + said(p.stderr, 120),
                         "fix": fix})
        return None
    out = []
    for line in p.stdout.splitlines():
        m = re.match(r"^(\d+)\s+(SHA256:\S+)\s+(.*?)\s*\(([A-Z0-9-]+)\)\s*$", line.strip())
        if m:
            out.append({"bits": int(m.group(1)), "fingerprint": m.group(2), "comment": said(m.group(3), 80),
                        "type": m.group(4)})
    return out


def github_keys(uses=("authentication", "signing")) -> tuple[dict, list[dict]]:
    """GitHub's keys of each use, read with `gh api` (JSON; `gh ssh-key list` has none).
    Returns ({use: [rows] or None when unread}, degraded)."""
    degraded: list[dict] = []
    out: dict[str, list[dict] | None] = {}
    endpoints = {"authentication": "user/keys", "signing": "user/ssh_signing_keys"}
    for use in uses:
        try:
            p = run([tool("gh"), "api", "-H", "Accept: application/vnd.github+json",
                     f"{endpoints[use]}?per_page={GITHUB_PAGE}"], timeout=60)
        except FileNotFoundError:
            degraded.append({"source": "github", "reason": "gh (GitHub CLI) is not installed",
                             "fix": "install gh, then gh auth login"})
            return {u: None for u in uses}, degraded
        except (OSError, subprocess.SubprocessError) as exc:
            out[use] = None
            degraded.append({"source": f"github:{use}", "reason": f"gh could not run ({type(exc).__name__})"})
            continue
        rows = None
        if p.returncode == 0:
            try:
                rows = json.loads(p.stdout or "[]")
            except ValueError:
                rows = None
        if not isinstance(rows, list):
            out[use] = None
            degraded.append({"source": f"github:{use}", "reason": "not read: " + (said(p.stderr, 160) or "no answer"),
                             "fix": f"gh auth login, or gh auth refresh -h github.com -s {LIST_SCOPES}"})
            continue
        if len(rows) >= GITHUB_PAGE:
            degraded.append({"source": f"github:{use}", "reason": f"only the first {GITHUB_PAGE} keys were read"})
        keys = []
        for r in rows:
            parsed = parse_public(str((r or {}).get("key") or ""))
            if parsed:
                keys.append({"id": r.get("id"), "title": said(str(r.get("title") or ""), 80), "use": use,
                             "fingerprint": parsed["fingerprint"], "type": KEY_TYPES[parsed["type"]],
                             "added": str(r.get("created_at") or "")[:10]})
        out[use] = keys
    return out, degraded


# ── the inventory (OSS-62) ────────────────────────────────────────────────────

def _probe(path: pathlib.Path) -> tuple[str, str]:
    """(kind, error) for `path` without following a link and without raising: kind is file, dir,
    link, other, missing or error; error names the errno when the look itself failed (EACCES on
    a parent, ELOOP, EIO). `Path.is_file()` raises some of these on Python 3.11 and swallows them
    on 3.14, so neither is used where a refusal must become a line of `unseen`."""
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "missing", ""
    except OSError as exc:
        import errno as _errno
        return "error", _errno.errorcode.get(exc.errno or 0, type(exc).__name__)
    if stat.S_ISLNK(st.st_mode):
        return "link", ""
    if stat.S_ISREG(st.st_mode):
        return "file", ""
    if stat.S_ISDIR(st.st_mode):
        return "dir", ""
    return "other", ""

def _candidates(folder: pathlib.Path) -> tuple[list[pathlib.Path], list[pathlib.Path], list[pathlib.Path]]:
    """(files that may be private keys, .pub files, links), from ~/.ssh and one level below."""
    files, pubs, links = [], [], []
    dirs = [folder]
    try:
        dirs += sorted(p for p in folder.iterdir() if _probe(p)[0] == "dir" and not p.name.startswith("."))
    except OSError:
        pass
    for d in dirs:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.name.startswith(".") or reserved(p.name):
                continue
            kind = _probe(p)[0]
            if kind == "link":
                links.append(p)
            elif p.name.endswith("-cert.pub"):
                continue
            elif p.name.endswith(".pub"):
                if kind == "file":
                    pubs.append(p)
            elif kind in ("file", "error"):
                files.append(p)   # an unreadable entry still gets its row and an unseen line
    return files, pubs, links


def inventory(github: bool = True) -> dict:
    """What the machine holds, read without changing anything but access times put back.

    Permissions are osprivacy's answer, never raw mode bits: Windows reports 0o666 for every
    file and keeps owner-only in an ACL, so a mode check there was a false critical."""
    degraded: list[dict] = []
    folder = ssh_dir()
    doc: dict = {"schema": "ssh-keys/1", "measured_at": now(),
                 "folder": {"path": "~/.ssh", "exists": False, "mode": None, "private": None, "link": False},
                 "misplaced": []}
    unseen: list[str] = []
    try:
        st = os.lstat(folder)
        doc["folder"].update(exists=True, mode=osprivacy.describe(folder), private=osprivacy.private(folder),
                             link=stat.S_ISLNK(st.st_mode))
    except FileNotFoundError:
        pass
    except OSError as exc:
        unseen.append(f"~/.ssh (could not be read: {type(exc).__name__})")
    keygen_ok = available(tool("ssh-keygen"))
    if not keygen_ok:
        degraded.append({"source": "ssh-keygen", "reason": "not found; fingerprints computed by Observatory "
                                                           "from the public files", "fix": "install OpenSSH"})
    files, pubs, links = _candidates(folder) if doc["folder"]["exists"] else ([], [], [])
    agent_config: list[dict] = []
    config = read_config(degraded, agent_config)
    for entry in config:   # every key the config names is a key, at any depth, in ~/.ssh or not
        p = entry["path"]
        if p is None:
            continue
        kind, err = _probe(p)
        entry["kind"] = kind
        if kind == "error":
            unseen.append(f"IdentityFile {entry['spelled']} (could not be looked at: {err})")
        elif kind == "file" and p not in files:
            files.append(p)
            pub = pathlib.Path(str(p) + ".pub")
            if _probe(pub)[0] == "file" and pub not in pubs:
                pubs.append(pub)
    rows: dict[str, dict] = {}

    def row(base: pathlib.Path) -> dict:
        key = str(base)
        if key not in rows:
            try:
                name = str(base.relative_to(folder)).replace(os.sep, "/")
            except ValueError:
                name = tilde(base)
            rows[key] = {"name": name, "path": tilde(base), "private": False, "public": False, "link": False,
                         "format": None, "passphrase": "unknown", "type": None, "bits": None,
                         "fingerprint": None, "comment": "", "private_mode": None, "private_ok": None,
                         "public_mode": None, "last_access": None, "hosts": [], "in_agent": None,
                         "github": None, "mismatch": False}
        return rows[key]

    for f in files:
        try:
            st = os.lstat(f)
            header = private_header(f)
            if header is not None:
                mode_text, private_ok = osprivacy.describe(f), osprivacy.private(f)
        except OSError as exc:
            if f.name.startswith("id_") or _probe(pathlib.Path(str(f) + ".pub"))[0] != "missing" \
                    or any(e["path"] == f for e in config):
                row(f).update(private=True, hint=f"unreadable: {type(exc).__name__}")
                unseen.append(f"{tilde(f)} (could not be read: {type(exc).__name__})")
            continue
        if header is None:
            continue
        r = row(f)
        r.update(private=True, private_mode=mode_text, private_ok=private_ok,
                 last_access=stamp(st.st_atime), **header)
    for link in links:
        base = pathlib.Path(str(link)[:-4]) if link.name.endswith(".pub") else link
        named = any(e["path"] == link for e in config)
        if link.name.startswith("id_") or named or _probe(pathlib.Path(str(link) + ".pub"))[0] != "missing" \
                or link.name.endswith(".pub"):
            row(base).update(link=True)
    for pub in pubs:
        base = pathlib.Path(str(pub)[:-4])
        if holds_private_key(pub):
            doc["misplaced"].append(tilde(pub))
            continue
        r = row(base)
        try:
            parsed = read_public(pub)
        except (Refused, OSError):
            r["hint"] = f"{tilde(pub)} is not one public key line"
            continue
        from_header = r["fingerprint"]
        r.update(public=True, public_mode=osprivacy.describe(pub), comment=said(parsed["comment"], 120))
        described = (keygen_describe(pub) if keygen_ok else None) or {
            "type": KEY_TYPES[parsed["type"]], "bits": blob_bits(parsed["type"], parsed["blob"]),
            "fingerprint": parsed["fingerprint"]}
        if from_header and from_header != described["fingerprint"]:
            # The private key authenticates; a .pub of another key would be copied and sent.
            r.update(mismatch=True, hint=f"{tilde(pub)} is not the public half of {tilde(base)}; "
                                         f"`ssh-keygen -y -f {tilde(base)}` prints the right one")
            continue
        r.update(described)
    for r in rows.values():
        if r["private"] and not r["public"] and "hint" not in r:
            r["hint"] = (f"no public half: `ssh-keygen -y -f {r['path']} > {r['path']}.pub` recreates it "
                         f"(it asks for the passphrase)")
    for entry in config:   # a key the config names through a link: a row, never followed
        if entry.get("kind") == "link":
            row(entry["path"]).update(link=True)
    by_path = {str(pathlib.Path(k)): r for k, r in rows.items()}
    identity_files = []
    for entry in config:
        p = entry["path"]
        kind = entry.get("kind")
        exists = None if p is None or kind == "error" else kind != "missing"
        identity_files.append({"host": " ".join(entry["hosts"]), "path": entry["spelled"], "exists": exists})
        r = by_path.get(str(p)) if p is not None else None
        if r is not None:
            for h in entry["hosts"]:
                if h != "*" and h not in r["hosts"]:
                    r["hosts"].append(h)
    deadline = __import__("time").monotonic() + AGENT_BUDGET
    agent = agent_fingerprints(degraded, deadline=deadline)
    # WHAT THIS INVENTORY COULD NOT SEE. A GitHub key is called foreign (and a delete offered)
    # only when every place a key of this machine can live was read: the agent, every other
    # agent the config names, every IdentityFile, every linked key. Round 2 found five ways a
    # key in use stayed invisible and was offered for deletion.
    if agent is None:
        unseen.append("the ssh-agent")
    other_agents: list[dict] = []
    seen_socks: set[str] = set()
    for a in agent_config:
        value = a["value"]
        if value.lower() == "none" or value == "SSH_AUTH_SOCK":
            continue
        where = " ".join(a["hosts"])
        if value.startswith("$") and not value.startswith("${"):
            path = os.environ.get(value[1:])
            spelled = value
        else:
            target, spelled = _expand(value)
            path = str(target) if target is not None else None
            if target is not None:
                spelled = tilde(target)
        label = f"agent {spelled} (IdentityAgent, Host {where})"
        if path is None:
            degraded.append({"source": "identity-agent", "reason": f"{label}: its path depends on the host or an "
                                                                    f"unset variable; not read"})
            unseen.append(label)
            continue
        if path == os.environ.get("SSH_AUTH_SOCK") or path in seen_socks:
            continue
        seen_socks.add(path)
        held = agent_fingerprints(degraded, sock=path, label=f"identity-agent {spelled}", deadline=deadline)
        if held is None:
            unseen.append(label)
        else:
            other_agents += [dict(k, agent=spelled) for k in held]
    agent_keys = (agent or []) + other_agents
    disk = {r["fingerprint"] for r in rows.values() if r["fingerprint"]}
    if agent is not None or other_agents:
        loaded = {a["fingerprint"] for a in agent_keys}
        for r in rows.values():
            r["in_agent"] = bool(r["fingerprint"]) and r["fingerprint"] in loaded
    doc["agent_only"] = [a for a in agent_keys if a["fingerprint"] not in disk]
    for entry in config:
        p = entry["path"]
        if p is None:
            unseen.append(f"IdentityFile {entry['spelled']} (Host {' '.join(entry['hosts'])}): the path "
                          f"depends on the host or an unset variable")
        elif entry.get("kind") == "file" and str(p) not in by_path:
            unseen.append(f"IdentityFile {entry['spelled']}: not readable as a key")
    for r in rows.values():
        if not r["fingerprint"] and (r["private"] or r["link"]):
            unseen.append(f"{r['path']}" + (" (a link, not followed)" if r["link"] else " (fingerprint unknown)"))
    doc["unseen"] = sorted(set(unseen))
    doc["github"] = None
    if github:
        gh, gh_degraded = github_keys()
        degraded += gh_degraded
        held = disk | {a["fingerprint"] for a in agent_keys}
        if any(v is not None for v in gh.values()):
            keys = [dict(k, on_machine=k["fingerprint"] in held) for v in gh.values() if v for k in v]
            doc["github"] = {"read": {u: v is not None for u, v in gh.items()}, "keys": keys}
            if all(v is not None for v in gh.values()):
                for r in rows.values():
                    r["github"] = [u for u, v in gh.items() if any(k["fingerprint"] == r["fingerprint"] for k in v)] \
                        if r["fingerprint"] else []
    else:
        degraded.append({"source": "github", "reason": "not read (--no-github)"})
    doc["keys"] = sorted(rows.values(), key=lambda r: r["name"])
    doc["identity_files"] = identity_files
    doc["degraded"] = degraded
    return doc


def saved_path() -> pathlib.Path:
    scratch = os.environ.get("OBSERVATORY_SCRATCH")
    if scratch:
        return pathlib.Path(scratch) / "ssh-keys.json"
    import paths
    return paths.SCRATCH / "ssh-keys.json"


def save(doc: dict) -> str:
    """Write the inventory for the board; "" or the reason it was not saved."""
    try:
        import atomic
        atomic.write_json(saved_path(), doc)
        return ""
    except Exception as exc:   # the listing stands without the saved copy; say why
        return f"{type(exc).__name__}"


def load_saved() -> dict | None:
    try:
        doc = json.loads(saved_path().read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) and doc.get("schema") == "ssh-keys/1" else None
    except (OSError, ValueError, ImportError):
        return None


# ── receipts ──────────────────────────────────────────────────────────────────

def receipt(event: str, fp: str, **detail) -> bool:
    """One row in the key movements journal (vault.journal): names, places, when, who. Never a key."""
    try:
        import vault
        vault.journal(event, f"ssh/{fp}", tool="ssh_keys.py", **detail)
        return True
    except (Exception, SystemExit) as exc:
        print(f"receipt not written ({type(exc).__name__}); the action above did happen", file=sys.stderr)
        return False


# ── commands ──────────────────────────────────────────────────────────────────

def _key_paths(name: str) -> tuple[pathlib.Path, pathlib.Path]:
    if not KEY_REF.fullmatch(name or "") or name.endswith(".pub"):
        raise Refused(f"{credential_shape.echo(name)} is not a key name in ~/.ssh "
                      f"(letters, digits, . _ -, at most one folder)")
    base = ssh_dir() / name
    return base, pathlib.Path(str(base) + ".pub")


def public_of(name: str) -> tuple[pathlib.Path, pathlib.Path, dict]:
    base, pub = _key_paths(name)
    if not os.path.lexists(pub):
        hint = (f"; `ssh-keygen -y -f {tilde(base)} > {tilde(pub)}` recreates it (it asks for the passphrase)"
                if base.exists() else "")
        raise Refused(f"{tilde(pub)} does not exist{hint}")
    parsed = read_public(pub)
    if os.path.lexists(base) and not os.path.islink(base):
        try:
            own = (private_header(base) or {}).get("fingerprint")
        except OSError:
            own = None
        if own and own != parsed["fingerprint"]:
            raise Refused(f"{tilde(pub)} does not belong to {tilde(base)} ({parsed['fingerprint']} is not "
                          f"{own}); nothing was copied or sent. `ssh-keygen -y -f {tilde(base)}` prints the right one")
    return base, pub, parsed


def cmd_list(args) -> int:
    doc = inventory(github=not args.no_github)
    not_saved = save(doc)
    if args.json:
        print(json.dumps(doc, indent=1, ensure_ascii=False))
    else:
        print_listing(doc)
    if not_saved:
        print(f"inventory not saved for the findings board ({not_saved}); `{CMD.rsplit(' ', 1)[0]} init` "
              f"creates the workspace", file=sys.stderr)
    return 0


def print_listing(doc: dict) -> None:
    """The terminal listing. Every value from a file name or the config goes through `shown`."""
    keys = doc["keys"]
    local_gaps = list(doc.get("unseen") or []) + [
        f"{d['source']}: {d['reason']}" for d in doc["degraded"] if not str(d.get("source", "")).startswith("github")
        and d["reason"] not in " ".join(doc.get("unseen") or [])]
    if not keys and not doc["agent_only"] and local_gaps:
        print("No SSH keys found where Observatory could look. Not seen: "
              + "; ".join(shown(u) for u in local_gaps))
    elif not keys and not doc["agent_only"]:
        print("No SSH keys yet. Create one: " + f"{CMD} create NAME")
    else:
        print(f"SSH keys in ~/.ssh: {len(keys)}, measured {doc['measured_at']}\n")
        for k in keys:
            gh = ("not read" if k["github"] is None else
                  ", ".join("sign-in" if u == "authentication" else u for u in k["github"]) or "no")
            agent = {None: "not read", True: "loaded", False: "no"}[k["in_agent"]]
            what = f"{k['type'] or '?'} {k['bits'] or ''}".strip()
            print(f"  {shown(k['name'])}  {what}  {k['fingerprint'] or 'fingerprint unknown'}")
            print(f"      passphrase: {k['passphrase']}  agent: {agent}  GitHub: {gh}"
                  f"  hosts: {shown(', '.join(k['hosts'])) or '-'}")
            print(f"      {shown(k['path'])}  {shown(k['private_mode'] or '-')} / {shown(k['public_mode'] or '-')}"
                  + (f"  comment: {shown(k['comment'])}" if k["comment"] else "")
                  + ("  (a link, not followed)" if k["link"] else ""))
            if k.get("hint"):
                print(f"      {shown(k['hint'])}")
        for a in doc["agent_only"]:
            print(f"  (agent only) {shown(a['type'])} {a['bits']}  {a['fingerprint']}  {shown(a['comment'])}")
    for path in doc.get("misplaced") or []:
        print(f"  {shown(path)} holds a PRIVATE key under a public name: move it (ssh-keys findings)")
    for f in doc["identity_files"]:
        if f["exists"] is False:
            print(f"  Host {shown(f['host'])}: IdentityFile {shown(f['path'])} does not exist")
    if doc.get("unseen") and (keys or doc["agent_only"]):
        print("\nKeys this machine may hold that were not seen (no GitHub key is called foreign until they are):")
        for u in doc["unseen"]:
            print(f"  - {shown(u)}")
    if doc["degraded"]:
        print("\nNot read:")
        for d in doc["degraded"]:
            print(f"  - {shown(d['source'])}: {shown(d['reason'])}" + (f"; fix: {shown(d['fix'])}" if d.get("fix") else ""))
    import ssh_key_findings
    n = len(ssh_key_findings.findings(doc))
    print(f"\nFindings: {n} ({CMD} findings)")
    print(f"Next: {CMD} copy NAME · {CMD} send NAME --to github · {CMD} create NAME")


def cmd_show(args, clipboard: bool = False) -> int:
    base, pub, parsed = public_of(args.name)
    print(parsed["line"])
    print(f"{parsed['fingerprint']}  {tilde(pub)}")
    if not clipboard:
        return 0
    program = clipboard_tool()
    if program is None:
        print("the clipboard was not set: no clipboard tool (pbcopy, wl-copy, xclip); copy the line above",
              file=sys.stderr)
        return 3
    try:
        p = run(program, input=parsed["line"], timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"the clipboard was not set ({type(exc).__name__}); copy the line above", file=sys.stderr)
        return 3
    if p.returncode != 0:
        print("the clipboard was not set; copy the line above", file=sys.stderr)
        return 3
    print(f"Copied. Paste it where the key is asked for, or let Observatory send it: "
          f"{CMD} send {args.name} --to github")
    return 0


def clipboard_tool() -> list[str] | None:
    chosen = os.environ.get("OBSERVATORY_CLIPBOARD")
    if chosen is not None:
        return None if chosen in ("", "none") else [chosen]
    if sys.platform == "darwin" and available("pbcopy"):
        return ["pbcopy"]
    if sys.platform == "win32" and available("clip"):
        return ["clip"]
    if available("wl-copy"):
        return ["wl-copy"]
    if available("xclip"):
        return ["xclip", "-selection", "clipboard"]
    return None


def cmd_create(args) -> int:
    name = args.name or ""
    if not NAME.fullmatch(name) or name.endswith(".pub"):
        raise Refused(f"{credential_shape.echo(name)} is not a key name: letters, digits, . _ - "
                      f"(at most 64, not starting with a dot or ending in .pub)")
    if reserved(name):
        raise Refused(f"{name} is a name ssh reads as something else (config, authorized keys, known hosts or a "
                      f"certificate); choose another name")
    comment = args.comment if args.comment is not None else name
    if len(comment) > 128 or any(not c.isprintable() for c in comment):
        raise Refused("the comment must be one printable line of at most 128 characters")
    try:
        credential_shape.refuse("the comment", comment)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    folder = ssh_dir()
    if folder.is_symlink():
        raise Refused("~/.ssh is a link; Observatory does not write through a link")
    target, pub = folder / name, folder / f"{name}.pub"
    for p in (target, pub):
        if os.path.lexists(p):
            raise Refused(f"{tilde(p)} already exists; a key is never overwritten. Choose another name, "
                          f"or rotate (OSS-67)")
    block = host_block(args, name)
    passphrase = not args.no_passphrase
    if passphrase and not sys.stdin.isatty():
        raise Refused("a passphrase is typed by a person, into ssh-keygen's own prompt; run this in a "
                      "terminal, or pass --no-passphrase for an unattended key (the findings will list it)")
    keygen = tool("ssh-keygen")
    if not available(keygen):
        raise Refused("ssh-keygen is not installed; install OpenSSH")
    if not folder.exists():
        folder.mkdir(mode=0o700)
        osprivacy.make_private(folder)
    linked: list[pathlib.Path] = []
    # A private folder inside ~/.ssh (same file system, so os.link works), removed on every path.
    with tempfile.TemporaryDirectory(prefix=".observatory-keygen-", dir=folder) as scratch:
        temp = pathlib.Path(scratch)
        argv = [keygen, "-q", "-t", "ed25519", "-a", "100", "-C", comment, "-f", str(temp / name)]
        if not passphrase:
            argv += ["-N", ""]
        if passphrase:
            p = subprocess.run(argv, stdout=subprocess.DEVNULL)
            err = ""
        else:
            p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True, timeout=120)
            err = p.stderr
        if p.returncode != 0 or not (temp / name).is_file() or not (temp / f"{name}.pub").is_file():
            print(f"ssh-keygen did not make the key: {said(err) or f'exit {p.returncode}'}; nothing was written",
                  file=sys.stderr)
            return 1
        for src, dst in ((temp / name, target), (temp / f"{name}.pub", pub)):
            try:
                os.link(src, dst)   # fails if dst exists: no-replace, unlike a rename
            except FileExistsError:
                for mine in linked:
                    if os.path.samefile(mine, temp / mine.name):
                        mine.unlink()
                raise Refused(f"{tilde(dst)} appeared while the key was being made; it was left as it is and "
                              f"nothing of this run remains") from None
            linked.append(dst)
        osprivacy.make_private(target)
        os.chmod(pub, 0o644)
    parsed = read_public(pub)
    print(f"Created {tilde(target)} (ed25519)")
    print(parsed["line"])
    print(parsed["fingerprint"])
    notes = []
    if passphrase and not args.no_keychain:
        if sys.platform == "darwin":
            k = subprocess.run([tool("ssh-add"), "--apple-use-keychain", str(target)])
            if k.returncode != 0:
                notes.append(f"the passphrase is not stored in the Keychain; retry: ssh-add --apple-use-keychain {tilde(target)}")
            else:
                print("The passphrase is in your login Keychain, and the key is loaded into the running agent.")
        else:
            notes.append(f"the passphrase is not stored in a keychain on this system; ssh asks for it on use, "
                         f"or `ssh-add {tilde(target)}` loads it into the agent")
    if block:
        append_config(block)
        print(f"Added Host {args.host} to ~/.ssh/config")
    for n in notes:
        print(n)
    ok = receipt("ssh.created", parsed["fingerprint"], how=f"created ed25519 {tilde(target)}",
                 to=f"~/.ssh/{name}")
    print(f"Next: {CMD} copy {name}  ·  {CMD} send {name} --to github  ·  {CMD} send {name} --to server:HOST")
    return 0 if ok else 4


def host_block(args, name: str) -> str:
    if not args.host:
        if args.hostname or args.user or args.port:
            raise Refused("--hostname, --user and --port go with --host ALIAS")
        return ""
    if not NAME.fullmatch(args.host) or not args.hostname or not CONFIG_WORD.fullmatch(args.hostname):
        raise Refused("--host takes an alias (letters, digits, . _ -) and needs --hostname HOST")
    if args.user and not USER_WORD.fullmatch(args.user):
        raise Refused("--user takes one user name")
    if args.port is not None and not 0 < args.port < 65536:
        raise Refused("--port takes 1-65535")
    config = ssh_dir() / "config"
    if config.is_symlink():
        raise Refused("~/.ssh/config is a link; add the Host block by hand")
    if args.host in config_hosts():
        raise Refused(f"Host {args.host} is already in ~/.ssh/config; nothing was created")
    lines = [f"Host {args.host}", f"  HostName {args.hostname}"]
    if args.user:
        lines.append(f"  User {args.user}")
    if args.port:
        lines.append(f"  Port {args.port}")
    lines += [f"  IdentityFile ~/.ssh/{name}", "  IdentitiesOnly yes", "  AddKeysToAgent yes"]
    if sys.platform == "darwin":
        lines.append("  UseKeychain yes")   # other systems' ssh refuses this option
    return "\n".join(lines) + "\n"


def append_config(block: str) -> None:
    config = ssh_dir() / "config"
    fd = osprivacy.open(config, os.O_WRONLY | os.O_APPEND | os.O_CREAT | osprivacy.NOFOLLOW, 0o600)
    try:
        size = os.fstat(fd).st_size
        lead = ""
        if size:
            with open(config, "rb") as f:
                f.seek(size - 1)
                lead = "\n" if f.read(1) == b"\n" else "\n\n"
        os.write(fd, (lead + block).encode())
    finally:
        os.close(fd)


def cmd_send(args) -> int:
    target = args.to or ""
    if target == "github":
        return send_github(args)
    if target.startswith("server:"):
        return send_server(args, target[len("server:"):])
    _key_paths(args.name)
    raise Refused(f"no Observatory door registers SSH keys at {credential_shape.echo(target)} yet (OBS-112); "
                  f"copy the key with `{CMD} copy {args.name}` and paste it in the provider's console")


def send_github(args) -> int:
    base, pub, parsed = public_of(args.name)
    uses = ["authentication", "signing"] if args.as_ == "both" else [args.as_]
    gh = tool("gh")
    if not available(gh):
        raise Refused("gh (GitHub CLI) is not installed; install it, then `gh auth login`")
    title = args.title if args.title is not None else (parsed["comment"] or args.name)
    if len(title) > 100 or any(not c.isprintable() for c in title):
        raise Refused("--title must be one printable line of at most 100 characters")
    held, degraded = github_keys(uses)
    for use in uses:
        if held.get(use) is None:
            reasons = "; ".join(d["reason"] for d in degraded)
            raise Refused(f"GitHub's {use} keys could not be read ({reasons}); nothing was sent. "
                          f"Grant the scope: gh auth refresh -h github.com -s {USES[use]}")
    fp, worst = parsed["fingerprint"], 0
    for use in uses:
        to = f"github:{use}"
        if any(k["fingerprint"] == fp for k in held[use]):
            print(f"{args.name}: already registered on GitHub for {use}")
            worst = max(worst, 0 if receipt("ssh.already_registered", fp, to=to, how="found by fingerprint") else 4)
            continue
        try:
            p = run([gh, "ssh-key", "add", "-", "--type", use, f"--title={title}"],
                    input=parsed["line"] + "\n", timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            p = subprocess.CompletedProcess([], 1, "", type(exc).__name__)
        if p.returncode != 0:
            reason = said(p.stderr) or f"exit {p.returncode}"
            print(f"GitHub refused {args.name} for {use}: {reason}", file=sys.stderr)
            receipt("ssh.register_failed", fp, to=to, how=f"gh ssh-key add: {reason}")
            worst = 1
            continue
        print(f"{args.name}: registered on GitHub for {use} as {title!r}")
        worst = max(worst, 0 if receipt("ssh.registered", fp, to=to, how="gh ssh-key add") else 4)
    if "authentication" in uses and worst != 1:
        print("Test it: ssh -T git@github.com")
    return worst


def send_server(args, alias: str) -> int:
    if not HOST_ARG.fullmatch(alias):
        raise Refused(f"{credential_shape.echo(alias)} is not a host: a Host of ~/.ssh/config or [user@]host")
    base, pub, parsed = public_of(args.name)
    ssh = tool("ssh")
    if not available(ssh):
        raise Refused("ssh is not installed; install OpenSSH")
    batch = [] if args.interactive else ["-o", "BatchMode=yes"]
    # The script goes on stdin to `sh -s`; the remote login shell parses only this one command.
    argv = [ssh, "-o", "ConnectTimeout=15", *batch, "--", alias, "sh -s -- " + shlex.quote(parsed["line"])]
    fp, to = parsed["fingerprint"], f"server:{alias}"
    try:
        p = run(argv, input=REMOTE_APPEND, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        p = subprocess.CompletedProcess(argv, 255, "", type(exc).__name__)
    if p.returncode not in (ADDED, ALREADY):
        words = (p.stdout.strip().splitlines() or [""])[-1].strip()
        reason = said(p.stderr) or said(words) or f"exit {p.returncode}"
        hint = ""
        if "password" in p.stderr.lower() and not args.interactive:
            hint = "; the server asks for a password: run with --interactive in a terminal"
        print(f"{alias} did not take the key: {reason}{hint}", file=sys.stderr)
        receipt("ssh.register_failed", fp, to=to, how=f"authorized_keys append: {reason}")
        return 1
    answer = "added" if p.returncode == ADDED else "already"
    if answer == "added":
        print(f"{args.name}: added to {alias}:~/.ssh/authorized_keys")
        worst = 0 if receipt("ssh.registered", fp, to=to, how="authorized_keys append") else 4
    else:
        print(f"{args.name}: already in {alias}:~/.ssh/authorized_keys")
        worst = 0 if receipt("ssh.already_registered", fp, to=to, how="found by key blob") else 4
    if not args.verify:
        print(f"Test it: {CMD} send {args.name} --to server:{alias} --verify")
        return worst
    ok, why = verify_login(ssh, alias, base, fp, args)
    if not ok:
        print(f"the key was {answer}, but it is not verified: {why}", file=sys.stderr)
        receipt("ssh.verify_failed", fp, to=to, how=said(why))
        return 1
    if getattr(args, "_ssh_key_unchecked", False):
        print(f"Verified: {alias} accepted {fp} for {args.name}; the private file was not checked against "
              f"{tilde(pub)} (its format stores no public key in clear)")
    else:
        print(f"Verified: {alias} accepted {args.name} alone (the server named {fp})")
    return max(worst, 0 if receipt("ssh.verified", fp, to=to, how="ssh -F none -i KEY, server named the key") else 4)


def resolve_host(ssh: str, alias: str) -> dict:
    """`ssh -G` for one host: what the user's config makes of it. Raises Refused."""
    try:
        p = run([ssh, "-G", "--", alias], stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refused(f"ssh -G {alias} could not run ({type(exc).__name__})") from None
    if p.returncode != 0:
        raise Refused(f"ssh -G {alias} failed: {said(p.stderr) or p.returncode}")
    out: dict[str, str] = {}
    for line in p.stdout.splitlines():
        key, _, value = line.strip().partition(" ")
        if key and key.lower() not in out:
            out[key.lower()] = value.strip()
    if not HOST_ARG.fullmatch(out.get("hostname", "")) or not out.get("port", "").isdigit() \
            or not USER_WORD.fullmatch(out.get("user", "")):
        raise Refused(f"ssh -G {alias} gave no usable host, port and user")
    return out


def verify_login(ssh: str, alias: str, base: pathlib.Path, fp: str, args) -> tuple[bool, str]:
    """Log in with the new key ALONE, and require the server to name it.

    A plain `ssh -o IdentitiesOnly=yes -i NEW ALIAS` is not alone: IdentityFile lines of the
    config accumulate with -i, so a Host block naming the old key can log in with it while the
    answer says the new one worked (round-1 finding F1). So the host is resolved once with
    `ssh -G`, and the login runs with `-F none` (no config at all), only `-i NEW`, publickey
    only, and `-v`; it passes only when ssh's own log says the server accepted NEW's
    fingerprint. The agent is not offered (`IdentityAgent=none`) unless the key has a passphrase
    and no person is at the terminal: then the agent may hold it unlocked, and `IdentitiesOnly`
    with `-F none` lets it offer that one key and no other."""
    if not base.is_file():
        return False, f"there is no private file {tilde(base)} to log in with"
    try:
        host = resolve_host(ssh, alias)
    except Refused as exc:
        return False, str(exc)
    if host.get("proxycommand", "none").lower() != "none":
        return False, (f"{alias} is reached through a ProxyCommand, which a config-free login cannot "
                       f"reproduce; check by hand: ssh -v -o IdentitiesOnly=yes -i {tilde(base)} {alias} true, "
                       f"and look for 'Server accepts key' with {fp}")
    jump = []
    if host.get("proxyjump", "none").lower() != "none":
        hops = []
        for hop in host["proxyjump"].split(","):
            m = re.fullmatch(r"(?:([^@:\s]+)@)?([A-Za-z0-9_.-]+)(?::(\d+))?", hop.strip())
            if not m:
                return False, f"the jump host {said(hop)} could not be read; verify by hand"
            try:
                h = resolve_host(ssh, m.group(2))
            except Refused as exc:
                return False, str(exc)
            hops.append(f"{m.group(1) or h['user']}@{h['hostname']}:{m.group(3) or h['port']}")
        jump = ["-J", ",".join(hops)]
    try:
        header = private_header(base) or {}
    except OSError:
        header = {}
    passphrase, own = header.get("passphrase", "unknown"), header.get("fingerprint")
    # THE AGENT IS OFFERED ONLY FOR A KEY PROVEN TO BE THIS ONE (round 3): a PKCS#8 key stores no
    # public key in clear, so a `.pub` holding another key B passed the pair check, and the agent
    # holding B signed for it. Without that proof the agent stays out; a key with a passphrase
    # then needs a person (--interactive) to type it.
    proven = own is not None and own == fp
    agent = (os.environ.get("SSH_AUTH_SOCK") or "none") if proven and passphrase != "no" and not args.interactive \
        else "none"
    args._ssh_key_unchecked = not proven
    known = ["-o", f"UserKnownHostsFile={host.get('userknownhostsfile', '~/.ssh/known_hosts')}",
             "-o", f"StrictHostKeyChecking={host.get('stricthostkeychecking', 'ask')}"]
    if host.get("hostkeyalias"):
        known += ["-o", f"HostKeyAlias={host['hostkeyalias']}"]
    check = [ssh, "-v", "-F", "none", *([] if args.interactive else ["-o", "BatchMode=yes"]),
             "-o", "ConnectTimeout=15", "-o", "IdentitiesOnly=yes", "-o", "PreferredAuthentications=publickey",
             "-o", f"IdentityAgent={agent}", *known, "-p", host["port"], "-l", host["user"], *jump,
             "-i", str(base), "--", host["hostname"], "true"]
    try:
        v = run(check, stdin=None if args.interactive else subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"ssh could not run ({type(exc).__name__})"
    accepted = re.findall(r"Server accepts key: .*?(SHA256:[A-Za-z0-9+/]+)", v.stderr or "")
    plain = "\n".join(line for line in (v.stderr or "").splitlines() if not line.startswith("debug"))
    if v.returncode != 0:
        hint = ""
        if agent == "none" and passphrase != "no" and not args.interactive:
            hint = ("; the key has a passphrase and the agent was not used (its private file could not be "
                    "proven to be this key): run with --interactive and type it")
        return False, f"logging in with it alone failed: {said(plain) or f'exit {v.returncode}'}{hint}"
    if fp not in accepted:
        named = ", ".join(sorted(set(accepted))) or "no key"
        return False, f"the server accepted {named}, not {fp}"
    return True, ""


def cmd_findings(args) -> int:
    import ssh_key_findings
    doc = load_saved()
    if doc is None:
        if args.json:
            print(json.dumps({"measured_at": None, "findings": [],
                              "degraded": [{"source": "inventory", "reason": f"no inventory yet: {CMD} list"}]}))
        else:
            print(f"No inventory yet: run `{CMD} list` first.")
        return 0
    rows = ssh_key_findings.findings(doc)
    if args.json:
        print(json.dumps({"measured_at": doc.get("measured_at"), "findings": rows}, indent=1, ensure_ascii=False))
        return 0
    print(f"SSH key findings, from the inventory of {doc.get('measured_at')}: {len(rows)}")
    for r in rows:
        print(f"  [{r['severity']}] {shown(r['title'])}" + (f"\n      {shown(r['action'])}" if r.get("action") else ""))
    return 0


# ── the agent's view (observatory_ssh_keys) ───────────────────────────────────

def _commands(name: str = "NAME") -> dict:
    return {"show": f"{CMD} show {name}", "copy": f"{CMD} copy {name}",
            "sendGithub": f"{CMD} send {name} --to github", "sendServer": f"{CMD} send {name} --to server:HOST --verify",
            "create": f"{CMD} create NAME", "list": f"{CMD} list"}


def agent_view(name: str | None = None, github: bool = False) -> dict:
    """The MCP answer: read-only, live for local sources; GitHub from the saved inventory unless
    `github` asks gh now. Never a private byte; one key's public line only when `name` asks."""
    if name is not None and (not KEY_REF.fullmatch(name) or credential_shape.shaped(name)):
        return {"error": "invalid key name", "name": credential_shape.shown(name),
                "hint": "a file name in ~/.ssh, as `list` shows it", "degraded": []}
    doc = inventory(github=github)
    if not github:
        doc["degraded"] = [d for d in doc["degraded"] if d["source"] != "github"]
        saved = load_saved()
        if saved and saved.get("github"):
            doc["github"] = dict(saved["github"], measuredAt=saved.get("measured_at"))
            by_fp = {k.get("fingerprint"): k.get("github") for k in saved.get("keys") or []}
            for k in doc["keys"]:
                k["github"] = by_fp.get(k["fingerprint"]) if k["fingerprint"] in by_fp else None
        else:
            doc["degraded"].append({"source": "github", "reason": "GitHub not read: pass github: true, or run "
                                                                  f"`{CMD} list` to save it"})
    if name is not None:
        row = next((k for k in doc["keys"] if k["name"] == name), None)
        if row is None:
            return {"error": "unknown key", "name": name, "hint": f"`{CMD} list` names the keys",
                    "degraded": doc["degraded"]}
        try:
            line = public_of(name)[2]["line"]
        except (Refused, OSError):
            line = None
        return {"measuredAt": doc["measured_at"], "key": row, "public": line, "commands": _commands(name),
                "degraded": doc["degraded"]}
    import ssh_key_findings
    rows = ssh_key_findings.findings(doc)
    return {"measuredAt": doc["measured_at"], "folder": doc["folder"], "keys": doc["keys"],
            "agentOnly": doc["agent_only"],
            "identityFiles": [f for f in doc["identity_files"] if f["exists"] is False],
            "github": doc["github"],
            "findings": [{k: r[k] for k in ("type", "severity", "subject", "title", "detail", "action") if k in r}
                         for r in rows],
            "commands": _commands(), "degraded": doc["degraded"]}


# ── the command line ──────────────────────────────────────────────────────────

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=CMD, description="SSH keys on this machine: list, create, copy the "
                                "public line, send it. The private key is never shown or moved.")
    sub = p.add_subparsers(dest="command")   # no command: list
    ls = sub.add_parser("list", help="every key, where it is used and registered; saves the inventory")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--no-github", action="store_true", help="do not ask GitHub")
    for verb, what in (("show", "print one key's public line and fingerprint"),
                       ("copy", "put one key's public line on the clipboard")):
        s = sub.add_parser(verb, help=what)
        s.add_argument("name")
    c = sub.add_parser("create", help="make an ed25519 key; never overwrites")
    c.add_argument("name")
    c.add_argument("--comment")
    c.add_argument("--no-passphrase", action="store_true", help="an unattended key (the findings list it)")
    c.add_argument("--no-keychain", action="store_true", help="macOS: do not store the passphrase in the Keychain")
    c.add_argument("--host", help="append a Host block with this alias to ~/.ssh/config")
    c.add_argument("--hostname")
    c.add_argument("--user")
    c.add_argument("--port", type=int)
    s = sub.add_parser("send", help="register the public line on GitHub or a server")
    s.add_argument("name")
    s.add_argument("--to", required=True, help="github | server:ALIAS")
    s.add_argument("--as", dest="as_", choices=("authentication", "signing", "both"), default="authentication")
    s.add_argument("--title")
    s.add_argument("--verify", action="store_true", help="server: log in with the new key alone afterwards")
    s.add_argument("--interactive", action="store_true", help="server: allow a password prompt")
    f = sub.add_parser("findings", help="what the saved inventory owes you")
    f.add_argument("--json", action="store_true")
    return p


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv or ["list"])
    try:
        if args.command == "list":
            return cmd_list(args)
        if args.command in ("show", "copy"):
            return cmd_show(args, clipboard=args.command == "copy")
        if args.command == "create":
            return cmd_create(args)
        if args.command == "send":
            return cmd_send(args)
        return cmd_findings(args)
    except Refused as exc:
        print(f"ssh-keys: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
