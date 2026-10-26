#!/usr/bin/env python3
"""Vault slots across the operator's own machines, compared by keyed digests, never by values.

    project-observatory full vault-digests [view] [--json] [--peers DIR] [--max-age-hours N]
    project-observatory full vault-digests write [--machine NAME]
    project-observatory full vault-digests mark PROJECT ENV NAME --why TEXT
    project-observatory full vault-digests unmark PROJECT ENV NAME
    project-observatory full vault-digests key status
    project-observatory full vault-digests key import < FILE
    project-observatory full vault-digests key export FILE

The design: docs/design/VAULT-DIGESTS.md (OBS-194, step 1).

WHY DIGESTS AND NOT VALUES. "Does the other machine hold the same key?" needs an answer that
can travel and be stored without being a credential. Each machine computes, per slot
(`project/env/NAME`), HMAC-SHA-256 under the ESTATE KEY and writes a manifest: slot, digest,
when the slot was last rotated, whether it differs on purpose, this machine's name and when the
manifest was made. A manifest holds no value and no unkeyed hash of one: an unkeyed SHA-256 of
a short token is a lookup away from the token, a keyed one is not without the key.

WHERE THE ESTATE KEY LIVES. In the vault itself (`.estate-digest-key` at the store's top, beside
`leaks.jsonl` and `movements.jsonl`, owner-only), so the vault's own backup carries it and no
second store exists. `write` creates it on first use; nothing prints it. Two machines compare
only under one key: `key export FILE` writes it to a new owner-only file and `key import` reads it
on stdin. Moving it between machines as a sealed envelope is step 2, with the transport.

WHAT A PEER IS IN STEP 1. A manifest file in the peers folder (`--peers`, else
`OBSERVATORY_VAULT_PEERS`, else `<state>/vault-digests/peers`). Step 2 delivers them over the
network; until then the operator, or a test, places them there. A peer whose manifest is older
than `--max-age-hours` (48 by default), unreadable, or made under another estate key is
UNREACHABLE, with the time of its last answer: an old answer is not evidence about today.

THE FIVE STATES, per slot and peer, seen from this machine: `same`, `differs`, `only here` (this
machine holds it and the peer does not), `missing` (the peer holds it and this machine does not)
and `unreachable`. A `differs` row says why when it can: `on purpose` (marked here or by the peer
with `mark`), or `stale` (one side rotated later than the other: the older copy is stale).

Exit codes: 0 done (view: every slot matches); 1 view: something differs, is missing or a peer is
unreachable; 2 refused before anything changed.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import socket
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import credential_shape  # noqa: E402
import paths  # noqa: E402
import vault  # noqa: E402  (the store, its private-file helpers, its lock and its journal)

CMD = "project-observatory full vault-digests"
SCHEMA = "vault-digests/1"
ALGORITHM = "HMAC-SHA-256"
KEY_NAME = ".estate-digest-key"
KEY_SHAPE = re.compile(r"[0-9a-f]{64}")
DIGEST_SHAPE = re.compile(r"[0-9a-f]{64}")
MACHINE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SLOT_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
SLOT_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}/(local|stage|prod)/[A-Z_][A-Z0-9_]{0,127}")
MAX_AGE_HOURS = 48
#: The label the key id is derived under. The id names WHICH key a manifest was made with, so
#: two machines on different keys are told apart without either revealing it.
KEY_ID_LABEL = b"observatory vault-digests key-id/1"
#: A manifest larger than this is refused unread: a peers folder is written by hand in step 1.
MAX_MANIFEST_BYTES = 4 * 1024 * 1024

SAME, DIFFERS, ONLY_HERE, MISSING, UNREACHABLE = "same", "differs", "only here", "missing", "unreachable"


class Refused(Exception):
    """A safe, static diagnostic: never a value, a key or a digest."""


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(text: object) -> datetime.datetime | None:
    if not isinstance(text, str):
        return None
    try:
        value = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def digest_dir() -> pathlib.Path:
    return paths.STATE / "vault-digests"


def local_manifest_path() -> pathlib.Path:
    return digest_dir() / "local.json"


def peers_dir(explicit: str | None = None) -> pathlib.Path:
    value = explicit or os.environ.get("OBSERVATORY_VAULT_PEERS")
    folder = pathlib.Path(value).expanduser() if value else digest_dir() / "peers"
    if not folder.is_absolute():
        raise Refused("the peers folder must be an absolute path")
    # Resolved like `key export`'s folder (macOS's /tmp and /var are links); each manifest in it
    # is still refused when it is a link itself.
    return folder.resolve()


def machine_name(explicit: str | None = None) -> str:
    """This machine's name in manifests: `--machine`, `OBSERVATORY_MACHINE_NAME`, the host name."""
    name = explicit or os.environ.get("OBSERVATORY_MACHINE_NAME") or socket.gethostname().split(".")[0]
    if not MACHINE.fullmatch(name or "") or credential_shape.shaped(name):
        raise Refused("the machine name must be one plain identifier (letters, digits, . _ -)")
    return name


