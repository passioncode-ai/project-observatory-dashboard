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
    probed = es.mcp_document(dict(scan, probe={"state": "probed", "probed_at": "2026-09-14T10:00:00Z",
                                               "complete": True, "servers": {"x": {}}}), rows, "2026-09-14")
    check("the document says when liveness was last asked (A36), without the probe's rows",
          probed["probe"] == {"state": "probed", "probed_at": "2026-09-14T10:00:00Z", "complete": True},
          str(probed.get("probe")))


STUB = """#!{py}
import sys, time
print("docs-server: https://mcp.docs.example.com/mcp (HTTP) - \u2714 Connected", flush=True)
time.sleep({sleep})
print("slow-server: https://mcp.slow.example.com/mcp (HTTP) - \u2714 Connected", flush=True)
"""


def stub_claude(directory: pathlib.Path, sleep: float) -> None:
    fake = directory / "claude"
    fake.write_text(STUB.format(py=sys.executable, sleep=sleep), encoding="utf-8")
    fake.chmod(0o755)


def test_the_probe_timeout_is_configurable_and_generous_by_default() -> None:
    import os
    m = load("collectors/scan_mcp.py", "scan_mcp")
    saved = os.environ.pop(m.PROBE_TIMEOUT_ENV, None)
    try:
        value, note = m.probe_timeout()
        check("the default outlasts a CLI that health-checks many servers", value >= 180 and note is None,
              f"{value} {note}")
        os.environ[m.PROBE_TIMEOUT_ENV] = "300"
        check("the environment raises it", m.probe_timeout() == (300.0, None), str(m.probe_timeout()))
        for bad in ("soon", "0", "-5", "99999"):
            os.environ[m.PROBE_TIMEOUT_ENV] = bad
            value, note = m.probe_timeout()
            check(f"an unusable value {bad!r} falls back to the default and says so",
                  value == m.DEFAULT_PROBE_TIMEOUT and note and m.PROBE_TIMEOUT_ENV in note, f"{value} {note}")
    finally:
        os.environ.pop(m.PROBE_TIMEOUT_ENV, None)
        if saved is not None:
            os.environ[m.PROBE_TIMEOUT_ENV] = saved


def test_a_probe_that_runs_out_of_time_keeps_what_it_heard_and_says_so() -> None:
    import os
    import tempfile
    m = load("collectors/scan_mcp.py", "scan_mcp")
    saved_path = os.environ.get("PATH", "")
    with tempfile.TemporaryDirectory() as tmp:
        stub_claude(pathlib.Path(tmp), sleep=30)
        os.environ["PATH"] = tmp + os.pathsep + saved_path
        try:
            probe, why, complete = m.claude_probe(timeout=2)
        finally:
            os.environ["PATH"] = saved_path
    check("a timeout is not a crash and not a complete answer", complete is False and why is not None, str(why))
    check("the reason names the limit and the variable that raises it",
          why is not None and "2 s" in why and m.PROBE_TIMEOUT_ENV in why, str(why))
    check("servers reported before the cut keep their verdict",
          probe.get("docs-server", {}).get("status") == "connected", str(probe))
    rows = [{"name": "docs-server", "agent": "claude", "scope": "user"},
            {"name": "slow-server", "agent": "claude", "scope": "user"},
            {"name": "docs-server", "agent": "cursor", "scope": "user"}]
    m.attribute_liveness(rows, probe, complete)
    live = {(r["agent"], r["name"]): r["liveness"] for r in rows}
    check("a server the cut-off list never reached is not-probed, never not-listed",
          live[("claude", "slow-server")] == "not-probed", str(live))
    check("the one it reached is connected", live[("claude", "docs-server")] == "connected", str(live))
    check("and Cursor still borrows nothing", live[("cursor", "docs-server")] == "not-probed", str(live))
    full = [{"name": "gone-server", "agent": "claude", "scope": "user"}]
    m.attribute_liveness(full, {"docs-server": {"status": "connected", "plugin": None, "detail": "x"}}, True)
    check("a complete list that omits a declared server still says not-listed",
          full[0]["liveness"] == "not-listed", str(full))


