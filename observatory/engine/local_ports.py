"""local_ports: the port of each Observatory listener for THIS OS account (OBS-354).

Loopback is one per host, not one per account: two accounts that both run Observatory on its historical
ports collide, and the second account's clients reach the first account's server. So each account records
the ports it listens on, and tells the other accounts which ports it took.

THE RECORD. `config/local.json` (OBS-353), key `ports`: `{scheme, server, memory_http, relay, keyserver,
allocatedAt}`. `scheme` is `legacy` (47311, 47313, 47317, 7717), `allocated` (a block of the range below)
or `chosen` (a person named a port). The record holds only while local.json's place is this place.

THE HOST CLAIM. One world-readable file per account and product in the host's shared sticky folder
(`/Users/Shared` on macOS, `/var/tmp` on Linux, `%ProgramData%\\PassionCode\\port-claims` on Windows):
`.passioncode-ports.project-observatory.<uid>.json` = `{schema, service, account, ports, updatedAt}`. It
holds no secret, path or place. A reader ignores a link, a file over 16 KB, another schema, a file whose
owner is not the account its name gives, and a uid that no longer exists. A planted claim can only make
the allocator skip a block; clients read their own owner-only record, never a claim.

THE ALLOCATION. One account on the host: the legacy ports, as before. Several (or an unknown count): an
existing install keeps its legacy ports while nobody else holds or claims them; otherwise a block of 8 from
47400 to 47999 (75 blocks, starting at uid mod 75): server = B, memory_http = B + 2, relay = B + 6,
keyserver = B + 7, the offsets of 47311, 47313 and 47317. A block is free when none of its four ports is
claimed by another account, declared by a descriptor of this account, bound, or listening. Two accounts
allocating at once both write their claim, wait, read again, and the later one moves on.

THE CLIENTS. `port(name)`: OBSERVATORY_SERVER_PORT (server only), then the record, then the server's own
receipt (server only), then the legacy default on a one-account host; on a shared host with no record it
raises `port-unallocated` rather than guess another account's listener.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import configuration as config
import host_accounts

SERVICE = "project-observatory"
CLAIM_SCHEMA = "passioncode-port-claim/1"
CLAIM_PREFIX = f".passioncode-ports.{SERVICE}."
CLAIM_MAX_BYTES = 16 * 1024
LEGACY = {"server": 47311, "memory_http": 47313, "relay": 47317, "keyserver": 7717}
NAMES = tuple(LEGACY)
OFFSETS = {"server": 0, "memory_http": 2, "relay": 6, "keyserver": 7}
FIRST, LAST, BLOCK = 47400, 47999, 8
BLOCKS = (LAST - FIRST + 1) // BLOCK
SCHEMES = ("legacy", "allocated", "chosen")
RACE_WAIT = 0.2
CLAIMS_ENV = "OBSERVATORY_PORT_CLAIMS_DIR"


class PortError(RuntimeError):
    """A typed refusal: `code` from the spec's table, `step` the command that lifts it."""

    def __init__(self, code: str, message: str, step: str | None = None):
        super().__init__(message)
        self.code, self.step = code, step

    def answer(self) -> dict:
        return {"ok": False, "code": self.code, "message": str(self), **({"step": self.step} if self.step else {})}


# --- what the host says (each a module function, so a test replaces it) -----------------------------

def accounts() -> int | None:
    """People's accounts on the host. A sandbox is a one-account host unless it says otherwise
    (OBSERVATORY_HOST_ACCOUNTS), so a suite's expectations never depend on the CI runner's accounts."""
    if _testing():
        told = os.environ.get("OBSERVATORY_HOST_ACCOUNTS", "1")
        return int(told) if told.isdigit() else None
    return host_accounts.count()


def account() -> str:
    return host_accounts.this_account()


def account_exists(name: str) -> bool:
    return host_accounts.uid_exists(name)


def bind_free(port: int) -> bool:
    """Whether 127.0.0.1:<port> binds now, without SO_REUSEADDR; the probe socket is closed at once."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def listener_kind(port: int) -> str:
    """Who listens on the port: peer_owner's `this-account`, `another-account`, `system`, `unknown` or `none`."""
    try:
        import peer_owner
        return peer_owner.listener(port).kind
    except Exception:  # noqa: BLE001 - an unreadable table is `unknown`, never `none`
        return "unknown"