# --- the estate key -------------------------------------------------------------------------

def key_path() -> pathlib.Path:
    return vault.STORE / KEY_NAME


def _read_key() -> str | None:
    path = key_path()
    vault._no_symlinks(path)
    if not path.exists():
        return None
    text = vault._read_private(path).strip()
    if not KEY_SHAPE.fullmatch(text):
        raise Refused("the estate key file is not 64 lowercase hexadecimal characters; restore it "
                      "from the vault's backup or import the estate's key")
    return text


def _key(create: bool) -> str | None:
    found = _read_key()
    if found or not create:
        return found
    with vault._mutation_lock():
        found = _read_key()
        if found:
            return found
        vault._atomic_write(key_path(), secrets.token_hex(32) + "\n")
        vault.journal("estate-key-create", "estate/digest-key", tool="vault_digests.py",
                      key_id=key_id(_read_key() or ""))
    return _read_key()


def key_id(key: str) -> str:
    return hmac.new(bytes.fromhex(key), KEY_ID_LABEL, hashlib.sha256).hexdigest()[:16]


def digest(key: str, value: str) -> str:
    """HMAC-SHA-256 of the value under the estate key, as the vault hands values out (stripped)."""
    return hmac.new(bytes.fromhex(key), value.encode("utf-8"), hashlib.sha256).hexdigest()


# --- this machine's manifest ----------------------------------------------------------------

def local_slots() -> list[tuple[str, pathlib.Path]]:
    """(`project/env/NAME`, the slot's file) for every slot of the store, as `vault.py list` reads it."""
    out: list[tuple[str, pathlib.Path]] = []
    store = vault.STORE
    if not store.is_dir():
        return out
    for project in sorted(store.iterdir()):
        if project.name.startswith(".") or project.is_symlink() or not project.is_dir():
            continue
        for env in vault.ENVS:
            folder = project / env
            if folder.is_symlink() or not folder.is_dir():
                continue
            for slot in sorted(folder.iterdir()):
                if SLOT_NAME.fullmatch(slot.name) and not slot.is_symlink() and slot.is_file():
                    out.append((f"{project.name}/{env}/{slot.name}", slot))
    return out


def build_manifest(machine: str, key: str) -> dict:
    slots: dict[str, dict] = {}
    for name, slot in local_slots():
        meta = vault._read_meta(slot)
        row: dict = {"digest": digest(key, vault._read_private(slot).strip()),
                     "rotated_at": meta.get("rotated") if isinstance(meta.get("rotated"), str) else None}
        mark = meta.get("differs_on_purpose")
        if isinstance(mark, dict):
            row["on_purpose"] = {"why": str(mark.get("why") or ""), "at": str(mark.get("at") or "")}
        slots[name] = row
    return {"schema": SCHEMA, "machine": machine, "generated_at": now(), "algorithm": ALGORITHM,
            "key_id": key_id(key), "slots": slots}


