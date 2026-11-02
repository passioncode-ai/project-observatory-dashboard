#!/usr/bin/env python3
"""Your own relay, `fabric-relay/0.1`, on your always-on machine (OBS-287, the relay project's interim RLY-04), until
the hosted relay runs.

    project-observatory full relay serve [--origin URL] [--host HOST] [--port N] [--data DIR | --memory]
        run the relay in the foreground until Ctrl-C or SIGTERM; logs are JSON lines on stderr
    project-observatory full relay enable --origin URL [--host HOST] [--port N]
        run it inside the node's own server from its next start (no service of its own)
    project-observatory full relay disable
        stop running it there from the server's next start
    project-observatory full relay status [--json]
        whether it is enabled, whether it answers, and whom it admits (counts, never a key)

WHOM IT ADMITS. The machines of the estate record this machine's node verified and applied (`full org records
apply`, under `node/records/`), minus every machine and key a revision or an applied delta entry revokes. Nobody else,
and nobody at all while this machine has applied no record or the record's window has ended.

THE ORIGIN. What every machine signs into its `auth`. On this machine alone it is `http://127.0.0.1:<port>` (the
default). For your other machines put a TLS terminator in front (a private network's own HTTPS, a tunnel or a reverse
proxy) and pass its `https://` origin: the relay itself serves plain HTTP on the address you bind, by default
127.0.0.1. An `http://` origin is a loopback literal only, as the protocol requires.

WHAT IT KEEPS. Queued messages, sealed under the relay's own key (`spool.key`) in `node/relay/spool.sqlite`, both
owner-only, until they are acknowledged or their ttl ends; never a key of yours and never a secret in clear (what must
stay secret is sealed end to end by its sender). `--memory` keeps nothing on disk. Blobs are not carried yet.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))                    # the engine first: `node` is the package, never tools/node.py
import paths  # noqa: E402

PROG = "project-observatory full" + " relay"
CONFIG = paths.CONFIG / "relay.json"
SCHEMA = "observatory-relay/1"
DEFAULT_PORT = 47317


def node_folder() -> Path:
    return paths.setting_path("OBSERVATORY_NODE", paths.HOME / "node").resolve()


def _relay_server():
    """`node.relay_server` of the engine, never `tools/node.py` (`full node`), which shares the name."""
    import importlib
    if sys.path[0] != str(ROOT):
        sys.path.insert(0, str(ROOT))
    loaded = sys.modules.get("node")
    if loaded is not None and not hasattr(loaded, "__path__"):
        del sys.modules["node"]
    return importlib.import_module("node.relay_server")


class RelayConfigError(ValueError):
    """A setting the relay refuses; the message says what to pass instead."""


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def checked(origin: str | None, host: str | None, port: int | None) -> dict:
    """`{origin, host, port}` the relay can serve, or RelayConfigError. An `http://` origin is this machine's own
    address: a loopback bind on the same port. An `https://` origin is a TLS terminator in front of the bind."""
    port = DEFAULT_PORT if port is None else port
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise RelayConfigError("--port is 1 to 65535")
    host = host or "127.0.0.1"
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        raise RelayConfigError("--host is an address literal to bind, such as 127.0.0.1") from None
    origin = origin or f"http://127.0.0.1:{port}"
    rs = _relay_server()
    if not rs.relay.is_relay_origin(origin):
        raise RelayConfigError("--origin is https:// and a host name (the TLS terminator in front of this relay), or "
                               "http:// and a loopback literal with its port, such as http://127.0.0.1:%d; no path"
                               % port)
    parts = urlsplit(origin)
    if parts.scheme == "http":
        if not _loopback(host):
            raise RelayConfigError("an http:// origin serves this machine only: bind a loopback --host, or put a TLS "
                                   "terminator in front and pass its https:// --origin")
        if parts.port != port:
            raise RelayConfigError(f"an http:// origin names the port it is served on: http://127.0.0.1:{port}")
    return {"origin": origin, "host": host.strip("[]"), "port": port}


def read_config() -> dict | None:
    try:
        doc = json.loads(CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise RelayConfigError(f"{CONFIG} is not {SCHEMA}; run `{PROG} enable` again")
    return doc


def write_config(doc: dict) -> None:
    import atomic
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(CONFIG, json.dumps(doc, indent=1, sort_keys=True) + "\n")


def _printer(stream):
    lock = threading.Lock()

    def log(record: dict) -> None:
        line = json.dumps(dict(record, at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                          separators=(",", ":"), sort_keys=True)
        with lock:
            print(line, file=stream, flush=True)
    return log


# ---- Inside the node's server (MACHINE-UTILITY.md D1) ------------------------------------------------------------------

def start_configured(log=None):
    """The relay `config/relay.json` enables, started, or None when none is enabled. Raises when an enabled relay
    cannot start (a refused setting, a port in use); the server records that in its `degraded`."""
    doc = read_config()
    if not doc or not doc.get("enabled"):
        return None
    setting = checked(doc.get("origin"), doc.get("host"), doc.get("port"))
    rs = _relay_server()
    server = rs.build(node_folder(), log=log or _printer(sys.stdout), **setting)
    return server.start()


def health(server) -> str | None:
    """None while the relay accepts connections and admits, else why not, for the server's `degraded`."""
    if server is None:
        return None
    listening = server.health()                       # an accept loop that ended or keeps failing
    if listening:
        return listening
    answer = server.admission.current(server.now_ms())
    if answer.get("ok"):
        return None
    why = {"no-record": "this machine's node has applied no estate record (`full org records apply` applies one)",
           "expired": "the estate record this machine applied has expired; apply its newer revision",
           "not-yet-valid": "the estate record this machine applied is not valid yet (check the clock)",
           "record-unreadable": "the applied estate record does not match its digest; apply it again"}
    return "relay: admits nobody, " + why.get(answer.get("refusal"), str(answer.get("refusal")))


