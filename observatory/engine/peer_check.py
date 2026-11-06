"""The servers' peer check: on a host with several OS accounts, refuse another account's
connection before reading it (docs/design/ACCESS.md rule 10; OBS-355 part 1).

Loopback is one per machine. The local server (`tools/serverd.py`), memory over HTTP
(`mcp/http_service.py`) and the keyserver (`tools/keyserver.py`) bind 127.0.0.1, and on a host
with several accounts every other account's processes reach them: measured before this check,
the dashboard pages and `/health` answered another account in full. So each of them asks, for
every accepted connection and before a byte of its request is read, which account the peer is
(`peer_owner.peer`), and answers anything but this account with

    403 {"error":"peer-foreign-account"}   another account's process, or the system's
    403 {"error":"peer-unverified"}        the owner could not be read

with `Connection: close`, then closes. Every path, `/.well-known/fabric-service` and the
dashboard pages included. The relay is exempt: its admission is the estate record's signatures,
and behind a TLS terminator every peer is the terminator's process, which may run as another
account or root.

WHEN IT IS ACTIVE. When `host_accounts.count()` is not 1 (an unreadable count is "several"), or
when `config/local.json` sets `{"loopback": {"peerCheck": "always"}}`. `auto` is the default and
there is no `off`: a value other than `auto` or `always` reads as `always`, the safe reading.
Both are read at the first connection and every ACTIVE_TTL seconds after, so an account added
later turns the check on without a restart. On a one-account host the check costs a clock
comparison per connection: no table read, no subprocess.

WHAT IS READ. Nothing of the request: no request line, no header, no body. Bytes the peer had
already sent are discarded unread after the answer (at most DRAIN_BYTES, for DRAIN_SECONDS), so
the close does not reset the connection before the peer reads its 403 (a "lingering close").
Memory over HTTP is the exception the ASGI server imposes: uvicorn has parsed the request line
and headers before the door runs, and the door uses none of them, reads no body and resolves no
bearer.

WHAT IS SAID. One JSON line on the server's stderr per refused connection, at most one per peer
uid and code per LOG_EVERY seconds: `{"event": "peer.refused", "code": ..., "peerUid": ...,
"method": ...}`. Never a header (none was read), a token or a path. The counters (`snapshot()`)
go into the local server's receipt `store/raw/serverd.json` under `peer`, which is excluded from
the receipt's change comparison, so a flood of refusals never causes a receipt write; they are
written with the next keepalive.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import socket
import sys
import threading
import time

import host_accounts
import peer_owner

FOREIGN = "peer-foreign-account"
UNVERIFIED = "peer-unverified"
CODES = (FOREIGN, UNVERIFIED)
SETTINGS = ("auto", "always")

ACTIVE_TTL = 600.0
LOG_EVERY = 60.0
DRAIN_BYTES = 64 * 1024
DRAIN_SECONDS = 0.5


def _now_z() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _config_dir() -> pathlib.Path:
    import paths
    return pathlib.Path(paths.CONFIG)


def read_setting(config_dir: pathlib.Path) -> tuple[str, str | None]:
    """(`auto` or `always`, a problem or None) from `config/local.json`'s `loopback.peerCheck`."""
    try:
        doc = json.loads((config_dir / "local.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "auto", None
    except (OSError, ValueError) as exc:
        return "auto", f"local.json unreadable: {type(exc).__name__}"
    value = ((doc or {}).get("loopback") or {}).get("peerCheck", "auto") if isinstance(doc, dict) else "auto"
    if value in SETTINGS:
        return value, None
    return "always", f"loopback.peerCheck {value!r} is not auto or always; read as always"


def code_for(owner: peer_owner.Owner) -> str | None:
    """None for this account's peer, else the refusal code."""
    if owner.kind == peer_owner.THIS_ACCOUNT:
        return None
    if owner.kind in (peer_owner.ANOTHER_ACCOUNT, peer_owner.SYSTEM):
        return FOREIGN
    return UNVERIFIED


class Gate:
    """One per server process. Every dependency is injectable for the tests."""

    def __init__(self, *, accounts=None, config_dir=None, peer=None, peer_at=None, method=None,
                 log=None, clock=time.monotonic) -> None:
        self._accounts = accounts or host_accounts.count
        self._config_dir = config_dir or _config_dir
        self._peer = peer or peer_owner.peer
        self._peer_at = peer_at or peer_owner.peer_at
        self._method = method or peer_owner.method
        self._log = log or self._stderr
        self._clock = clock
        self._lock = threading.Lock()
        self._decided: tuple | None = None            # (at, active, accounts, setting, problem)
        self._logged: dict[tuple, float] = {}
        self._counts = {code: 0 for code in CODES}
        self._since = _now_z()
        self._last_method: str | None = None

    @staticmethod
    def _stderr(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    def _decide(self) -> tuple:
        now = self._clock()
        with self._lock:
            if self._decided is not None and now - self._decided[0] < ACTIVE_TTL:
                return self._decided
        accounts = self._accounts()
        try:
            setting, problem = read_setting(self._config_dir())
        except Exception as exc:  # noqa: BLE001 - an unreadable setting never turns the check off
            setting, problem = "auto", f"setting unreadable: {type(exc).__name__}"
        active = setting == "always" or accounts != 1
        with self._lock:
            self._decided = (now, active, accounts, setting, problem)
            return self._decided

    def active(self) -> bool:
        return self._decide()[1]

    def _judge(self, owner: peer_owner.Owner) -> str | None:
        self._last_method = owner.method
        code = code_for(owner)
        if code is None:
            return None
        now = self._clock()
        key = (owner.uid, code)
        with self._lock:
            self._counts[code] += 1
            due = now - self._logged.get(key, -LOG_EVERY - 1) >= LOG_EVERY
            if due:
                self._logged[key] = now
        if due:
            try:
                self._log(json.dumps({"event": "peer.refused", "code": code, "peerUid": owner.uid,
                                      "method": owner.method}))
            except Exception:  # noqa: BLE001 - a full disk never turns a refusal into an admission
                pass
        return code

    def check_socket(self, sock: socket.socket) -> str | None:
        """For an accepted socket: None to serve it, or the refusal code. Reads nothing from it."""
        if not self.active():
            return None
        return self._judge(self._peer(sock))

    def check_addresses(self, server, client) -> str | None:
        """For a server that has the two addresses only (ASGI's `scope["server"]` and `["client"]`)."""
        if not self.active():
            return None
        if not server or not client:
            return self._judge(peer_owner.Owner(peer_owner.UNKNOWN, method="none", reason="no-address"))
        return self._judge(self._peer_at(tuple(server), tuple(client)))

    def counters(self) -> dict:
        with self._lock:
            return {**self._counts, "since": self._since}

    def snapshot(self) -> dict:
        """What the receipt and doctor show: the setting, whether it is active and why, the counters."""
        _at, active, accounts, setting, problem = self._decide()
        doc = {"setting": setting, "active": active, "accountsOnHost": accounts,
               "method": (self._last_method or self._method()) if active else None,
               "refused": self.counters()}
        if problem:
            doc["problem"] = problem
        return doc


def response(code: str) -> bytes:
    """The whole answer, written raw: no request was read, so there is no request to answer in kind."""
    body = json.dumps({"error": code}, separators=(",", ":")).encode()
    return (b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Cache-Control: no-store\r\nConnection: close\r\n\r\n" + body)


def refuse(sock: socket.socket, code: str) -> None:
    """Write the 403, end the sending side, discard what the peer already sent unread, so the
    close does not reset the answer away, and leave the close to the server's own shutdown."""
    try:
        sock.sendall(response(code))
        sock.shutdown(socket.SHUT_WR)
        deadline = time.monotonic() + DRAIN_SECONDS
        left = DRAIN_BYTES
        while left > 0:
            wait = deadline - time.monotonic()
            if wait <= 0:
                break
            sock.settimeout(wait)
            chunk = sock.recv(min(left, 8192))
            if not chunk:
                break
            left -= len(chunk)
    except OSError:
        pass


#: The gate of this process: the local server, memory over HTTP and the keyserver each run one.
GATE = Gate()


def status(base: pathlib.Path | None = None) -> dict:
    """Doctor's `loopback` block: this process's view of the setting and the account count, and the
    local server's refusal counters from its receipt (`store/raw/serverd.json`, key `peer`)."""
    gate = GATE if base is None else Gate(config_dir=lambda: pathlib.Path(base) / "config")
    doc = gate.snapshot()
    doc.pop("refused", None)
    try:
        import paths
        receipt = json.loads((pathlib.Path(paths.SCRATCH) / "serverd.json").read_text(encoding="utf-8"))
        refused = ((receipt or {}).get("peer") or {}).get("refused")
    except (OSError, ValueError, ImportError):
        refused = None
    doc["serverRefused"] = refused
    return doc
