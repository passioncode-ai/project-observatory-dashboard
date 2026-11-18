#!/usr/bin/env python3
"""The door to Prowl API keys: issue one straight into a vault slot, revoke one, list them.

    T="$(project-observatory full-path)/tools"            # the installed engine's tools
    python "$T/prowl.py" issue NAME --user EMAIL --slot PROJECT/ENV/NAME [--daily-usd 100] [--total-usd N]
                                    [--billing-mode standard|at_cost] [--rotate] [--admin-slot P/E/N] [--base URL]
    python "$T/prowl.py" revoke KEY_ID --reason TEXT [--admin-slot P/E/N] [--base URL]
    python "$T/prowl.py" list --user EMAIL [--include-revoked] [--admin-slot P/E/N] [--base URL]

Prowl mints keys through its admin console MCP (`/admin/mcp`, tool
`admin_api_key_issue`), which answers the raw key once, in the tool's result. An agent that
calls that tool itself gets the value in its transcript. This door makes the same call in
its own process, hands the value to `vault.py put` (or `rotate`) on stdin and prints only the
key's id, its prefix and the slot. The value never reaches argv, a URL, the output or the
journal.

The admin credential is an admin's own `prowl_` key, read in memory from a vault slot named
by `--admin-slot` or `OBSERVATORY_PROWL_ADMIN_SLOT` (`project/env/NAME`), and Prowl's origin by
`--base` or `OBSERVATORY_PROWL_BASE` (`https://...`). Neither is built in: which slot holds the
key and which host serves the console are the installation's facts, not the engine's.

Failure order: the destination slot is checked BEFORE anything is minted (a key minted for a
slot the vault then refuses is a live credential with nowhere to go); a key whose delivery
fails is revoked again at once, and the answer says whether that revocation was confirmed.
"""
from __future__ import annotations

#: This door declares itself (OBS-605): `tools/door_registry.py` reads it, so integration health tracks prowl.
DOOR_PROVIDER = "prowl"

import argparse
import ipaddress
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

ADMIN_PATH = "/admin/mcp"
ADMIN_SLOT_ENV = "OBSERVATORY_PROWL_ADMIN_SLOT"
BASE_ENV = "OBSERVATORY_PROWL_BASE"
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT = 60
#: Prowl's own default (T441): a key issued with no daily limit gets 100 USD a day.
DEFAULT_DAILY_USD = 100.0


class DoorError(RuntimeError):
    """A refusal or failure the door reports in one line, with no value in it."""


# ─────────────────────────── arguments ───────────────────────────────────────

def parse_slot(text: str | None, what: str) -> tuple[str, str, str]:
    parts = str(text or "").strip().split("/")
    if len(parts) != 3 or not all(parts):
        raise DoorError(f"{what} is PROJECT/ENV/NAME, got {text!r}")
    return parts[0], parts[1], parts[2]


def admin_slot(arg: str | None) -> tuple[str, str, str]:
    text = arg or os.environ.get(ADMIN_SLOT_ENV, "")
    if not text:
        raise DoorError(f"no admin slot: pass --admin-slot PROJECT/ENV/NAME or set {ADMIN_SLOT_ENV} "
                        "to the vault slot holding an admin's own prowl_ key")
    return parse_slot(text, "the admin slot")


def check_base(base: str | None) -> str:
    """https anywhere; plain http only to loopback (tests, a local Prowl). None reads the env."""
    base = base or os.environ.get(BASE_ENV, "")
    if not base:
        raise DoorError(f"no Prowl origin: pass --base https://... or set {BASE_ENV}")
    u = urllib.parse.urlparse(base)
    if u.scheme == "https" and u.hostname:
        return base.rstrip("/")
    if u.scheme == "http" and u.hostname:
        try:
            if u.hostname == "localhost" or ipaddress.ip_address(u.hostname).is_loopback:
                return base.rstrip("/")
        except ValueError:
            pass
    raise DoorError(f"the base must be https, or http on loopback; got {base!r}")