def write_manifest(machine: str | None = None) -> dict:
    """Compute this machine's manifest under the estate key (created on first use) and save it."""
    manifest = build_manifest(machine_name(machine), _key(create=True) or "")
    vault._atomic_write(local_manifest_path(), json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


def read_manifest(path: pathlib.Path) -> dict:
    """One manifest, validated; Refused names what is wrong with it, never its content."""
    vault._no_symlinks(path)
    if not path.is_file():
        raise Refused("not a regular file")
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise Refused("larger than a manifest can be")
    try:
        doc = json.loads(vault._read_private(path, private=False))
    except (ValueError, OSError):
        raise Refused("not readable JSON") from None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise Refused(f"not a {SCHEMA} manifest")
    if not MACHINE.fullmatch(str(doc.get("machine") or "")) or credential_shape.shaped(doc.get("machine")):
        raise Refused("its machine name is not one plain identifier")
    if _parse_time(doc.get("generated_at")) is None:
        raise Refused("its generated_at is not an ISO-8601 time")
    if not isinstance(doc.get("key_id"), str) or not isinstance(doc.get("slots"), dict):
        raise Refused("it has no key_id or no slots")
    for name, row in doc["slots"].items():
        if not SLOT_KEY.fullmatch(str(name)) or not isinstance(row, dict) \
                or not DIGEST_SHAPE.fullmatch(str(row.get("digest") or "")):
            raise Refused("a slot row is malformed")
    return doc


def read_peers(folder: pathlib.Path) -> tuple[list[dict], list[dict]]:
    """(valid peer manifests, unreadable files as `{file, reason}`), in file order."""
    good: list[dict] = []
    bad: list[dict] = []
    if not folder.is_dir():
        return good, bad
    for path in sorted(folder.glob("*.json")):
        try:
            good.append(read_manifest(path))
        except Refused as exc:
            bad.append({"file": path.name, "reason": str(exc)})
    return good, bad


# --- the view -------------------------------------------------------------------------------

def compare(local: dict, peers: list[dict], *, at: datetime.datetime | None = None,
            max_age_hours: float = MAX_AGE_HOURS, unreadable: list[dict] | None = None) -> dict:
    """The view: per slot and peer one of the five states. Pure: no file, no key, no value."""
    at = at or datetime.datetime.now(datetime.timezone.utc)
    here = local["machine"]
    machines: list[dict] = []
    degraded: list[dict] = []
    reachable: list[dict] = []
    chosen: dict[str, dict] = {}
    seen = {here}
    for peer in sorted(peers, key=lambda p: p["machine"]):
        name = peer["machine"]
        if name in seen:
            degraded.append({"source": "peers", "code": "duplicate-machine",
                             "reason": f"two manifests name the machine {name}; the first is used"})
            continue
        seen.add(name)
        chosen[name] = peer
        answered = _parse_time(peer["generated_at"])
        row = {"machine": name, "last_answer": peer["generated_at"]}
        if not hmac.compare_digest(str(peer.get("key_id")), str(local.get("key_id"))):
            row.update(state=UNREACHABLE, reason="its manifest was made under another estate key")
        elif answered is None or (at - answered).total_seconds() > max_age_hours * 3600:
            row.update(state=UNREACHABLE,
                       reason=f"its last answer is older than {max_age_hours:g} hours")
        else:
            row["state"] = "reachable"
            reachable.append(peer)
        machines.append(row)
    for bad in unreadable or []:
        degraded.append({"source": "peers", "code": "unreadable-manifest",
                         "reason": f"{bad['file']}: {bad['reason']}"})
    unreachable = [m for m in machines if m["state"] == UNREACHABLE]
    lslots = local.get("slots") or {}
    names = set(lslots)
    for peer in chosen.values():
        names |= set(peer["slots"])
    rows = []
    for name in sorted(names):
        mine = lslots.get(name)
        cells: dict[str, dict] = {}
        holders = [here] if mine else []
        for m in machines:
            theirs = chosen[m["machine"]]["slots"].get(name)
            if m["state"] == UNREACHABLE:
                cells[m["machine"]] = {"state": UNREACHABLE, "last_answer": m["last_answer"]}
                continue
            if theirs:
                holders.append(m["machine"])
            cells[m["machine"]] = _cell(mine, theirs)
        rows.append({"slot": name, "here": bool(mine), "holders": holders, "machines": cells})
    total = len(rows)
    agree = all(c["state"] == SAME or (c["state"] == DIFFERS and c.get("on_purpose"))
                for r in rows for c in r["machines"].values())
    all_match = bool(reachable) and not unreachable and not degraded and agree
    newest = max([local["generated_at"], *(p["generated_at"] for p in reachable)])
    if all_match:
        summary = (f"All {total} slot(s) match on {len(reachable) + 1} machines "
                   f"({here} and {', '.join(sorted(p['machine'] for p in reachable))}; newest answer {newest}).")
    elif not machines:
        summary = (f"{total} slot(s) here and no peer manifest to compare with: nothing is known "
                   f"about other machines.")
    else:
        counts: dict[str, int] = {}
        for r in rows:
            for c in r["machines"].values():
                counts[c["state"]] = counts.get(c["state"], 0) + 1
        summary = (f"{total} slot(s) across {len(machines) + 1} machines: "
                   + ", ".join(f"{counts.get(s, 0)} {s}" for s in (SAME, DIFFERS, ONLY_HERE, MISSING, UNREACHABLE))
                   + (f"; {len(unreachable)} machine(s) unreachable" if unreachable else "") + ".")
    return {"schema": "vault-digests-view/1", "machine": here, "generated_at": local["generated_at"],
            "machines": machines, "slots": rows, "all_match": all_match, "summary": summary,
            "degraded": degraded}


def _cell(mine: dict | None, theirs: dict | None) -> dict:
    if mine and not theirs:
        return {"state": ONLY_HERE}
    if theirs and not mine:
        return {"state": MISSING}
    if hmac.compare_digest(mine["digest"], theirs["digest"]):
        return {"state": SAME}
    cell: dict = {"state": DIFFERS}
    mark = mine.get("on_purpose") or theirs.get("on_purpose")
    if isinstance(mark, dict):
        cell["on_purpose"] = {"why": str(mark.get("why") or ""), "by": "here" if mine.get("on_purpose") else "peer"}
        return cell
    # A ROTATION MARKS THE OTHER COPIES STALE: the side rotated later holds the current value.
    ours, other = mine.get("rotated_at") or "", theirs.get("rotated_at") or ""
    if ours > other:
        cell["stale"] = {"where": "peer", "rotated_here_at": ours}
    elif other > ours:
        cell["stale"] = {"where": "here", "rotated_there_at": other}
    return cell


def holders_index(machine: str | None = None) -> tuple[dict[str, list[str]], list[dict]]:
    """({`project/env/NAME`: [machines holding it]}, degraded) from manifests alone.

    `observatory_credentials` calls this: it reads no value and no key, only the manifests this
    machine and its peers wrote. This machine is not listed here; the caller adds it for the
    slots it holds. A stale or foreign-key peer is left out and named in `degraded`."""
    index: dict[str, list[str]] = {}
    degraded: list[dict] = []
    try:
        folder = peers_dir()
        peers, bad = read_peers(folder)
    except (Refused, OSError, vault.VaultBoundaryError) as exc:
        return index, [{"source": "vault-digests", "code": "peers-unreadable",
                        "reason": f"the peers folder could not be read: {type(exc).__name__}"}]
    local_key_id = None
    try:
        local = read_manifest(local_manifest_path()) if local_manifest_path().is_file() else None
        local_key_id = local.get("key_id") if local else None
    except (Refused, OSError, vault.VaultBoundaryError):
        local = None
    here = machine or (local or {}).get("machine") or "this-machine"
    view = compare({"machine": here, "generated_at": now(), "key_id": local_key_id or "", "slots": {}},
                   peers, unreadable=bad) if peers or bad else None
    if view:
        for m in view["machines"]:
            if m["state"] == UNREACHABLE and local_key_id:
                degraded.append({"source": "vault-digests", "code": "peer-unreachable",
                                 "reason": f"{m['machine']}: {m['reason']} (last answer {m['last_answer']}); "
                                           f"the slots it holds are not named"})
        if not local_key_id and peers:
            degraded.append({"source": "vault-digests", "code": "no-local-manifest",
                             "reason": f"this machine has no digest manifest yet, so peers cannot be checked "
                                       f"against its estate key; run `{CMD} write`"})
        degraded.extend(view["degraded"])
        used: set[str] = set()
        for peer in sorted(peers, key=lambda p: p["machine"]):
            ok = any(m["machine"] == peer["machine"] and m["state"] != UNREACHABLE for m in view["machines"])
            if ok and local_key_id and peer["machine"] not in used:
                used.add(peer["machine"])
                for name in peer["slots"]:
                    index.setdefault(name, []).append(peer["machine"])
    return index, degraded


# --- the marks ------------------------------------------------------------------------------

def set_mark(project: str, env: str, name: str, why: str | None) -> None:
    """Mark a slot as different on purpose on this machine (`why`), or clear the mark (None)."""
    with vault._mutation_lock():
        slot = vault._slot(project, env, name)
        if not slot.is_file():
            raise Refused(f"nothing at {project}/{env}/{name} on this machine to mark")
        meta = vault._read_meta(slot)
        if why is None:
            if "differs_on_purpose" not in meta:
                raise Refused(f"{project}/{env}/{name} carries no mark")
            meta.pop("differs_on_purpose")
            event = "unmark-differs"
        else:
            meta["differs_on_purpose"] = {"why": why, "at": now()}
            event = "mark-differs"
        vault._atomic_write(vault._meta_path(slot), json.dumps(meta, indent=1), 0o600)
        vault.journal(event, f"{project}/{env}/{name}", tool="vault_digests.py",
                      **({"why": why} if why else {}))


# --- the CLI --------------------------------------------------------------------------------

def render(view: dict) -> str:
    if view["all_match"]:
        return view["summary"]
    lines = [view["summary"]]
    for m in view["machines"]:
        if m["state"] == UNREACHABLE:
            lines.append(f"  {m['machine']}: unreachable, {m['reason']}; last answer {m['last_answer']}")
    for d in view["degraded"]:
        lines.append(f"  degraded: {d['reason']}")
    for row in view["slots"]:
        if all(c["state"] == SAME for c in row["machines"].values()) and row["machines"]:
            continue
        parts = []
        for machine, c in row["machines"].items():
            text = c["state"]
            if c.get("on_purpose"):
                text += f" (on purpose: {c['on_purpose']['why']})"
            elif c.get("stale"):
                text += (f" (stale on {machine}: rotated here {c['stale']['rotated_here_at']})"
                         if c["stale"]["where"] == "peer" else
                         f" (stale here: rotated on {machine} {c['stale']['rotated_there_at']})")
            elif c["state"] == UNREACHABLE:
                text += f" (last answer {c['last_answer']})"
            parts.append(f"{machine}: {text}")
        lines.append(f"  {row['slot']}  " + ("; ".join(parts) if parts else "only here, no peer"))
    return "\n".join(lines)


def cmd_view(args) -> int:
    local = write_manifest(args.machine)
    peers, bad = read_peers(peers_dir(args.peers))
    view = compare(local, peers, max_age_hours=args.max_age_hours, unreadable=bad)
    print(json.dumps(view, indent=1) if args.json else render(view))
    return 0 if view["all_match"] else 1


def cmd_write(args) -> int:
    manifest = write_manifest(args.machine)
    print(f"wrote {local_manifest_path()}: {len(manifest['slots'])} slot digest(s) for "
          f"{manifest['machine']} under estate key {manifest['key_id']}; no value is in it")
    return 0


def cmd_mark(args) -> int:
    why = (args.why or "").strip()
    if len(why) < 8:
        raise Refused("--why must say why the copies differ on purpose (at least 8 characters)")
    set_mark(args.project, args.env, args.name, vault._note("--why", why))
    print(f"marked {args.project}/{args.env}/{args.name} as different on purpose; "
          f"`{CMD} write` publishes the mark to the manifest")
    return 0


def cmd_unmark(args) -> int:
    set_mark(args.project, args.env, args.name, None)
    print(f"cleared the mark on {args.project}/{args.env}/{args.name}")
    return 0


def cmd_key(args) -> int:
    if args.action == "status":
        found = _read_key()
        print(f"estate key {key_id(found)} at {key_path()} (value hidden)" if found else
              f"no estate key yet; `{CMD} write` creates one, `{CMD} key import` reads the estate's on stdin")
        return 0
    if args.action == "import":
        if sys.stdin.isatty():
            raise Refused("the key must arrive on stdin from a protected file, never in an argument")
        text = sys.stdin.read().strip()
        if not KEY_SHAPE.fullmatch(text):
            raise Refused("stdin did not hold 64 lowercase hexadecimal characters")
        with vault._mutation_lock():
            found = _read_key()
            if found and not hmac.compare_digest(found, text) and not args.replace:
                raise Refused("this vault already holds another estate key; --replace changes every digest "
                              "this machine publishes")
            vault._atomic_write(key_path(), text + "\n")
            vault.journal("estate-key-import", "estate/digest-key", tool="vault_digests.py",
                          key_id=key_id(text), replaced=bool(found and found != text))
        print(f"estate key {key_id(text)} imported (value hidden); run `{CMD} write`")
        return 0
    if not args.file:
        raise Refused("key export needs FILE: a new file it writes owner-only")
    # The folder is resolved (macOS's /tmp and /var are links); the file itself may not be a link.
    given = pathlib.Path(args.file).expanduser()
    target = given.parent.resolve() / given.name
    vault._no_symlinks(target)
    if target.exists():
        raise Refused("FILE already exists; export never overwrites")
    found = _key(create=True) or ""
    vault._atomic_write(target, found + "\n")
    vault.journal("estate-key-export", "estate/digest-key", tool="vault_digests.py",
                  key_id=key_id(found), into=str(target))
    print(f"estate key {key_id(found)} written to {target} (600, value hidden); import it on the other "
          f"machine with `{CMD} key import < FILE`, then delete the file")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=CMD, description="Vault slots across this operator's machines, "
                                "compared by keyed digests under the estate key, never by values.")
    sub = p.add_subparsers(dest="command")   # no command: view
    v = sub.add_parser("view", help="every slot per machine: same, differs, only here, missing, unreachable")
    w = sub.add_parser("write", help="compute and save this machine's digest manifest")
    for s in (v, w):
        s.add_argument("--machine", help="this machine's name in manifests (default: the host name)")
    v.add_argument("--json", action="store_true")
    v.add_argument("--peers", help="the folder of peer manifests (default: <state>/vault-digests/peers)")
    v.add_argument("--max-age-hours", type=float, default=MAX_AGE_HOURS,
                   help="a peer answer older than this is unreachable")
    m = sub.add_parser("mark", help="mark a slot as different on purpose on this machine")
    vault._slot_args(m)
    m.add_argument("--why", required=True)
    u = sub.add_parser("unmark", help="clear that mark")
    vault._slot_args(u)
    k = sub.add_parser("key", help="the estate key: status, import (stdin), export FILE")
    k.add_argument("action", choices=("status", "import", "export"))
    k.add_argument("file", nargs="?")
    k.add_argument("--replace", action="store_true", help="import over a different key")
    return p


def arguments(argv: list[str]) -> list[str]:
    """No command, or options alone, mean `view`."""
    if not argv or argv[0] not in ("view", "write", "mark", "unmark", "key", "-h", "--help"):
        return ["view", *argv]
    return list(argv)


def main(argv: list[str]) -> int:
    args = parser().parse_args(arguments(argv))
    try:
        if args.command in ("mark", "unmark"):
            vault.validate_names(args.project, args.env, args.name)
        return {"view": cmd_view, "write": cmd_write, "mark": cmd_mark, "unmark": cmd_unmark,
                "key": cmd_key}[args.command](args)
    except (Refused, vault.VaultBoundaryError) as exc:
        print(f"vault-digests: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("vault-digests: the private filesystem refused the operation", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