def test_a_probe_that_finishes_in_time_is_complete() -> None:
    import os
    import tempfile
    m = load("collectors/scan_mcp.py", "scan_mcp")
    saved_path = os.environ.get("PATH", "")
    with tempfile.TemporaryDirectory() as tmp:
        stub_claude(pathlib.Path(tmp), sleep=0)
        os.environ["PATH"] = tmp + os.pathsep + saved_path
        try:
            probe, why, complete = m.claude_probe(timeout=60)
        finally:
            os.environ["PATH"] = saved_path
    check("both servers are heard and nothing is degraded",
          complete is True and why is None and set(probe) == {"docs-server", "slow-server"}, f"{probe} {why}")


def test_the_scheduled_tick_carries_a_raised_limit() -> None:
    """launchd starts the tick with the plist's environment only, so a limit raised
    in the shell that installs the job must travel in it, and nothing else extra."""
    import os
    m = load("collectors/scan_mcp.py", "scan_mcp")
    sys.path.insert(0, str(ROOT / "tools"))
    launchd = load("tools/install_launchd.py", "install_launchd")
    saved = os.environ.pop(m.PROBE_TIMEOUT_ENV, None)
    try:
        check("no limit set, no variable in the plist",
              m.PROBE_TIMEOUT_ENV not in launchd.build(1800)["EnvironmentVariables"])
        os.environ[m.PROBE_TIMEOUT_ENV] = "400"
        env = launchd.build(1800)["EnvironmentVariables"]
        check("a raised limit reaches the scheduled tick", env.get(m.PROBE_TIMEOUT_ENV) == "400", str(env))
        os.environ[m.PROBE_TIMEOUT_ENV] = "not-a-number"
        env = launchd.build(1800)["EnvironmentVariables"]
        check("an unusable value is not written into the plist", m.PROBE_TIMEOUT_ENV not in env, str(env))
    finally:
        os.environ.pop(m.PROBE_TIMEOUT_ENV, None)
        if saved is not None:
            os.environ[m.PROBE_TIMEOUT_ENV] = saved


def _fixture():
    sys.path.insert(0, str(ROOT / "tests"))
    import mcp_config_fixture
    return mcp_config_fixture


def test_every_agent_format_is_read_with_its_wire_transport() -> None:
    import tempfile
    fx = _fixture()
    m = load("collectors/scan_mcp.py", "scan_mcp")
    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        fx.write_home(home)
        rows, sources = m.read_sources(home)
    got = {(r["agent"], r["name"], r["wire"]) for r in rows if r.get("name")}
    check("each of the six formats yields its servers with the wire transport the agent uses",
          got == fx.EXPECTED, f"missing {sorted(fx.EXPECTED - got)} extra {sorted(got - fx.EXPECTED)}")
    check("every source is recorded as read",
          sorted((s["agent"], s["state"]) for s in sources)
          == sorted((a, "read") for a in ("claude", "cursor", "opencode", "codex", "gemini", "kiro")),
          str(sources))
    check("a source names its file home-relative, never the absolute home",
          all(s["file"].startswith("~/") and tmp not in s["file"] for s in sources), str(sources))
    by = {(r["agent"], r["name"]): r for r in rows if r.get("name")}
    check("a server switched off in its own config says so (opencode enabled=false, Kiro disabled)",
          by[("opencode", "eta-local")]["disabled"] is True and by[("kiro", "xi-local")]["disabled"] is True
          and by[("claude", "alpha-docs")]["disabled"] is False, str(by[("opencode", "eta-local")]))
    check("opencode's `environment` block is a keyed env like everyone else's",
          by[("opencode", "eta-local")]["key_in_env"] is True, str(by[("opencode", "eta-local")]))
    check("Codex's bearer variable and headers count as a key in a header",
          by[("codex", "iota-http")]["key_in_header"] is True, str(by[("codex", "iota-http")]))
    check("a password inside the URL counts as a key in the URL",
          by[("kiro", "nu-remote")]["key_in_url"] is True and by[("kiro", "nu-remote")]["target"]
          == "https://nu.example.com/mcp", str(by[("kiro", "nu-remote")]))
    check("a project-scoped Claude server keeps its scope, home-relative",
          by[("claude", "delta-project")]["scope"] == "project:~/work/alpha-web",
          by[("claude", "delta-project")]["scope"])