def destination(slot: tuple[str, str, str]) -> tuple[str, str, str]:
    """The slot with its project normalised to the vault folder, checked before minting."""
    import vault
    try:
        folder, said = vault.project_folder(slot[0], slot[1], slot[2])
        vault.validate_names(folder, slot[1], slot[2])
    except vault.VaultBoundaryError as exc:
        raise DoorError(str(exc)) from None
    if said:
        print(f"project: {said}", file=sys.stderr)
    return folder, slot[1], slot[2]


def slot_holds_value(slot: tuple[str, str, str]) -> bool:
    import vault
    return vault._slot(*slot).is_file()


def read_admin_key(slot: tuple[str, str, str]) -> str:
    import vault
    try:
        folder, _said = vault.project_folder(slot[0], slot[1], slot[2])
        path = vault._slot(folder, slot[1], slot[2])
        value = vault._read_private(path).strip() if path.is_file() else ""
    except (OSError, RuntimeError, vault.VaultBoundaryError) as exc:
        raise DoorError(f"the admin slot {'/'.join(slot)} could not be read ({type(exc).__name__})") from None
    if not value:
        raise DoorError(f"the admin slot {'/'.join(slot)} holds no value")
    return value


# ─────────────────────────── the admin MCP ───────────────────────────────────

def _messages(raw: bytes, content_type: str) -> list[dict]:
    """JSON-RPC messages of one answer: a JSON body, or the `data:` lines of an event stream."""
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in (content_type or ""):
        out, data = [], []
        for line in text.splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line.strip() and data:
                try:
                    out.append(json.loads("\n".join(data)))
                except ValueError:
                    pass
                data = []
        return out
    if not text.strip():
        return []
    doc = json.loads(text)
    return doc if isinstance(doc, list) else [doc]


class AdminMCP:
    """A minimal streamable-HTTP MCP client for one admin session. The key goes only into the
    Authorization header; an HTTP error is reported by status, never with the body echoed."""

    def __init__(self, base: str, key: str):
        self.url = check_base(base) + ADMIN_PATH
        self._key = key
        self._session: str | None = None
        self._next = 0

    def _post(self, payload: dict) -> list[dict]:
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL_VERSION,
                   "User-Agent": "observatory-prowl-door"}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session = sid
                return _messages(resp.read(), resp.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:
            raise DoorError(f"Prowl's admin console answered HTTP {exc.code}"
                            + (" (the admin key was refused)" if exc.code in (401, 403) else "")) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DoorError(f"Prowl's admin console is unreachable ({type(exc).__name__})") from None

    def _rpc(self, method: str, params: dict) -> dict:
        self._next += 1
        rid = self._next
        for msg in self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}):
            if msg.get("id") == rid:
                if "error" in msg:
                    err = msg["error"] or {}
                    raise DoorError(f"{method} refused: {str(err.get('message', 'error'))[:200]}")
                return msg.get("result") or {}
        raise DoorError(f"{method}: no answer from Prowl's admin console")

    def open(self) -> None:
        self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                 "clientInfo": {"name": "observatory-prowl-door", "version": "1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: str, arguments: dict) -> dict:
        """The tool's JSON answer. A tool that answers `Error: ...` raises with that reason."""
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        text = "".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")
        if result.get("isError") or text.startswith("Error"):
            raise DoorError(f"{tool}: {text[:300] or 'refused'}")
        try:
            doc = json.loads(text)
        except ValueError:
            raise DoorError(f"{tool}: the answer was not JSON") from None
        if not isinstance(doc, dict):
            raise DoorError(f"{tool}: the answer was not an object")
        return doc


# ─────────────────────────── delivery ────────────────────────────────────────

def deliver(value: str, slot: tuple[str, str, str], rotate: bool) -> None:
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "vault.py"), "rotate" if rotate else "put", *slot],
                       input=value, capture_output=True, text=True, timeout=TIMEOUT)
    if p.returncode != 0:
        raise DoorError("the vault refused the key; child output withheld")


def _public(doc: dict) -> dict:
    """An answer with every value-bearing field removed."""
    return {k: v for k, v in doc.items() if k not in ("key", "raw_key", "secret", "value", "note")}


# ─────────────────────────── commands ────────────────────────────────────────

