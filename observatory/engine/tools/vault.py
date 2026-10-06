#!/usr/bin/env python3
"""Project secrets: one door in, one door out, and a leak is a recorded fact.

    T="$(project-observatory full-path)/tools"          # the installed engine's tools
    python "$T/vault.py" put <project> <env> <NAME> < file   # value on stdin, only
    python "$T/vault.py" list [project] [env]                # names and metadata, never values
    python "$T/vault.py" inject <project> <env> <dir>        # write <dir>/.env, only if git ignores it
    python "$T/vault.py" rotate <project> <env> <NAME>       # new value on stdin; old is archived
    python "$T/vault.py" leak <project> <env> <NAME> --where "…"   # mark leaked, MUST say where
    python "$T/vault.py" settle <project> <env> <NAME> --how "…"   # close a leak, with evidence
    python "$T/vault.py" moved <project> <env> <NAME> --how "…"    # record a movement done elsewhere
    python "$T/vault.py" movements [project]                 # the movement journal
    python "$T/vault.py" remove <project> <env> <NAME> [--retired] [--force]  # delete a slot, or only its archives
    python "$T/vault.py" bind <project> <env> <NAME> --header-for <https URL|host>   # the one MCP server
    python "$T/vault.py" bind <project> <env> <NAME> --clear                          #   `use_secret.py header` may serve it to
    python "$T/vault.py" leaks                               # the register, oldest unrotated first
    python "$T/vault.py" backup                              # run the store's encrypted backup now

WHERE VALUES LIVE, AND WHY NOT A NEW STORE. Values go under an existing
credential store whose whole job is holding plaintext credentials safely — a
directory at mode 700/600, ignored by git, and backed up as a whole by its own
encrypted backup script. Project secrets go under it
(`projects/<project>/<env>/<NAME>`, or `OBSERVATORY_VAULT_DIR`), so backup,
restore and machine migration are inherited rather than rebuilt. A second store
beside a working one is how one of them quietly stops being backed up.

THE CONTRACT WITH AGENTS — the four rules the observatory skill makes mandatory:

  1. A value travels only on stdin, never in argv and never in a chat message.
     Argv lands in shell history and `ps`; a chat message lands in a transcript
     that outlives the key.
  2. An agent never opens the store's files. `inject` writes the project's
     `.env` and prints VARIABLE NAMES; from then on the agent works with names.
     The store refuses to run at all if the target .env is not gitignored.
  3. An agent that SEES a secret value anywhere it does not belong — a log, a
     transcript, a commit, a pasted terminal — runs `leak` with `--where`.
     Marking is mandatory and cheap; rotation is the operator's call, but the
     register must know first.
  4. A leaked secret stays in the register until it is settled — `settle`, or
     `moved --settle` — with evidence that the old value is revoked at its
     issuer and that its consumers were checked. A local `rotate` alone does not
     settle it: the retired value keeps working until revoked at the provider.

METADATA IS NOT SECRET. `meta.json` beside each value (created, rotated, envs,
and `header_for`, the server a slot is bound to) and `leaks.jsonl` hold names,
dates and places — never values — so the register can be read, listed and
rendered without touching a single secret byte.

Every value file is chmod 600 and every write goes through a sibling-and-rename,
so a crash mid-write cannot leave a half-written credential.
"""

from __future__ import annotations
import argparse
import contextlib
import fcntl
import functools
import re
import tempfile
import uuid
import datetime
import json
import os
import pathlib
import stat
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import credential_shape  # noqa: E402
import leak_register  # noqa: E402
import paths                                            
import safe_git  # noqa: E402  — the engine's one git door
import vault_project  # noqa: E402

GATEWAY = paths.source_path("gateway_root", paths.HOME / "disabled/gateway")
STORE = pathlib.Path(os.environ.get("OBSERVATORY_VAULT_DIR",
                                    paths.source_path("secret_store", paths.SECRETS) / "projects"))
LEAKS = STORE / "leaks.jsonl"
#: EVERY MOVEMENT OF EVERY KEY, append-only, names and places only. The
#: operator's rule (2026-09-14): whether an agent issues, rotates, delivers,
#: revokes or settles a key — through these tools, through a provider's CLI
#: (`heroku config:set`, `doctl`), through a dashboard — the movement is
#: recorded in the same turn. The tools here write it themselves; anything
#: done outside them is recorded with `vault.py moved`. A rotation nobody
#: recorded looks exactly like one that never happened.
MOVES = STORE / "movements.jsonl"
ENVS = ("local", "stage", "prod")
#: The backup script's name, and the two places it is looked for under
#: `sources.gateway_root`: the root, then `bin/`. The documentation named only
#: `bin/` while this looked only at the root, so a script placed as documented
#: was "not on this machine". Its contract: a bash script, run with no
#: arguments; its output and its exit code are `vault.py backup`'s.
BACKUP_NAME = "backup-secrets.sh"


