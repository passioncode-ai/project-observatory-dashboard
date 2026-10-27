#!/usr/bin/env python3
"""The person's door to agent-bindings/1: which local agent the node names when it asks for a key (OBS-260).

    project-observatory full agent-binding show [--json]
    project-observatory full agent-binding issue NAME
    project-observatory full agent-binding revoke NAME

A local agent's MCP server (`mcp/server.py`, started by the agent) asks the node for a key the vault lacks with
`observatory_secret_request`, and the node shows the person which agent asks. That name comes from the agent's
binding, never from text the agent wrote. `issue` is how the person connects an agent: it writes a random bearer to
the owner-only file `config/agent-bindings/NAME.token` (printed as a path, never as a token), keeps only its SHA-256
in `config/agent-bindings.json`, and prints the line that registers the MCP server with `--agent NAME`. Issuing
again for a connected agent keeps its token. `revoke` takes effect on the agent's next request and removes the
token file.

Both need a terminal: a script able to mint bindings could name itself after any agent the person trusts. The
limit, stated plainly (docs/design/PERSON-SURFACES.md D4): the token file is owner-only, so any agent running as the
same user can read another agent's token and file a request under its name. The prompt still shows the slot and the
reason, and nothing is stored without the person typing it.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths  # noqa: E402
import secret_prompt as SP  # noqa: E402


def _is_terminal() -> bool:
    return sys.stdin.isatty()


def require_terminal(action: str) -> None:
    if not _is_terminal():
        raise SystemExit(f"agent-binding: refusing to {action} without a terminal. A binding names an agent to the "
                         f"person, and a script must not be able to name itself. Run it yourself; there is no --yes.")


def registration(name: str) -> str:
    """The line that registers this engine's MCP server for the agent, with its binding."""
    return (f"claude mcp add observatory --scope user -e OBSERVATORY_HOME=\"{paths.HOME}\" -- "
            f"\"{sys.executable}\" \"{ROOT / 'mcp' / 'server.py'}\" --agent {name}")


def cmd_show(args) -> int:
    try:
        doc = SP.read_bindings()
    except SP.BindingsError as exc:
        print(f"agent-binding: {exc}", file=sys.stderr)
        return 1
    rows = [{"name": b["name"], "issuedAt": b["issuedAt"], "revokedAt": b["revokedAt"],
             "tokenFile": SP.token_path(b["name"]).is_file()} for b in doc["bindings"]]
    if args.json:
        print(json.dumps({"schema": SP.BINDINGS_SCHEMA, "revision": doc["revision"], "bindings": rows}, indent=1))
        return 0
    if not rows:
        print("no agent is connected: `project-observatory full agent-binding issue NAME` connects one")
        return 0
    for r in rows:
        state = f"revoked {r['revokedAt']}" if r["revokedAt"] else (
            "connected" if r["tokenFile"] else "token file missing: issue it again")
        print(f"{r['name']}: issued {r['issuedAt']}, {state}")
    return 0


def cmd_issue(args) -> int:
    require_terminal("issue an agent binding")
    try:
        path, new = SP.issue(args.name)
    except SP.BindingsError as exc:
        print(f"agent-binding: {exc}", file=sys.stderr)
        return 1
    print(f"{'issued' if new else 'kept'} the binding for {args.name}: token in {path} (owner-only; never paste it)")
    print("register the MCP server with it (or add `--agent " + args.name + "` to an existing registration):")
    print("  " + registration(args.name))
    return 0


def cmd_revoke(args) -> int:
    require_terminal("revoke an agent binding")
    try:
        found = SP.revoke(args.name)
    except SP.BindingsError as exc:
        print(f"agent-binding: {exc}", file=sys.stderr)
        return 1
    print(f"revoked the binding for {args.name}" if found else f"{args.name} had no live binding; nothing changed")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="project-observatory full agent-binding",
                                description="Which local agent the node names when it asks the person for a key.")
    sub = p.add_subparsers(dest="action", required=True)
    show = sub.add_parser("show", help="the connected agents (never a token)")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)
    for verb, func, text in (("issue", cmd_issue, "connect an agent: a binding and its token file"),
                             ("revoke", cmd_revoke, "disconnect an agent")):
        q = sub.add_parser(verb, help=text)
        q.add_argument("name", help="the agent's name: lowercase letters, digits, '.', '_' or '-'")
        q.set_defaults(func=func)
    return p


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
