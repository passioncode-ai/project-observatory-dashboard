#!/usr/bin/env python3
"""The operator's door to access-bindings/1: which agent may reach this workspace's memory.

    project-observatory full access-binding show [--json]
    project-observatory full access-binding issue PRINCIPAL --project PROJECT --token-file PATH
    project-observatory full access-binding revoke BINDING_ID

`issue` also takes `--project` again for more projects, `--scope`, `--class`,
`--effect read|propose`, `--workflow`, `--days` and `--audience observatory:<instance>`.

A binding lets one principal (`agent:<name>` or `service:<name>`) reach the memory tools
over HTTP, for the listed projects, scopes, class ceiling and effect ceiling, until it
expires. The local stdio agent needs none: it is built in (`local:stdio`).

`issue` writes the new bearer to `--token-file` (created, owner-only, never overwritten)
and NOWHERE ELSE: not to the terminal, not to a log. The registry keeps only its SHA-256.
Hand the file to the client's configuration; a bearer pasted into a chat is a bearer in a
transcript. `revoke` takes effect on the next call. Both need a terminal: a binding is
the operator's grant, and a script able to mint one could grant itself the memory.

Every change raises `revision`; the engine refuses a file older than the last revision
it applied, so a restored backup cannot reopen a revoked binding.

The decision and its rules: docs/design/ACCESS-BINDING.md (PB-137 N-007, N-008).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import access_binding as AB                                                         # noqa: E402
import memory_access as MA                                                          # noqa: E402

DEFAULT_SCOPES = ("memory.read", "memory.search")
MAX_DAYS = 365


def _is_terminal() -> bool:
    return sys.stdin.isatty()


def require_terminal(action: str) -> None:
    if not _is_terminal():
        raise SystemExit(
            f"access-binding: refusing to {action} without a terminal. A binding is the "
            f"operator's grant of this workspace's memory, and a script must not be able to "
            f"grant it. Run it yourself; there is no --yes.")


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _read() -> dict:
    """The current document, validated, or a fresh one. A broken file is refused:
    rewriting it would silently drop whatever the operator meant by it."""
    reg = MA.registry()          # raises BindingError on a broken file or a rollback
    if reg.revision == 0:
        return {"schema": AB.SCHEMA, "revision": 0, "bindings": []}
    return json.loads(MA.registry_path().read_text(encoding="utf-8"))


def _write(doc: dict) -> AB.Registry:
    import atomic
    doc["revision"] = int(doc["revision"]) + 1
    reg = AB.parse(doc)
    path = MA.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(path, doc, indent=2)
    os.chmod(path, 0o600)
    MA.registry()                # record the applied revision now, so a rollback is caught
    return reg


def _state(b: AB.Binding, now: str) -> str:
    if b.revoked_at is not None:
        return f"revoked {b.revoked_at}"
    if AB._when(b.expires_at) <= AB._when(now):
        return f"expired {b.expires_at}"
    return f"in force until {b.expires_at}"


def cmd_show(as_json: bool) -> int:
    try:
        reg = MA.registry()
    except AB.BindingError as exc:
        print(f"access-binding: refused — {exc}. Every HTTP request is refused until it is "
              f"repaired; the local stdio agent is still served.", file=sys.stderr)
        return 1
    now = _stamp(datetime.now(timezone.utc))
    rows = [{"bindingId": b.binding_id, "principal": b.principal, "audience": b.audience,
             "projects": list(b.projects), "workflows": list(b.workflows) if b.workflows else None,
             "scopes": sorted(b.scopes), "classCeiling": b.class_ceiling,
             "effectCeiling": b.effect_ceiling, "expiresAt": b.expires_at,
             "revokedAt": b.revoked_at, "state": _state(b, now)} for b in reg.bindings]
    if as_json:
        print(json.dumps({"schema": AB.SCHEMA, "revision": reg.revision,
                          "local": "local:stdio — every project and class, effect up to propose",
                          "bindings": rows}, indent=1))
        return 0
    print(f"access-bindings/1 · revision {reg.revision} · local:stdio is built in "
          f"(every project and class, effect up to propose)")
    if not rows:
        print("  no bindings: only the local stdio agent is served")
        return 0
    for r in rows:
        print(f"  {r['bindingId']} {r['principal']} · {', '.join(r['projects'])} · "
              f"{', '.join(r['scopes'])} · class ≤ {r['classCeiling']} · effect ≤ "
              f"{r['effectCeiling']} · {r['state']}")
    return 0


def cmd_issue(a: argparse.Namespace) -> int:
    require_terminal("issue a binding")
    token_file = pathlib.Path(a.token_file).expanduser()
    if token_file.exists():
        raise SystemExit(f"access-binding: {token_file} exists; the bearer is written to a new "
                         f"file only, never over another")
    if not 1 <= a.days <= MAX_DAYS:
        raise SystemExit(f"access-binding: --days must be 1..{MAX_DAYS}; a binding always expires")
    now = datetime.now(timezone.utc)
    bearer = secrets.token_urlsafe(32)
    binding = {
        "bindingId": f"bnd_{secrets.token_hex(6)}", "principal": a.principal, "channel": "http",
        "audience": a.audience or MA.own_audience(),
        "credentialRef": {"kind": "bearer-sha256", "digest": AB.bearer_digest(bearer)},
        "projects": a.projects, "workflows": a.workflows or None,
        "scopes": a.scopes or list(DEFAULT_SCOPES), "classCeiling": a.class_ceiling,
        "effectCeiling": a.effect, "issuedAt": _stamp(now),
        "expiresAt": _stamp(now + timedelta(days=a.days)), "revokedAt": None,
        "issuedBy": "operator", "via": "terminal"}
    try:
        doc = _read()
        doc["bindings"].append(binding)
        AB.parse({**doc, "revision": int(doc["revision"]) + 1})     # validate before any write
    except AB.BindingError as exc:
        raise SystemExit(f"access-binding: refused — {exc}") from None
    token_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(bearer + "\n")
    try:
        reg = _write(doc)
    except (AB.BindingError, OSError) as exc:
        token_file.unlink(missing_ok=True)
        raise SystemExit(f"access-binding: refused — {exc}; no binding was issued") from None
    print(f"{binding['bindingId']}: {a.principal} may reach {', '.join(a.projects)} "
          f"({', '.join(binding['scopes'])}; class ≤ {a.class_ceiling}; effect ≤ {a.effect}) "
          f"until {binding['expiresAt']} (revision {reg.revision}). The bearer is in "
          f"{token_file} (owner-only); give the client that file, never its contents in a chat.")
    return 0


def cmd_revoke(binding_id: str) -> int:
    require_terminal("revoke a binding")
    try:
        doc = _read()
        entry = next((b for b in doc["bindings"] if b["bindingId"] == binding_id), None)
        if entry is None:
            raise SystemExit(f"access-binding: no binding {binding_id}")
        if entry["revokedAt"] is not None:
            print(f"{binding_id}: already revoked {entry['revokedAt']}")
            return 0
        entry["revokedAt"] = _stamp(datetime.now(timezone.utc))
        reg = _write(doc)
    except AB.BindingError as exc:
        raise SystemExit(f"access-binding: refused — {exc}") from None
    print(f"{binding_id}: revoked (revision {reg.revision}). Its next call is refused with "
          f"`binding-revoked`.")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="project-observatory full access-binding",
                                description="Which agent may reach this workspace's memory over HTTP.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="the bindings and their state; never a bearer or its digest")
    s.add_argument("--json", action="store_true")
    i = sub.add_parser("issue", help="grant one principal (terminal only)")
    i.add_argument("principal", help="agent:<name> or service:<name>")
    i.add_argument("--project", dest="projects", action="append", required=True,
                   help="project:<slug>; repeatable")
    i.add_argument("--token-file", required=True,
                   help="a new file the bearer is written to (owner-only)")
    i.add_argument("--scope", dest="scopes", action="append", choices=AB.SCOPES,
                   help=f"repeatable; default {', '.join(DEFAULT_SCOPES)}")
    i.add_argument("--class", dest="class_ceiling", choices=AB.CLASSES,
                   default="project-internal", help="the highest class it may read")
    i.add_argument("--effect", choices=AB.EFFECTS, default="read",
                   help="read, or propose to write proposals and workflow memory")
    i.add_argument("--workflow", dest="workflows", action="append",
                   help="wf_<16 hex>; repeatable; a session binding touches only these")
    i.add_argument("--days", type=int, default=30, help=f"1..{MAX_DAYS}; default 30")
    i.add_argument("--audience", help="observatory:<instance>; default this workspace's")
    r = sub.add_parser("revoke", help="withdraw a binding at once (terminal only)")
    r.add_argument("binding_id")
    return p


def main(argv: list[str]) -> int:
    a = parser().parse_args(argv)
    if a.cmd == "show":
        return cmd_show(a.json)
    if a.cmd == "issue":
        return cmd_issue(a)
    return cmd_revoke(a.binding_id)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