PROBE_OUTPUT = (
    "Checking MCP server health\u2026\n"
    "alpha-docs: https://docs.example.com/mcp (HTTP) - \u2714 Connected\n"
    "beta-events: https://events.example.com/sse (SSE) - \u2718 Failed to connect\n"
    "gamma-local: /opt/example/bin/gamma-mcp --api-key planted-probe-arg-8d7c6b5a - \u2714 Connected\n"
    "plugin:omega:omega-api: https://omega.example.com/mcp?token=planted-probe-query-4a3b2c1d (HTTP)"
    " - ! Needs authentication\n"
    "plugin:omega:omega-local: omega-mcp serve --secret planted-probe-plugin-arg-0f9e8d7c - \u2714 Connected\n"
)


def test_a_planted_secret_never_reaches_the_scan() -> None:
    import tempfile
    fx = _fixture()
    m = load("collectors/scan_mcp.py", "scan_mcp")
    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        fx.write_home(home)
        doc = m.scan(home, probe=lambda: (m.parse_probe(PROBE_OUTPUT), None, True))
    text = json.dumps(doc)
    check("no planted config value reaches the scan, in any format", fx.leaked(text) == [], str(fx.leaked(text)))
    check("nor a value the probe echoed in a command line or URL",
          "planted-probe" not in text, text[text.find("planted-probe") - 80:][:200] if "planted-probe" in text else "")
    check("nor the absolute home directory", tmp not in text, "")
    plugin = {r["name"]: r for r in doc["servers"] if str(r.get("scope", "")).startswith("plugin:")}
    check("a plugin server gets its wire transport from the probe line, not a guess",
          plugin.get("omega-api", {}).get("wire") == "streamable-http"
          and plugin.get("omega-local", {}).get("wire") == "stdio", str(plugin))
    claude = {r["name"]: r for r in doc["servers"] if r["agent"] == "claude" and r.get("scope") == "user"}
    check("Claude's verdict lands on Claude's own declarations",
          claude["alpha-docs"]["liveness"] == "connected" and claude["beta-events"]["liveness"] == "failed",
          str({k: v.get("liveness") for k, v in claude.items()}))
    check("the scan carries the time it ran and its sources",
          doc["scanned_at"].endswith("Z") and len(doc["sources"]) == 6, str(doc.get("sources")))


def test_absent_and_unreadable_sources_are_named_not_dropped() -> None:
    import tempfile
    fx = _fixture()
    m = load("collectors/scan_mcp.py", "scan_mcp")
    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        fx.write_home(home, skip=("gemini", "kiro"))
        (home / ".cursor/mcp.json").write_text('{"mcpServers": {"x": ', encoding="utf-8")
        (home / ".codex/config.toml").write_text('[mcp_servers.broken\ncommand = "planted', encoding="utf-8")
        doc = m.scan(home, probe=lambda: ({}, None, True))
    states = {s["agent"]: s["state"] for s in doc["sources"]}
    check("an absent config is recorded as absent",
          states.get("gemini") == "absent" and states.get("kiro") == "absent", str(states))
    check("a config that does not parse is recorded as unreadable",
          states.get("cursor") == "unreadable" and states.get("codex") == "unreadable", str(states))
    degraded = {d["source"]: d["reason"] for d in doc["degraded"]}
    check("an unreadable config degrades the scan, naming the file",
          any(k.startswith("cursor:") and "~/.cursor/mcp.json" in k for k in degraded)
          and any(k.startswith("codex:") for k in degraded), str(degraded))
    check("the reason names the error kind and never the file's content",
          "planted" not in json.dumps(doc) and all("Error" in v or "unreadable" in v for v in degraded.values()),
          str(degraded))
    check("the servers of the readable configs are still reported",
          {r["agent"] for r in doc["servers"] if r.get("name")} == {"claude", "opencode"},
          str({r["agent"] for r in doc["servers"]}))


if __name__ == "__main__":
    print("the MCP inventory — declarations as facts, verdicts attributed, never a token\n")
    for fn in (test_a_declaration_becomes_facts_and_never_a_value,
               test_claudes_verdict_is_parsed_and_attributed,
               test_the_projection_ids_rows_and_keeps_other_agents_unprobed,
               test_the_probe_timeout_is_configurable_and_generous_by_default,
               test_a_probe_that_runs_out_of_time_keeps_what_it_heard_and_says_so,
               test_a_probe_that_finishes_in_time_is_complete,
               test_the_scheduled_tick_carries_a_raised_limit,
               test_every_agent_format_is_read_with_its_wire_transport,
               test_a_planted_secret_never_reaches_the_scan,
               test_absent_and_unreadable_sources_are_named_not_dropped):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe agents' MCP servers are tracked here, and no query string ever will be\033[0m")