def cmd_issue(a) -> dict:
    name = (a.name or "").strip()
    if not 3 <= len(name) <= 120:
        raise DoorError("the key's name must be 3 to 120 characters")
    for label, v in (("--daily-usd", a.daily_usd), ("--total-usd", a.total_usd)):
        if v is not None and not (0 < v <= 100_000):
            raise DoorError(f"{label} must be above 0 and at most 100000")
    check_base(a.base)
    slot = destination(parse_slot(a.slot, "--slot"))
    held = slot_holds_value(slot)
    if held and not a.rotate:
        raise DoorError(f"{'/'.join(slot)} already holds a value; pass --rotate to replace it keeping history")
    if not held and a.rotate:
        raise DoorError(f"{'/'.join(slot)} holds no value to rotate; issue without --rotate")
    client = AdminMCP(a.base, read_admin_key(admin_slot(a.admin_slot)))
    client.open()
    args = {"user": a.user, "name": name, "billing_mode": a.billing_mode,
            "daily_spend_limit_usd": a.daily_usd if a.daily_usd is not None else DEFAULT_DAILY_USD}
    if a.total_usd is not None:
        args["total_spend_limit_usd"] = a.total_usd
    minted = client.call("admin_api_key_issue", args)
    value = str(minted.get("key") or "")
    key_id = str(minted.get("id") or "")
    if not value or not key_id:
        raise DoorError("admin_api_key_issue answered no key or no id; nothing was delivered")
    try:
        deliver(value, slot, rotate=held)
    except DoorError:
        revoked = False
        try:
            revoked = bool(client.call("admin_api_key_revoke", {
                "key_id": key_id, "reason": "observatory prowl door: delivery to the vault failed"}).get("revoked"))
        except DoorError:
            pass
        raise DoorError(f"the vault refused key {key_id}; it was "
                        + ("revoked again" if revoked else "NOT confirmed revoked: revoke it with "
                           f"`prowl.py revoke {key_id} --reason ...`")) from None
    finally:
        value = ""
    out = _public(minted)
    out.update({"slot": "/".join(slot), "delivered": "rotate" if held else "put"})
    return out


def cmd_revoke(a) -> dict:
    reason = (a.reason or "").strip()
    if len(reason) < 3:
        raise DoorError("--reason is required (3 characters or more)")
    client = AdminMCP(a.base, read_admin_key(admin_slot(a.admin_slot)))
    client.open()
    return _public(client.call("admin_api_key_revoke", {"key_id": a.key_id, "reason": reason}))


def cmd_list(a) -> dict:
    client = AdminMCP(a.base, read_admin_key(admin_slot(a.admin_slot)))
    client.open()
    doc = client.call("admin_api_keys_list", {"user": a.user, "include_revoked": bool(a.include_revoked)})
    doc["keys"] = [_public(k) for k in doc.get("keys") or [] if isinstance(k, dict)]
    return doc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="prowl.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--admin-slot", help=f"vault slot of an admin's prowl_ key; default ${ADMIN_SLOT_ENV}")
        sp.add_argument("--base", default=None, help=f"Prowl's origin; default ${BASE_ENV}")

    i = sub.add_parser("issue", help="mint a key and put it straight into a vault slot")
    i.add_argument("name")
    i.add_argument("--user", required=True, help="the account's email or user id")
    i.add_argument("--slot", required=True, help="PROJECT/ENV/NAME the key goes to")
    i.add_argument("--daily-usd", type=float, default=None, help=f"daily ceiling in USD (default {DEFAULT_DAILY_USD:g})")
    i.add_argument("--total-usd", type=float, default=None, help="total ceiling in USD (default none)")
    i.add_argument("--billing-mode", choices=("standard", "at_cost"), default="standard")
    i.add_argument("--rotate", action="store_true", help="the slot holds a value: replace it, keeping history")
    common(i)
    r = sub.add_parser("revoke", help="revoke a key by id; its next call answers 401")
    r.add_argument("key_id")
    r.add_argument("--reason", required=True)
    common(r)
    lst = sub.add_parser("list", help="a user's keys with limits and last use; never a value")
    lst.add_argument("--user", required=True)
    lst.add_argument("--include-revoked", action="store_true")
    common(lst)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        out = {"issue": cmd_issue, "revoke": cmd_revoke, "list": cmd_list}[a.cmd](a)
    except DoorError as exc:
        print(f"prowl.py {a.cmd}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