# ---- Commands ----------------------------------------------------------------------------------------------------------

def serve(args) -> int:
    try:
        setting = checked(args.origin, args.host, args.port)
        rs = _relay_server()
    except RelayConfigError as error:
        print(f"relay: {error}", file=sys.stderr)
        return 2
    except RuntimeError as error:                      # the engine's `full` extra
        print(f"relay: unavailable: {error}", file=sys.stderr)
        return 2
    log = _printer(sys.stderr)
    try:
        server = rs.build(node_folder(), data=Path(args.data).resolve() if args.data else None, memory=args.memory,
                          log=log, **setting).start()
    except OSError as error:
        print(f"relay: cannot listen on {setting['host']}:{setting['port']}: {error.strerror or error}. Another "
              f"program holds the port (the node's server, when `{PROG} enable` is on); pass --port.", file=sys.stderr)
        return 1
    reason = health(server)
    if reason:
        log({"event": "relay.degraded", "reason": reason})
    stop = threading.Event()
    for name in ("SIGTERM", "SIGINT"):
        signal.signal(getattr(signal, name), lambda *_: stop.set())
    print(f"relay {setting['origin']} listening on {setting['host']}:{server.port}", file=sys.stderr, flush=True)
    try:
        while not stop.wait(1.0):
            pass
    finally:
        server.stop()
        server.mailboxes.close()
    return 0


def status() -> dict:
    doc = read_config()
    out: dict = {"schema": "observatory-relay-status/1", "enabled": bool(doc and doc.get("enabled"))}
    if doc:
        out.update({k: doc.get(k) for k in ("origin", "host", "port")})
    rs = _relay_server()
    admission = rs.Admission(rs.NodeRecords(node_folder()).read, log=lambda record: None).current(
        int(time.time() * 1000))
    out["admitting"] = bool(admission.get("ok"))
    if admission.get("ok"):
        out.update({"revision": admission.get("revision"), "machines": len(admission.get("keys") or {}),
                    "excluded": len(admission.get("excluded") or []), "revoked": len(admission.get("revoked") or [])})
    else:
        out["refusal"] = admission.get("refusal")
    if doc and doc.get("enabled"):
        host = doc.get("host") or "127.0.0.1"
        probe = "127.0.0.1" if not _loopback(host) else host
        url = f"http://{'[' + probe + ']' if ':' in probe else probe}:{doc.get('port')}/healthz"
        try:
            with urllib.request.urlopen(url, timeout=3) as answer:
                out["answers"] = json.loads(answer.read(4096)).get("ok") is True
        except (OSError, ValueError, urllib.error.URLError):
            out["answers"] = False
    return out


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=PROG, description="Your own fabric-relay/0.1 relay for your estate's machines.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the relay in the foreground")
    e = sub.add_parser("enable", help="run the relay inside the node's server from its next start")
    for q in (s, e):
        q.add_argument("--origin", required=q is e, help="what machines sign: https://NAME of the TLS terminator in "
                                                         "front, or http://127.0.0.1:PORT on this machine alone")
        q.add_argument("--host", help="the address to bind (default 127.0.0.1)")
        q.add_argument("--port", type=int, help=f"the port to bind (default {DEFAULT_PORT})")
    store = s.add_mutually_exclusive_group()
    store.add_argument("--data", help="the spool folder (default the workspace's node/relay/)")
    store.add_argument("--memory", action="store_true", help="keep queued messages in memory only")
    sub.add_parser("disable", help="stop running the relay inside the node's server from its next start")
    t = sub.add_parser("status", help="whether the relay is enabled, answers and admits")
    t.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.cmd == "serve":
            return serve(args)
        if args.cmd == "enable":
            setting = checked(args.origin, args.host, args.port)
            write_config(dict({"schema": SCHEMA, "enabled": True}, **setting))
            print(f"relay: enabled at {setting['origin']} (bound to {setting['host']}:{setting['port']}); it starts "
                  "with the node's server, at its next start (a login, `full update`). To start it now, restart that "
                  "server: python \"$(project-observatory full-path)/tools/serverd.py\" --install")
            return 0
        if args.cmd == "disable":
            doc = read_config()
            if doc is None or not doc.get("enabled"):
                print("relay: not enabled")
                return 0
            write_config(dict(doc, enabled=False))
            print("relay: disabled; it stops with the node's server's next start")
            return 0
        doc = status()
        if args.json:
            print(json.dumps(doc, indent=1, sort_keys=True))
        else:
            line = ("enabled at " + str(doc.get("origin"))) if doc["enabled"] else "not enabled"
            if doc["enabled"]:
                line += "; answers" if doc.get("answers") else "; does not answer (is the node's server running?)"
            line += (f"; admits {doc['machines']} machines of revision {doc['revision']}" if doc["admitting"]
                     else f"; admits nobody ({doc.get('refusal')})")
            print("relay: " + line)
        return 0 if not doc["enabled"] or (doc.get("answers") and doc["admitting"]) else 1
    except RelayConfigError as error:
        print(f"relay: {error}", file=sys.stderr)
        return 2
    except RuntimeError as error:
        print(f"relay: unavailable: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