def declared_ports() -> set[int]:
    """Ports the descriptors of this account's other services claim (their `origin`)."""
    out: set[int] = set()
    try:
        import fabric_service
        import service_identity
        mine = service_identity.instance()
        for _path, doc in fabric_service.read_descriptors():
            if doc.get("id") == SERVICE and doc.get("instance") == mine:
                continue
            port = _origin_port(doc.get("origin"))
            if port:
                out.add(port)
    except Exception:  # noqa: BLE001 - an unreadable folder adds nothing; the bind probe still runs
        pass
    return out


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _origin_port(origin) -> int | None:
    try:
        return urlparse(str(origin)).port if origin else None
    except ValueError:
        return None


# --- the host claim ----------------------------------------------------------------------------------

def _testing() -> bool:
    return os.environ.get("OBSERVATORY_SYSTEM_SETUP") == "0" or \
        os.environ.get("OBSERVATORY_MACHINE_ID_FOR_TESTS") == "1"


def claims_dir() -> Path:
    """The host's shared folder for claims. A sandbox or a test may point it elsewhere; a variable carried in a
    shell profile alone never does (the rule `machine_identity.machine_id` applies to the machine id)."""
    if _testing():
        # A sandbox never writes into the host's shared folder: its own claims live in its workspace.
        override = os.environ.get(CLAIMS_ENV)
        return Path(override) if override else config.home() / "store" / "port-claims"
    if sys.platform == "darwin":
        return Path("/Users/Shared")
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "PassionCode" / "port-claims"
    return Path("/var/tmp")


def claim_path(name: str | None = None, folder: Path | None = None) -> Path:
    return (folder or claims_dir()) / f"{CLAIM_PREFIX}{name if name is not None else account()}.json"


def read_claims(folder: Path | None = None) -> tuple[list[dict], bool]:
    """Every OTHER account's valid claim, and whether the folder could be read."""
    folder = folder or claims_dir()
    me = account()
    try:
        names = [n for n in os.listdir(folder) if n.startswith(CLAIM_PREFIX) and n.endswith(".json")]
    except FileNotFoundError:
        return [], True                                 # nobody has claimed yet (a sandbox's folder, first use)
    except OSError:
        return [], False
    out = []
    for name in sorted(names):
        who = name[len(CLAIM_PREFIX):-len(".json")]
        if not who or who == me:
            continue
        doc = _read_claim(folder / name, who)
        if doc is not None:
            out.append(doc)
    return out, True


