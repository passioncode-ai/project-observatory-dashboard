#!/usr/bin/env python3
"""`collectors/scan_mcp.py` and the `mcp-servers.json` projection, on planted configs.

Each agent's own config holds its MCP declarations, and this is what tracks
them. Three properties are
the whole point: a URL's query string never reaches a file (a token in one
would reach a transcript through `claude mcp list`);
Claude's liveness verdict is parsed for the servers Claude was asked about and
never borrowed for another agent; and a server scoped to another project is
out of view, not down.
"""
from __future__ import annotations
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_a_declaration_becomes_facts_and_never_a_value() -> None:
    m = load("collectors/scan_mcp.py", "scan_mcp")
    r = m.classify("alpha-search", {"type": "http", "url": "https://search.example.com/mcp?token=" + "x" * 24})
    check("the target keeps scheme, host and path", r["target"] == "https://search.example.com/mcp", r["target"])
    check("and says a key was in the URL — without the key",
          r["key_in_url"] is True and "x" * 24 not in json.dumps(r), json.dumps(r))
    r = m.classify("beta-tool", {"url": "https://tool.example.com/mcp",
                               "headers": {"Authorization": "Bearer nope"}})
    check("a keyed header is a boolean, not a header",
          r["key_in_header"] is True and "nope" not in json.dumps(r) and r["transport"] == "http",
          json.dumps(r))
    r = m.classify("local", {"command": "/usr/bin/env", "args": ["python3", "x.py"],
                             "env": {"OPENAI_API_KEY": "sk-nope"}})
    check("a stdio server records its executable's NAME and whether it exists",
          r["transport"] == "stdio" and r["command"] == "env" and r["command_present"] is True
          and r["args_count"] == 2, json.dumps(r))
    check("and a keyed env block is a boolean", r["key_in_env"] is True and "sk-nope" not in json.dumps(r))
    r = m.classify("oc", {"type": "remote", "command": ["npx", "-y", "thing"]})
    check("opencode's list-shaped command is read as executable + args",
          r["command"] == "npx" and r["args_count"] == 2, json.dumps(r))


def test_claudes_verdict_is_parsed_and_attributed() -> None:
    m = load("collectors/scan_mcp.py", "scan_mcp")
    lines = [
        "claude.ai Alpha Tracker: https://mcp.tracker.example.com/mcp - ! Needs authentication",
        "plugin:alpha:alpha-api: https://mcp.alpha.example.com/mcp (HTTP) - ✔ Connected",
        "docs-server: https://mcp.docs.example.com/mcp (HTTP) - ✔ Connected",
        "broken: /usr/local/bin/nothing - ✘ Failed to connect",
        "Checking MCP server health…",
    ]
    parsed = {}
    for raw in lines:
        mm = m.LINE.match(raw.strip())
        if mm:
            name = mm.group("name").strip()
            parsed[name] = {"✔": "connected", "✘": "failed", "!": "needs-auth"}[mm.group("mark")]
    check("every status line parses, the banner does not",
          set(parsed) == {"claude.ai Alpha Tracker", "plugin:alpha:alpha-api", "docs-server", "broken"},
          str(sorted(parsed)))
    check("connected, failed and needs-auth are told apart",
          parsed["docs-server"] == "connected" and parsed["broken"] == "failed"
          and parsed["claude.ai Alpha Tracker"] == "needs-auth", str(parsed))


def test_the_projection_ids_rows_and_keeps_other_agents_unprobed() -> None:
    es = load("collectors/estate_surfaces.py", "estate_surfaces")
    scan = {"scanned_at": "2026-09-14T00:00:00Z", "own_server": "observatory", "own_declared": False,
            "servers": [
                {"name": "docs-server", "agent": "claude", "scope": "user", "transport": "http",
                 "target": "https://mcp.docs.example.com/mcp", "liveness": "connected"},
                {"name": "docs-server", "agent": "cursor", "scope": "user", "transport": "http",
                 "target": "https://mcp.docs.example.com/mcp", "liveness": "not-probed"},
                {"name": "errors-server", "agent": "claude", "scope": "project:/srv/alpha-web",
                 "transport": "http", "target": "https://mcp.errors.example.com/mcp", "liveness": "not-probed"},
                {"name": "alpha-search", "agent": "claude", "scope": "user", "transport": "http",
                 "target": "https://search.example.com/mcp", "key_in_url": True, "liveness": "connected"},
                {"name": None, "agent": "opencode", "error": "ValueError"}]}
    rows = es.mcp_rows(scan)
    ids = [r["id"] for r in rows]
    check("ids are namespaced by agent, scope and name",
          "mcp:claude/user/docs-server" in ids and "mcp:cursor/user/docs-server" in ids
          and any(i.startswith("mcp:claude/project-") and i.endswith("/errors-server") for i in ids), str(ids))
    check("a row with no name (an unreadable config) is not a server", len(rows) == 4, str(len(rows)))
    check("Cursor's copy of a server is not given Claude's verdict",
          next(r for r in rows if r["agent"] == "cursor")["liveness"] == "not-probed")
    doc = es.mcp_document(scan, rows, "2026-09-14")
    check("totals count declarations and distinct servers apart",
          doc["totals"]["declarations"] == 4 and doc["totals"]["distinct_servers"] == 3, str(doc["totals"]))
    check("and how many servers live in one agent only",
          doc["totals"]["in_one_agent_only"] == 2, str(doc["totals"]))
    check("and how many carry a key in the URL", doc["totals"]["key_in_url"] == 1)
    check("the document carries a DATE, not a clock", doc["scanned_on"] == "2026-09-14")
    check("and says whether the observatory's own server is declared anywhere",
          doc["own_declared"] is False)


if __name__ == "__main__":
    print("the MCP inventory — declarations as facts, verdicts attributed, never a token\n")
    for fn in (test_a_declaration_becomes_facts_and_never_a_value,
               test_claudes_verdict_is_parsed_and_attributed,
               test_the_projection_ids_rows_and_keeps_other_agents_unprobed):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe agents' MCP servers are tracked here, and no query string ever will be\033[0m")