def backup_script(root: pathlib.Path) -> pathlib.Path | None:
    for candidate in (root / BACKUP_NAME, root / "bin" / BACKUP_NAME):
        if candidate.is_file():
            return candidate
    return None


class VaultBoundaryError(ValueError):
    """Safe static diagnostics, never interpolated secret or request values."""


def die(msg: str) -> None:
    sys.exit(f"vault: {msg}")


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _no_symlinks(path: pathlib.Path) -> None:
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise VaultBoundaryError("Private paths must be absolute and contain no symbolic links")


def _private_dirs(leaf: pathlib.Path) -> None:
    _no_symlinks(leaf)
    leaf.mkdir(parents=True, exist_ok=True, mode=0o700)
    _no_symlinks(leaf)
    # Injecting an env file must not chmod the user's whole project directory.
    walk = leaf
    while walk == STORE or STORE in walk.parents:
        fd = os.open(walk, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
        if walk == STORE:
            break
        walk = walk.parent


def _read_private(path: pathlib.Path, *, private: bool = True) -> str:
    _no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (private and info.st_mode & 0o077):
            raise VaultBoundaryError("Private data must be a regular owner-only file")
        return stream.read()


def _append_private(path: pathlib.Path, text: str) -> None:
    _no_symlinks(path)
    _private_dirs(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK |
                 getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise VaultBoundaryError("Private journal must be a regular file")
        os.fchmod(stream.fileno(), 0o600)
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_write(path: pathlib.Path, data: str, mode: int = 0o600) -> None:
    _no_symlinks(path)
    _private_dirs(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".vault-write-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _no_symlinks(path)
        os.replace(temporary, path)
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def _mutation_lock():
    _private_dirs(STORE)
    lock = STORE / ".vault.lock"
    _no_symlinks(lock)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise VaultBoundaryError("Vault lock must be a regular file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        for journal_path in (MOVES, LEAKS):
            _no_symlinks(journal_path)
            if journal_path.exists() and not journal_path.is_file():
                raise VaultBoundaryError("Private journal must be a regular file")
        yield
    finally:
        os.close(fd)


def _serialized(function):
    @functools.wraps(function)
    def locked(*args, **kwargs):
        with _mutation_lock():
            return function(*args, **kwargs)
    return locked


def validate_names(project: str, env: str | None = None, name: str | None = None) -> None:
    """The slot's three names, refused when malformed OR credential-shaped.

    The second test is the one the first cannot make: a pasted token is a
    perfectly good identifier, and one accepted as a PROJECT became the folder
    `projects/<token>/…`, and from there `credential:vault/<token>/…` in the
    registry, the findings, the dashboard pages and the MCP answers. The shape
    test is `credential_shape`, the same one every other door uses. Messages
    name the field, never the text."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", project or ""):
        raise VaultBoundaryError("Project must be one plain identifier")
    if env is not None and env not in ENVS:
        raise VaultBoundaryError("env must be one of local, stage, prod")
    if name is not None and not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name or ""):
        raise VaultBoundaryError("Secret NAME must be UPPER_SNAKE_CASE")
    for field, text in (("project", project), ("NAME", name)):
        try:
            credential_shape.refuse(field, text)
        except ValueError as exc:
            raise VaultBoundaryError(str(exc)) from None


def _note(field: str, text: str) -> str:
    """A free-text note (`--how`, an evidence reference), refused when it
    carries a credential shape: these land in the leak register and the
    movement journal, which the board and the keys page print."""
    kind = credential_shape.find(text)
    if kind:
        die(f"{field} looks like it carries a credential ({credential_shape.describe(kind)}); "
            f"describe the operation and cite references (a short commit id, a ticket, a "
            f"release number), never the value")
    return text


_PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def project_folder(text: str, env: str | None = None, name: str | None = None, *,
                   verb: str = "put") -> tuple[str, str]:
    """(the vault folder to use, a sentence saying so or "") for a typed PROJECT.

    PROJECT IS THE PROJECT'S FOLDER NAME, and the registry id (`project:<slug>`
    or the bare slug) is accepted and normalised to it, so one project keeps
    ONE vault directory whichever name a person types (`vault_project.py`
    holds the rule and its ambiguity check). Two exceptions, both about what
    the store already holds rather than what a new slot should be called:

      * a verb that addresses an existing slot (`rotate`, `settle`, `leak`,
        `remove`, `moved`, `inject`, `list`) keeps the typed folder when a slot
        or an open leak is already filed under it — slots written before this
        rule under a registry slug stay reachable, and the sentence says where
        the project's folder is;
      * a string two projects claim is refused with both named, never guessed.

    A string the registry does not know is used as typed: an organisation's
    shared key has a folder of its own.
    """
    try:
        credential_shape.refuse("project", text)
    except ValueError as exc:
        raise VaultBoundaryError(str(exc)) from None
    r = vault_project.resolve(text)
    if r.how in ("folder", "unknown"):
        return text, ""
    if r.how == "unreadable":
        return text, r.sentence()
    if verb != "put" and _PLAIN.fullmatch(text or ""):
        here = STORE / text
        _no_symlinks(here)
        holds = (here.is_dir() if not (env and name) else
                 (here / env / name).is_file() or bool(_unsettled_for(f"{text}/{env}/{name}")))
        if holds:
            return text, (f"{text!r} names {r.project_id or 'a registered project'}, whose vault "
                          f"folder is {r.folder or 'not settled'}; the slot already filed under "
                          f"{text!r} is used")
    if r.how == "ambiguous":
        raise VaultBoundaryError(r.sentence())
    return r.folder, r.sentence()


def _slot(project: str, env: str, name: str) -> pathlib.Path:
    validate_names(project, env, name)
    result = STORE / project / env / name
    _no_symlinks(result)
    return result


def _meta_path(slot: pathlib.Path) -> pathlib.Path:
    return slot.with_name(slot.name + ".meta.json")


def _read_meta(slot: pathlib.Path) -> dict:
    p = _meta_path(slot)
    _no_symlinks(p)
    if p.exists() and not p.is_file():
        raise VaultBoundaryError("Secret metadata must be a regular file")
    if p.is_file():
        try:
            value = json.loads(_read_private(p))
            if not isinstance(value, dict):
                raise VaultBoundaryError("Secret metadata must be an object")
            rotations = value.get("rotations", 0)
            if type(rotations) is not int or rotations < 0:
                raise VaultBoundaryError("Secret rotation metadata must be a non-negative integer")
            return value
        except json.JSONDecodeError:
            raise VaultBoundaryError("Unreadable secret metadata; refusing to replace it") from None
    return {}


def _stdin_value() -> str:
    if sys.stdin.isatty():
        die("the value must arrive on stdin, never in an argument:\n"
            "    run vault put <project> <env> <NAME> with a protected file redirected to stdin")
    v = sys.stdin.read().strip()
    if not v:
        die("stdin was empty")
    if "\n" in v:
        die("the value carries a newline — one secret per slot; for a multi-line "
            "credential (a PEM key), base64 it and record that in the name: …_B64")
    return v


@_serialized
def cmd_put(a) -> int:
    slot = _slot(a.project, a.env, a.name)
    if slot.is_file() and not a.force:
        die(f"{a.project}/{a.env}/{a.name} already holds a value. `rotate` replaces "
            f"it keeping history; `put --force` overwrites losing it.")
    meta = _read_meta(slot)
    value = _stdin_value()
    _atomic_write(slot, value)
    meta.setdefault("created", now())
    meta["updated"] = now()
    _atomic_write(_meta_path(slot), json.dumps(meta, indent=1), 0o600)
    journal("put", f"{a.project}/{a.env}/{a.name}")
    print(f"stored {a.project}/{a.env}/{a.name}: length {len(value)}, "
          f"mode 600; value hidden")
    return 0


#: An S3-compatible account endpoint whose first label IS the account id — an identifier the
#: provider prints in every URL and dashboard, not a secret (Cloudflare R2:
#: `<32 hex>.r2.cloudflarestorage.com`, with `eu.` or `fedramp.` for a jurisdiction). The shape
#: check reads 32 hex as a key; for exactly this label of exactly these hosts it is waived, and the
#: rest of the address is still checked. Without it a secret bound to its own R2 endpoint was
#: refused, and the only way left to read it put the value in a child's environment.
_ACCOUNT_ENDPOINT = re.compile(r"^((?:https?://)?)[0-9a-fA-F]{32}"
                               r"(\.(?:eu\.|fedramp\.)?r2\.cloudflarestorage\.com(?::[0-9]{1,5})?(?:/.*)?)$")


def _shape_checked(raw: str) -> str:
    """`raw` as the credential-shape check should see it: an account endpoint's id label masked."""
    return _ACCOUNT_ENDPOINT.sub(lambda m: f"{m.group(1)}account{m.group(2)}", raw)


#: An HTTP host as a binding may name it: DNS labels, or an IP literal.
_HOST = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                   r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")


def origin(text: str, *, https_only: bool = True) -> tuple[str, str, str]:
    """(origin `scheme://host[:port]`, the host as shown, the path that was dropped).

    THE BINDING IS AN ORIGIN, NOT A URL. `use_secret.py header` compares it with
    the `CLAUDE_CODE_MCP_SERVER_URL` Claude Code hands its `headersHelper`, and
    the question that comparison answers is "is this request going to the server
    the operator meant", which scheme, host and port decide. A path is accepted
    (an operator pastes the server's whole URL) and reported as not part of it.

    `https_only` is the binding's rule: a bearer bound to plain http would travel
    readable on the wire. The header door parses the server's URL with it off,
    so a plain-http server is named in the refusal rather than failing to parse.
    A bare host means https on the default port. Userinfo, a query and a
    fragment are refused: each is a place a credential rides in a URL.
    Messages name the rule, never the text, except the host — hosts are not
    secrets, and a refusal that names the wrong one is the useful kind.
    """
    raw = (text or "").strip()
    if credential_shape.find(_shape_checked(raw)):
        raise VaultBoundaryError("the address looks like it carries a credential; give the "
                                 "server's https URL or its host, never a value")
    import ipaddress
    import urllib.parse
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port
    except ValueError:
        raise VaultBoundaryError("the address is not a URL or a host[:port]") from None
    scheme = parts.scheme.lower()
    if https_only and scheme != "https":
        raise VaultBoundaryError("only https is accepted: a bearer bound to plain http "
                                 "would cross the network readable")
    if scheme not in ("https", "http"):
        raise VaultBoundaryError("the address must be an http(s) URL")
    if "@" in parts.netloc:
        raise VaultBoundaryError("the address carries userinfo (user:password@); "
                                 "give the server's URL without it")
    if parts.query or parts.fragment or raw.endswith(("?", "#")):
        raise VaultBoundaryError("the address carries a query or a fragment; "
                                 "the binding is the server's scheme, host and port")
    host = (parts.hostname or "").lower()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if not host or (literal is None and not _HOST.fullmatch(host)):
        raise VaultBoundaryError("the address names no valid host")
    shown = f"[{host}]" if literal is not None and literal.version == 6 else host
    default = 443 if scheme == "https" else 80
    if port is not None and port != default:
        shown = f"{shown}:{port}"
    path = parts.path if parts.path not in ("", "/") else ""
    return f"{scheme}://{shown}", shown, path


@_serialized
def cmd_bind(a) -> int:
    """Bind a slot to the one MCP server `use_secret.py header` may serve it to.

    Metadata only, in the slot's `meta.json`, and journalled like every other
    write here. The value is never read. A slot that holds no value is refused:
    a binding that describes nothing hides a typo in PROJECT, ENV or NAME.
    """
    slot = _slot(a.project, a.env, a.name)
    secret = f"{a.project}/{a.env}/{a.name}"
    if not slot.is_file():
        die(f"nothing at {secret} to bind — `vault.py put {a.project} {a.env} {a.name}` "
            f"with the value on stdin first; a binding describes a slot that exists")
    meta = _read_meta(slot)
    if a.clear:
        previous = meta.pop("header_for", None)
        meta.pop("header_bound", None)
        if previous is None:
            print(f"{secret} has no header binding; nothing to clear")
            return 0
        _atomic_write(_meta_path(slot), json.dumps(meta, indent=1), 0o600)
        journal("unbind", secret, header_for=previous)
        print(f"cleared: {secret} is no longer served by `use_secret.py header` "
              f"(it was bound to {previous})")
        return 0
    bound, _, dropped = origin(a.header_for)
    previous = meta.get("header_for")
    meta["header_for"] = bound
    meta["header_bound"] = now()
    _atomic_write(_meta_path(slot), json.dumps(meta, indent=1), 0o600)
    journal("bind", secret, header_for=bound,
            **({"replaced": previous} if previous and previous != bound else {}))
    print(f"bound {secret} to {bound}: `use_secret.py header` serves it only to an MCP "
          f"server at that scheme, host and port; value hidden")
    if dropped:
        print(f"  the path {dropped} is not part of the binding")
    if previous and previous != bound:
        print(f"  it was bound to {previous}")
    return 0


@_serialized
def cmd_rotate(a) -> int:
    slot = _slot(a.project, a.env, a.name)
    if not slot.is_file():
        # A key the vault never held can still leak, and it is rotated AT THE
        # PROVIDER — a provider CLI, a dashboard — so there is no value to hand
        # this command. The refusal therefore names the way to close the
        # register instead of only saying "use put".
        die(f"nothing at {a.project}/{a.env}/{a.name} to rotate — `put` a value to "
            f"start tracking it, or, if it was rotated at its provider, settle the "
            f"register: `vault.py settle {a.project} {a.env} {a.name} --how \"…\" "
            f"--revocation-evidence \"…\" --consumer-evidence \"…\"`")
    meta = _read_meta(slot)
    value = _stdin_value()
    old = _read_private(slot).strip()
    if value == old:
        die("the new value equals the old one — that is not a rotation")
    stamp = now().replace(":", "") + "-" + uuid.uuid4().hex[:12]
    archive = slot.with_name(f"{slot.name}.retired-{stamp}")
    _atomic_write(archive, old)
    _atomic_write(slot, value)
    meta["rotated"] = now()
    meta.setdefault("rotations", 0)
    meta["rotations"] += 1
    _atomic_write(_meta_path(slot), json.dumps(meta, indent=1), 0o600)
    journal("rotate", f"{a.project}/{a.env}/{a.name}", archived=archive.name, scope="local_slot")
    # A local replacement is not a settlement: the retired value still works
    # until it is revoked at its provider, so the leak register is left open.
    print(f"rotated {a.project}/{a.env}/{a.name}: old value archived as "
          f"{archive.name} (600); new length {len(value)}; value hidden")
    print("  local replacement does not settle a leak; record settlement after "
          "verifying revocation and consumers")
    print("  the RETIRED value still works until revoked at its provider — "
          "revoke it there, then delete the archive when you no longer need it")
    # THE SERVICES STILL ON THE OLD VALUE. A service reads its keys once, when
    # `use_secret.py serve` starts it, so it keeps the old one until restarted.
    # Naming them is what turns "consumers were checked" from a promise into a
    # list.
    # Beside this file, whether it runs as a script or is imported as a module.
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import use_secret
    running = use_secret.consumers(a.project, a.env, a.name)
    if running:
        print(f"  {len(running)} service(s) started with this slot read it at start and "
              f"still hold the old value until restarted:")
        for row in running:
            print(f"    {row['consumer']} (started {row.get('at', '?')}): "
                  f"launchctl kickstart -k gui/$(id -u)/{row['consumer']}")
    return 0


def _leak_rows() -> list[dict]:
    _no_symlinks(LEAKS)
    if not LEAKS.is_file():
        return []
    rows = []
    for line in _read_private(LEAKS).splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                rows.append({"unparseable": line[:80]})
    return rows


def journal(event: str, secret: str, **detail) -> None:
    """One movement, on the record. Never a value: callers pass names, places,
    providers, and the `how` of what they did."""
    assert "value" not in detail, "a value never enters the journal"
    row = {"at": now(), "event": event, "secret": secret,
           "by": os.environ.get("USER", "unknown"),
           "tool": detail.pop("tool", "vault.py"), **detail}
    _append_private(MOVES, json.dumps(row, ensure_ascii=False) + "\n")


def movements(project: str | None = None) -> list[dict]:
    _no_symlinks(MOVES)
    if not MOVES.is_file():
        return []
    out = []
    for line in _read_private(MOVES).splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if project and not (row.get("secret") or "").startswith(project + "/"):
            continue
        out.append(row)
    return out


@_serialized
def cmd_moved(a) -> int:
    """Record a movement of a key done outside these tools (a provider CLI, a dashboard)."""
    _slot(a.project, a.env, a.name)
    if not a.how or len(a.how.strip()) < 12:
        die("--how must describe what moved, where, and its evidence (at least 12 characters)")
    # `--settle` also closes the open leak rows of this slot, and then needs
    # the same revocation and consumer evidence `settle` does.
    detail = _settlement_detail(a) if a.settle else {"how": _note("--how", a.how.strip())}
    secret = f"{a.project}/{a.env}/{a.name}"
    rows = _unsettled_for(secret) if a.settle else []
    journal("moved", secret, at_provider=a.at or "unknown", **detail)
    print(f"recorded: {secret} moved at {a.at or 'an unnamed provider'}")
    if a.settle:
        _record_settlements(rows, detail)
        print(f"  settled {len(rows)} open leak(s) with manual revocation and consumer attestations"
              if rows else "  no open leak of it to settle")
    return 0


def cmd_movements(a) -> int:
    rows = movements(a.project)
    if not rows:
        print("no movements recorded" + (f" for {a.project}" if a.project else ""))
        return 0
    for r in rows[-(a.last or 40):]:
        extra = "; ".join(f"{k}={v}" for k, v in r.items()
                          if k not in ("at", "event", "secret", "by", "tool") and v)
        print(f"  {r.get('at', '?')}  {r.get('event', '?'):8s} {r.get('secret', '?'):48s} "
              f"{r.get('tool', '?')}  {extra[:110]}")
    print(f"{len(rows)} movement(s)" + (f" for {a.project}" if a.project else "")
          + " — names and places, never values")
    return 0


def _append_leak(row: dict) -> None:
    _append_private(LEAKS, json.dumps(row, ensure_ascii=False) + "\n")


def _settlement_detail(a) -> dict:
    """The settlement record, refused unless it carries revocation and consumer evidence."""
    if not a.how or len(a.how.strip()) < 12:
        die("--how must describe the operation and its evidence (at least 12 characters)")
    detail = {"verification": "manual_attestation", "how": _note("--how", a.how.strip())}
    for field in ("revocation_evidence", "consumer_evidence"):
        value = getattr(a, field, None)
        flag = "--" + field.replace("_", "-")
        if not isinstance(value, str) or len(value.strip()) < 12:
            die(f"{flag} must reference the completed check (at least 12 characters); "
                "settlement needs both issuer revocation and consumer evidence")
        detail[field] = _note(flag, value.strip())
    return detail


def _unsettled_for(secret: str) -> list[dict]:
    rows = _leak_rows()
    settled_of = leak_register.settled_ids(rows)
    return [r for r in rows if r.get("event") == "leaked"
            and r.get("secret") == secret and r.get("id") not in settled_of]


def _record_settlements(rows: list[dict], detail: dict) -> None:
    for row in rows:
        _append_leak({"event": "settled", "of": row["id"], "at": now(),
                      "by": os.environ.get("USER", "unknown"), **detail})


@_serialized
def cmd_settle(a) -> int:
    """Close every open leak of one slot with a manual attestation.

    A leak is settled only by evidence, never as a side effect: `rotate`
    replaces the local slot, but a key the provider still accepts is still
    leaked. This records how the old value was revoked and how its consumers
    were checked, in the leak register and the movement journal.
    """
    _slot(a.project, a.env, a.name)
    detail = _settlement_detail(a)
    secret = f"{a.project}/{a.env}/{a.name}"
    rows = _unsettled_for(secret)
    if not rows:
        die(f"no open leak of {secret}; `vault.py leaks` shows the register")
    _record_settlements(rows, detail)
    journal("settle", secret, **detail)
    print(f"settled {len(rows)} open leak(s) of {secret}")
    print("  recorded manual revocation and consumer attestations; no provider probe was run")
    print("  the board drops `secret.leaked_unrotated` at the next build; the register keeps the history")
    return 0


@_serialized
def cmd_leak(a) -> int:
    slot = _slot(a.project, a.env, a.name)
    if not a.where or len(a.where.strip()) < 8:
        die("--where must SAY where the value was seen (a path, a URL, 'chat "
            "transcript of session X') — a leak record that names no place "
            "cannot be judged later")
    known = slot.is_file()
    # THE PLACE IS KEPT, THE VALUE IS NOT. A person recording a sighting often
    # pastes the line they saw, value included; refusing would lose the leak,
    # so the credential-shaped part is replaced and the rest of the place stays.
    where = credential_shape.redact(a.where.strip())
    row = {"event": "leaked", "id": f"leak:{now()}:{a.project}/{a.env}/{a.name}",
           "secret": f"{a.project}/{a.env}/{a.name}", "where": where,
           "at": now(), "by": os.environ.get("USER", "unknown"),
           "slot_exists": known}
    _append_leak(row)
    journal("leak", row["secret"], where=row["where"][:120])
    print(f"recorded: {row['id']}")
    print(f"  where: {row['where']}")
    if where != a.where.strip():
        print("  the place carried a credential-shaped value; it was recorded as [redacted]")
    if known:
        print("  revoke the old version at its provider and verify consumers; "
              "replacing a local slot leaves this row open")
    else:
        print("  note: no slot holds this name; recorded anyway — a leak of a key "
              "the store never held is still a leak")
    print("  the board keeps `secret.leaked_unrotated` until explicit settlement "
          "records revocation and consumer evidence")
    return 0


def open_leaks() -> list[dict]:
    """Every leak row not yet settled — the register's one public reading.

    A function rather than a convention, because two readers (the board and
    `tools/cloudflare.py rotate --leaked`) were about to parse the JSONL with
    their own copies of the settled-set logic, and two copies of "is this leak
    still open" WILL disagree the day the format grows a field.
    """
    rows = _leak_rows()
    settled_of = leak_register.settled_ids(rows)
    return [r for r in rows if r.get("event") == "leaked"
            and r.get("id") not in settled_of]


def cmd_leaks(a) -> int:
    rows = _leak_rows()
    leaks = [r for r in rows if r.get("event") == "leaked"]
    settled_of = leak_register.settled_ids(rows)
    legacy = leak_register.legacy_rotation_ids(rows)
    # (cmd_leaks keeps its own pass because it prints settled rows too;
    # `open_leaks()` above is the reading everything else shares.)
    if not leaks:
        print("no leaks recorded")
        return 0
    open_n = 0
    for r in sorted(leaks, key=lambda r: r.get("at", "")):
        is_open = r.get("id") not in settled_of
        open_n += is_open
        mark = "OPEN   " if is_open else "settled"
        print(f"  {mark} {r.get('at', '?')}  {r.get('secret', '?')}")
        if is_open and r.get("id") in legacy:
            print("          closed only by a local rotate (0.2.0 or earlier): revocation "
                  "and consumers were never recorded")
        print(f"          seen: {r.get('where', '?')}")
    print(f"\n{len(leaks)} leak(s), {open_n} still open" +
           (" — verify revocation and consumers, then record settlement" if open_n else ""))
    return 1 if open_n and a.check else 0


def cmd_list(a) -> int:
    if not STORE.is_dir():
        print(f"empty store ({STORE})")
        return 0
    found = 0
    for slot in sorted(STORE.rglob("*")):
        if not slot.is_file() or slot.name.endswith((".meta.json", ".tmp")) \
                or ".retired-" in slot.name or slot.name == "leaks.jsonl":
            continue
        rel = slot.relative_to(STORE)
        parts = rel.parts
        if len(parts) != 3:
            continue
        project, env, name = parts
        if a.project and project != a.project:
            continue
        if a.env and env != a.env:
            continue
        meta = _read_meta(slot)
        mode = stat.S_IMODE(slot.stat().st_mode)
        extras = []
        if meta.get("rotated"):
            extras.append(f"rotated {meta['rotated'][:10]}×{meta.get('rotations', 1)}")
        if isinstance(meta.get("header_for"), str):
            extras.append(f"header for {meta['header_for']}")
        if mode & 0o077:
            extras.append(f"MODE {mode:o} — READABLE BEYOND OWNER")
        print(f"  {project}/{env}/{name}  ({slot.stat().st_size}B"
              + (", " + ", ".join(extras) if extras else "") + ")")
        found += 1
    if not found:
        print("  nothing matches")
    return 0


@_serialized
def cmd_inject(a) -> int:
    """Write the project's .env; the agent works with NAMES from here on."""
    validate_names(a.project, a.env)
    target_dir = pathlib.Path(a.dir).expanduser().absolute()
    _no_symlinks(target_dir)
    if not target_dir.is_dir():
        die(f"{target_dir} is not a directory")
    env_file = target_dir / ".env"
    # THE GITIGNORE CHECK IS NOT OPTIONAL. An .env that git would commit turns
    # an injection into a publication on the next `git add -A`.
    probe = safe_git.run(["check-ignore", "-q", str(env_file)], cwd=target_dir, timeout=60)
    in_repo = safe_git.run(["rev-parse", "--git-dir"], cwd=target_dir,
                           timeout=60).returncode == 0
    if in_repo and probe.returncode != 0:
        die(f"{env_file} is NOT gitignored in that repository — refusing to write "
            f"secrets where `git add -A` can publish them. Add `.env` to its "
            f".gitignore first.")
    src = STORE / a.project / a.env
    if not src.is_dir():
        die(f"no secrets stored for {a.project}/{a.env} — `python \"$(project-observatory full-path)/tools/vault.py\" list` "
            f"shows what exists")
    names = []
    lines = []
    _no_symlinks(env_file)
    _no_symlinks(src)
    old = _read_private(env_file, private=False) if env_file.is_file() else ""
    kept = [l for l in old.splitlines()
            if l.strip() and not l.startswith("# vault:")
            and l.split("=", 1)[0] not in
            {s.name for s in src.iterdir() if s.is_file()
             and not s.name.endswith((".meta.json", ".tmp")) and ".retired-" not in s.name}]
    for slot in sorted(src.iterdir()):
        if not slot.is_file() or slot.name.endswith((".meta.json", ".tmp")) \
                or ".retired-" in slot.name:
            continue
        lines.append(f"{slot.name}={_read_private(slot).strip()}")
        names.append(slot.name)
    if not names:
        die(f"{a.project}/{a.env} holds no values")
    body = (f"# vault: {a.project}/{a.env} injected {now()} — regenerate with\n"
            f"# vault:   python \"$(project-observatory full-path)/tools/vault.py\" inject {a.project} {a.env} {target_dir}\n"
            + "\n".join(kept + lines) + "\n")
    _atomic_write(env_file, body)
    journal("inject", f"{a.project}/{a.env}/*", names=sorted(names), into=str(env_file))
    print(f"wrote {env_file} (600): {len(names)} value(s) from {a.project}/{a.env}")
    print("  work with the NAMES from here on:")
    for n in names:
        print(f"    {n}")
    return 0


@_serialized
def cmd_remove(a) -> int:
    """Delete a slot (value and metadata), or with --retired only the archives a
    rotation left. Journalled; refused while a leak of the slot is open unless
    --force, because removing the local copy settles nothing at the provider."""
    slot = _slot(a.project, a.env, a.name)
    secret = f"{a.project}/{a.env}/{a.name}"
    archives = sorted(slot.parent.glob(f"{slot.name}.retired-*")) if slot.parent.is_dir() else []
    for p in archives:
        _no_symlinks(p)
    if a.retired:
        if not archives:
            die(f"nothing at {secret} to remove: no retired archive")
        for p in archives:
            p.unlink()
        journal("remove-retired", secret, removed=len(archives))
        print(f"removed {len(archives)} retired archive(s) of {secret}; the slot is kept")
        return 0
    if not slot.is_file():
        die(f"nothing at {secret} to remove; `vault.py list {a.project}` shows the slots")
    if _unsettled_for(secret) and not a.force:
        die(f"{secret} has an open leak; removing the local copy settles nothing at the "
            f"provider. Settle it (`vault.py settle …`) or pass --force")
    meta = _meta_path(slot)
    _no_symlinks(meta)
    slot.unlink()
    meta.unlink(missing_ok=True)
    for p in archives:
        p.unlink()
    journal("remove", secret, archives_removed=len(archives), forced=bool(a.force))
    print(f"removed {secret}" + (f" and {len(archives)} retired archive(s)" if archives else "")
          + "; the value was not printed")
    return 0


def cmd_backup(a) -> int:
    import configuration
    if not (configuration.load().get("sources") or {}).get("gateway_root"):
        # The placeholder path a missing source resolves to (`<home>/disabled/…`)
        # names a folder nobody made; say which setting is missing instead.
        die("no backup script is configured: set sources.gateway_root to the folder that "
            "holds backup-secrets.sh (project-observatory full configure sources gateway_root PATH)")
    script = backup_script(GATEWAY)
    if script is None:
        die(f"no {BACKUP_NAME} at {GATEWAY / BACKUP_NAME} or {GATEWAY / 'bin' / BACKUP_NAME}")
    p = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=600)
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    return p.returncode


#: What each positional means, shown by every subcommand's --help.
PROJECT_HELP = ("the project's folder name (e.g. alpha-web); its registry id "
                "(project:<slug> or the bare slug) is accepted and normalised to that folder")
ENV_HELP = "local, stage or prod"
NAME_HELP = "the variable, UPPER_SNAKE_CASE"


def _slot_args(p, *, optional: bool = False) -> None:
    if optional:
        p.add_argument("project", nargs="?", help=PROJECT_HELP)
        p.add_argument("env", nargs="?", help=ENV_HELP)
        return
    p.add_argument("project", help=PROJECT_HELP)
    p.add_argument("env", help=ENV_HELP)
    p.add_argument("name", help=NAME_HELP)


def main(argv: list[str]) -> int:
    doc = (__doc__ or "Manage private secret slots and movement records").strip("\n")
    usage = doc.split("\n\n", 2)[1] if doc.count("\n\n") >= 1 else ""
    ap = argparse.ArgumentParser(description=doc.splitlines()[0], epilog=usage,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("put");    _slot_args(p); p.add_argument("--force", action="store_true")
    p = sub.add_parser("rotate"); _slot_args(p)
    p = sub.add_parser("leak");   _slot_args(p); p.add_argument("--where", required=True)
    p = sub.add_parser("settle"); _slot_args(p); p.add_argument("--how", required=True)
    p.add_argument("--revocation-evidence", help="reference to verified old-version revocation; no secret values")
    p.add_argument("--consumer-evidence", help="reference to consumer checks, or evidence that none remain; no secret values")
    p = sub.add_parser("moved");  _slot_args(p); p.add_argument("--how", required=True); p.add_argument("--at", help="the provider: heroku, digitalocean, cloudflare, a dashboard"); p.add_argument("--settle", action="store_true", help="also settle an open leak of it")
    p.add_argument("--revocation-evidence", help="required with --settle: old-version revocation evidence")
    p.add_argument("--consumer-evidence", help="required with --settle: consumer verification evidence")
    p = sub.add_parser("movements"); p.add_argument("project", nargs="?", help=PROJECT_HELP); p.add_argument("--last", type=int)
    p = sub.add_parser("leaks");  p.add_argument("--check", action="store_true")
    p = sub.add_parser("list");   _slot_args(p, optional=True)
    p = sub.add_parser("inject"); p.add_argument("project", help=PROJECT_HELP); p.add_argument("env", help=ENV_HELP); p.add_argument("dir", help="the project directory whose git-ignored .env is written")
    p = sub.add_parser("remove"); _slot_args(p)
    p.add_argument("--retired", action="store_true", help="remove only the archives rotation left; keep the slot")
    p.add_argument("--force", action="store_true", help="remove even while a leak of it is open")
    p = sub.add_parser("bind", help="bind a slot to the one MCP server `use_secret.py header` may serve it to")
    _slot_args(p)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--header-for", metavar="URL_OR_HOST",
                        help="the MCP server's https URL or host[:port]; a bare host means https")
    target.add_argument("--clear", action="store_true", help="remove the binding")
    sub.add_parser("backup")
    a = ap.parse_args(argv[1:])
    try:
        if getattr(a, "project", None):
            a.project, said = project_folder(a.project, getattr(a, "env", None),
                                             getattr(a, "name", None), verb=a.cmd)
            if said:
                print(f"project: {said}")
            if a.project.startswith("project:"):
                raise VaultBoundaryError(
                    f"{credential_shape.echo(a.project)} is not a project in the registry; "
                    f"PROJECT is the project's folder name (a registry id is accepted only "
                    f"for a project the registry holds)")
            validate_names(a.project, getattr(a, "env", None), getattr(a, "name", None))
        return {"put": cmd_put, "settle": cmd_settle, "moved": cmd_moved, "movements": cmd_movements,
                "rotate": cmd_rotate, "leak": cmd_leak, "leaks": cmd_leaks,
                "list": cmd_list, "inject": cmd_inject, "backup": cmd_backup, "remove": cmd_remove,
                "bind": cmd_bind}[a.cmd](a)
    except VaultBoundaryError as exc:
        print(f"vault: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("vault: private filesystem or input validation refused the operation", file=sys.stderr)
        return 2



if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