def _read_claim(path: Path, who: str) -> dict | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    import stat
    if not stat.S_ISREG(st.st_mode) or st.st_size > CLAIM_MAX_BYTES:
        return None
    if hasattr(os, "getuid") and (not who.isdigit() or st.st_uid != int(who)):
        return None                                    # only its owner (or root) can have written it
    if not account_exists(who):
        return None
    try:
        with open(path, "rb") as fh:
            doc = json.loads(fh.read(CLAIM_MAX_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not (isinstance(doc, dict) and doc.get("schema") == CLAIM_SCHEMA and doc.get("service") == SERVICE
            and str(doc.get("account")) == who and isinstance(doc.get("ports"), list)):
        return None
    ports = [p for p in doc["ports"] if isinstance(p, int) and not isinstance(p, bool) and 0 < p < 65536]
    return {"account": who, "ports": ports, "updatedAt": str(doc.get("updatedAt") or "")}


def write_claim(ports: dict, folder: Path | None = None) -> str:
    """Write (or refresh) this account's claim. Answers the time it carries, or '' when it cannot be written."""
    folder = folder or claims_dir()
    stamp = now()
    doc = {"schema": CLAIM_SCHEMA, "service": SERVICE, "account": account(),
           "ports": sorted({int(ports[n]) for n in NAMES}), "updatedAt": stamp}
    target = claim_path(folder=folder)
    try:
        if os.name == "nt" or _testing():
            folder.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            return ""
        tmp = folder / f"{target.name}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        if hasattr(os, "chmod"):
            os.chmod(tmp, 0o644)
        os.replace(tmp, target)
        return stamp
    except OSError:
        return ""


def remove_claim(folder: Path | None = None) -> bool:
    try:
        claim_path(folder=folder).unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


# --- the record --------------------------------------------------------------------------------------

def _valid(doc) -> dict | None:
    if not isinstance(doc, dict) or doc.get("scheme") not in SCHEMES:
        return None
    if not all(isinstance(doc.get(n), int) and not isinstance(doc.get(n), bool) and 0 < doc[n] < 65536
               for n in NAMES):
        return None
    return doc


def record(base: Path | None = None) -> dict | None:
    """The ports this account recorded, or None (absent, malformed, or recorded at another place)."""
    base = base or config.home()
    return _valid((config.local(base)["settings"].get("ports") or {}).get("value"))


def _write_record(base: Path, ports: dict, scheme: str) -> dict:
    from workspace import lock
    entry = {"scheme": scheme, **{n: int(ports[n]) for n in NAMES}, "allocatedAt": now()}
    with lock(base):
        config.write_local(base, {"ports": entry})
    return entry


def current_server_port(base: Path | None = None) -> int | None:
    """The port this account's server uses today: the installed job's argv, else the descriptor's origin,
    else the running server's receipt."""
    base = base or config.home()
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import install_launchd
        import plistlib
        plist = install_launchd.plist_path(install_launchd.instance_label("server", base))
        if plist.is_file() and not plist.is_symlink():
            args = plistlib.loads(plist.read_bytes()).get("ProgramArguments") or []
            for i, a in enumerate(args[:-1]):
                if a == "--port" and str(args[i + 1]).isdigit():
                    return int(args[i + 1])
            return LEGACY["server"]
    except Exception:  # noqa: BLE001 - an unreadable job falls through to the descriptor
        pass
    try:
        import fabric_service
        import service_identity
        mine = service_identity.instance(base)
        for _path, doc in fabric_service.read_descriptors():
            if doc.get("id") == SERVICE and doc.get("instance") == mine:
                port = _origin_port(doc.get("origin"))
                if port:
                    return port
    except Exception:  # noqa: BLE001
        pass
    return receipt_port(base)


def receipt_port(base: Path | None = None) -> int | None:
    base = base or config.home()
    for file in (base / "store" / "raw" / "serverd.json",):
        try:
            port = json.loads(file.read_text(encoding="utf-8")).get("port")
            if isinstance(port, int) and 0 < port < 65536:
                return port
        except (OSError, ValueError, AttributeError):
            continue
    return None


# --- allocation --------------------------------------------------------------------------------------

def _block(start: int) -> dict:
    return {n: start + OFFSETS[n] for n in NAMES}


def _taken(port: int, claimed: set[int], declared: set[int]) -> str | None:
    """Why a port cannot be ours, or None."""
    if port in claimed:
        return "claimed"
    if port in declared:
        return "declared"
    kind = listener_kind(port)
    if kind == "this-account":
        return None
    if kind != "none":
        return "listening"
    if not bind_free(port):
        return "bound"
    return None


def _start_block() -> int:
    who = account()
    digits = who.rsplit("-", 1)[-1]
    return int(digits) % BLOCKS if digits.isdigit() else 0


def _choose(claims: list[dict], skip: set[int]) -> dict | None:
    claimed = {p for c in claims for p in c["ports"]}
    declared = declared_ports()
    k0 = _start_block()
    for i in range(BLOCKS):
        k = (k0 + i) % BLOCKS
        start = FIRST + k * BLOCK
        if start in skip:
            continue
        ports = _block(start)
        if all(_taken(p, claimed, declared) is None for p in ports.values()):
            return ports
    return None


def _legacy_with(server: int | None) -> tuple[dict, str]:
    ports = dict(LEGACY)
    if server and server != LEGACY["server"]:
        ports["server"] = server
        return ports, "chosen"
    return ports, "legacy"


def allocate(base: Path | None = None, *, reinstall: bool = False, folder: Path | None = None) -> dict:
    """Record this account's ports (keeping a valid record) and write the host claim. See the module doc."""
    base = base or config.home()
    have = record(base)
    claims, readable = read_claims(folder)
    notes = [] if readable else [{"code": "claims-folder-unreadable", "folder": str(folder or claims_dir())}]
    if have:
        stamp = write_claim(have, folder)
        return _answer(have, have["scheme"], stamp, notes, kept=True)
    old_server = current_server_port(base)
    n = accounts()
    if n == 1:
        ports, scheme = _legacy_with(old_server)
        entry = _write_record(base, ports, scheme)
        return _answer(entry, scheme, write_claim(entry, folder), notes)
    if old_server:
        ports, scheme = _legacy_with(old_server)
        claimed = {p for c in claims for p in c["ports"]}
        if all(p not in claimed and listener_kind(p) in ("this-account", "none") for p in ports.values()):
            entry = _write_record(base, ports, scheme)
            return _answer(entry, scheme, write_claim(entry, folder), notes)
    skip: set[int] = set()
    for _ in range(BLOCKS):
        ports = _choose(claims, skip)
        if ports is None:
            raise PortError("port-range-exhausted", f"no free block of {BLOCK} ports in {FIRST}-{LAST}",
                            "project-observatory full ports set server PORT")
        stamp = write_claim(ports, folder)
        if stamp:
            time.sleep(RACE_WAIT)
            claims, _ = read_claims(folder)
            mine = (stamp, account())
            if any(set(ports.values()) & set(c["ports"]) and (c["updatedAt"], c["account"]) < mine for c in claims):
                remove_claim(folder)                    # the other account wrote first; take the next block
                skip.add(ports["server"])
                continue
        entry = _write_record(base, ports, "allocated")
        answer = _answer(entry, "allocated", stamp, notes)
        if reinstall:
            answer["reinstall"] = _reinstall(base, entry["server"], old_server)
        return answer
    raise PortError("port-range-exhausted", f"no free block of {BLOCK} ports in {FIRST}-{LAST}",
                    "project-observatory full ports set server PORT")


def _answer(entry: dict, scheme: str, stamp: str, notes: list, kept: bool = False) -> dict:
    return {"ok": True, "scheme": scheme, "ports": {n: entry[n] for n in NAMES}, "kept": kept,
            "claim": "written" if stamp else "not-written", "notes": notes}


def _reinstall(base: Path, server: int, old: int | None) -> dict:
    """Move this account's installed server job to its new port (serverd rewrites the job, the descriptor and
    restarts it). Nothing is installed: nothing to move."""
    if old is None or old == server:
        return {"status": "not-needed"}
    import subprocess
    tool = Path(__file__).resolve().parent / "tools" / "serverd.py"
    p = subprocess.run([sys.executable, str(tool), "--install", "--port", str(server)],
                       capture_output=True, text=True, timeout=120)
    return {"status": "reinstalled" if p.returncode == 0 else "failed", "exit": p.returncode,
            "detail": (p.stderr or p.stdout).strip()[-300:]}


def resolve(name: str, base: Path | None = None, *, allocate_missing: bool = False) -> int:
    """The port of listener `name` to LISTEN on: the record, allocating one when asked."""
    have = record(base)
    if have:
        return have[name]
    if allocate_missing:
        return allocate(base)["ports"][name]
    return port(name, base)


def port(name: str, base: Path | None = None) -> int:
    """The port a CLIENT of listener `name` uses (module doc, THE CLIENTS)."""
    if name not in LEGACY:
        raise ValueError(f"unknown listener {name!r}")
    if name == "server" and os.environ.get("OBSERVATORY_SERVER_PORT", "").isdigit():
        return int(os.environ["OBSERVATORY_SERVER_PORT"])
    have = record(base)
    if have:
        return have[name]
    if name == "server":
        seen = receipt_port(base)
        if seen:
            return seen
    if accounts() == 1:
        return LEGACY[name]
    raise PortError("port-unallocated", "several OS accounts share this host and this account has no port record",
                    "project-observatory full ports allocate")


def _source(name: str, have: dict | None, base: Path) -> tuple[int | None, str]:
    if name == "server" and os.environ.get("OBSERVATORY_SERVER_PORT", "").isdigit():
        return int(os.environ["OBSERVATORY_SERVER_PORT"]), "env"
    if have:
        return have[name], "record"
    if name == "server" and receipt_port(base):
        return receipt_port(base), "receipt"
    if accounts() == 1:
        return LEGACY[name], "legacy"
    return None, "unallocated"


def show(base: Path | None = None, folder: Path | None = None) -> dict:
    """Each listener with its port, where the port came from, and who listens there. Reads only."""
    base = base or config.home()
    state = config.local(base)
    have = _valid((state["settings"].get("ports") or {}).get("value"))
    claims, readable = read_claims(folder)
    n = accounts()
    listeners, notes = {}, []
    for name in NAMES:
        value, source = _source(name, have, base)
        owner = listener_kind(value) if value else "none"
        listeners[name] = {"port": value, "source": source, "listening": owner != "none", "owner": owner}
        if value and owner == "another-account" and source in ("record", "env"):
            notes.append({"code": "port-held-by-another-account", "listener": name,
                          "step": "project-observatory full ports allocate --reinstall"})
    if listeners["server"]["source"] == "env" and have and have["server"] != listeners["server"]["port"]:
        notes.append({"code": "port-env-overrides-record", "variable": "OBSERVATORY_SERVER_PORT"})
    if state["place"]["matches"] is False:
        notes.append({"code": "port-record-stale", "step": "project-observatory full ports allocate --reinstall"})
    if not readable:
        notes.append({"code": "claims-folder-unreadable"})
    elif have and not claim_path(folder=folder).is_file():
        notes.append({"code": "claim-missing", "step": "project-observatory full ports allocate"})
    if have and have["scheme"] != "legacy":
        for name, legacy in LEGACY.items():
            if listener_kind(legacy) == "another-account":
                notes.append({"code": "default-port-held-by-another-account", "port": legacy})
                break
    if n != 1 and not have:
        notes.append({"code": "port-unallocated", "step": "project-observatory full ports allocate"})
    return {"accountsOnHost": n, "scheme": have["scheme"] if have else None, "listeners": listeners,
            "place": state["place"], "claims": {"folder": str(folder or claims_dir()), "readable": readable,
                                                "others": len(claims)}, "notes": notes}


def set_port(name: str, value: int, base: Path | None = None, folder: Path | None = None) -> dict:
    """A person's choice for one listener; the others keep their recorded or legacy values."""
    if name not in LEGACY:
        raise PortError("unknown-listener", f"unknown listener {name}; known: {', '.join(NAMES)}")
    if not 0 < value < 65536:
        raise PortError("port-invalid", "a port is a number from 1 to 65535")
    base = base or config.home()
    claims, _ = read_claims(folder)
    if any(value in c["ports"] for c in claims):
        raise PortError("port-claimed-by-another-account", f"another account's claim names {value}",
                        "project-observatory full ports allocate")
    if listener_kind(value) == "another-account":
        raise PortError("port-held-by-another-account", f"another account listens on {value}",
                        "project-observatory full ports allocate --reinstall")
    ports = dict(record(base) or LEGACY)
    ports[name] = value
    entry = _write_record(base, ports, "chosen")
    return _answer(entry, "chosen", write_claim(entry, folder), [])


def release(base: Path | None = None, folder: Path | None = None) -> dict:
    """Remove the claim and the record; refused while this account's server job is installed."""
    base = base or config.home()
    sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
    import install_launchd
    if install_launchd.plist_path(install_launchd.instance_label("server", base)).exists():
        raise PortError("server-installed", "this account's server job is installed and listens on the recorded port",
                        "python3 \"$(project-observatory full-path)/tools/serverd.py\" --uninstall")
    from workspace import lock
    with lock(base):
        doc, _ = config.read_local(base)
        if doc and "ports" in doc:
            import atomic
            doc = {k: v for k, v in doc.items() if k != "ports"}
            atomic.write_json(config.local_path(base), doc, indent=2)
    return {"ok": True, "claim": "removed" if remove_claim(folder) else "not-removed", "record": "removed"}


# --- the command -------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="project-observatory full ports",
                                 description="the port of each Observatory listener for this OS account")
    sub = ap.add_subparsers(dest="action", required=True)
    s = sub.add_parser("show", help="each listener, its port, where it came from and who listens there")
    s.add_argument("--json", action="store_true")
    a = sub.add_parser("allocate", help="record this account's ports and claim them on the host")
    a.add_argument("--reinstall", action="store_true", help="also move an installed server job to its new port")
    a.add_argument("--json", action="store_true")
    c = sub.add_parser("set", help="a person's choice for one listener")
    c.add_argument("name", choices=NAMES)
    c.add_argument("port", type=int)
    c.add_argument("--json", action="store_true")
    r = sub.add_parser("release", help="remove the claim and the record (the server job uninstalled first)")
    r.add_argument("--json", action="store_true")
    return ap


def main(argv: list[str]) -> int:
    a = parser().parse_args(argv)
    try:
        if a.action == "show":
            out = show()
        elif a.action == "allocate":
            out = allocate(reinstall=a.reinstall)
        elif a.action == "set":
            out = set_port(a.name, a.port)
        else:
            out = release()
    except PortError as exc:
        print(json.dumps(exc.answer(), indent=2))
        return 1
    print(json.dumps(out, indent=2))
    return 0
