#!/usr/bin/env python3
"""Every MCP server an agent on this machine is told to reach, and whether it does.

    scan_mcp.py store/raw/mcp.json                       # on demand: configs + Claude's probe
    scan_mcp.py store/raw/mcp.json --declarations-only   # the tick: configs only, no process started

WHY. The gateway that used to hold every MCP declaration in one place was
switched off on 2026-09-14; declarations now live in each agent's own config
(`~/.claude.json` and its project scopes, `~/.cursor/mcp.json`,
`~/.config/opencode/opencode.json`, `~/.codex/config.toml`, `~/.gemini/settings.json`,
`~/.kiro/settings/mcp.json`) plus the plugins Claude Code ships. Nothing
tracked them, so a server declared in one agent and not another, a server that
had stopped answering, or a token sitting in a URL were all invisible until a
session failed. The operator's rule: MCP works without the gateway AND is
             

WHAT IS NEVER WRITTEN. A URL is recorded as scheme+host+path with its QUERY
STRING and its USER-INFO (`user:password@`) DROPPED, arguments as a count, and a
header or env block as a boolean — `claude mcp list`
itself echoes `?token=…` verbatim, which is how one reached a transcript today.
This file reports THAT a key is in a URL (a finding) and never the key.

EVERY SOURCE IS ACCOUNTED FOR. `sources` records each config as `read`, `absent`
or `unreadable` (with the error's kind, never its message, which can quote the
line it failed on). An unreadable one also degrades the scan; an absent one does
not, since a machine without Cursor is not a partial scan — but
`machine.mcp.inventory` names both to its caller. Each declaration carries `wire`,
the MCP transport (stdio, streamable-http, sse) in the agent's own convention,
beside the older two-value `transport` the registry keeps.

Liveness comes from Claude Code's own probe (`claude mcp list`), parsed for the
servers this scan knows; Cursor and opencode have no equivalent probe, so their
rows say `liveness: not-probed` rather than borrowing Claude's answer for a
different process.

THE PROBE NEVER RUNS ON A SCHEDULE (lifecycle LC-04, LC-08). `claude mcp list`
starts every stdio server Claude Code knows (`npx …@latest` fetches included),
reads the operator's Claude credential and, when its access token has expired,
refreshes and rewrites it. Run from the 30-minute tick that was ~480 server
launches a day and a background job racing interactive sessions over the
operator's rotating login. So the tick passes `--declarations-only`: it reads the
configs, starts nothing, and carries forward the verdict of the last probe a
person or agent asked for (`full scan-mcp`, `machine.mcp.refresh`), stamped
`liveness_at` so an old verdict reads as old. Only an explicit request probes,
and the probe runs in a process group of its own that is killed as a whole on
exit or timeout, so no server it started outlives it.

THE PROBE IS SLOW BY DESIGN. `claude mcp list` health-checks every server it
knows before it prints, so a machine with many servers (plugins and claude.ai
connectors included) can take minutes. The limit defaults to
DEFAULT_PROBE_TIMEOUT seconds and is raised with OBSERVATORY_MCP_PROBE_TIMEOUT.
A probe that runs out of time is degraded, not fatal: the declarations are still
read from the configs, the servers the CLI reported before the cut keep their
verdict, and every other Claude server is `not-probed` — never `not-listed`,
because a cut-off list proves nothing about what it would have named.
"""
from __future__ import annotations
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths  # per-user configuration and private state
import atomic                                                       # noqa: E402
import osproc                                                       # noqa: E402

HOME = paths.source_path("mcp_config_root", paths.HOME / "disabled/mcp")
#: Where each agent keeps its user-level MCP declarations, relative to the home the
#: workspace names. The key is the agent as this scan (and the registry ids built
#: from it) has always spelled it; `mcp_inventory.AGENT_KINDS` maps it to the
#: runner-catalogue kind a host joins on.
AGENT_FILES = {
    "claude": ".claude.json",
    "cursor": ".cursor/mcp.json",
    "opencode": ".config/opencode/opencode.json",
    "codex": ".codex/config.toml",
    "gemini": ".gemini/settings.json",
    "kiro": ".kiro/settings/mcp.json",
}


def sources_for(home: pathlib.Path) -> dict[str, pathlib.Path]:
    return {agent: home / rel for agent, rel in AGENT_FILES.items()}


SOURCES = sources_for(HOME)
#: The observatory's own server, which must be declared somewhere or its nine
#: tools are unreachable to every agent on the machine.
OWN_SERVER = "observatory"
#: Seconds `claude mcp list` may take. 60 and then 120 were measured too short on
#: machines with many servers; the CLI checks each one before printing.
DEFAULT_PROBE_TIMEOUT = 180
PROBE_TIMEOUT_ENV = "OBSERVATORY_MCP_PROBE_TIMEOUT"
PROBE_TIMEOUT_MAX = 3600
#: Seconds the probe's process group gets between SIGTERM and SIGKILL once the
#: probe has answered or run out of time. MCP servers the CLI started belong to
#: that group; one that ignores SIGTERM is killed after this.
PROBE_GRACE = 2.0
#: Days a carried-forward verdict stays attributed. Older than this the row says
#: `not-probed` again: a week-old "connected" is a guess, not a measurement.
CARRY_DAYS = 7


def probe_timeout() -> tuple[float, str | None]:
    """(seconds, note): the configured limit, or the default with a note saying why."""
    raw = os.environ.get(PROBE_TIMEOUT_ENV, "").strip()
    if not raw:
        return float(DEFAULT_PROBE_TIMEOUT), None
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not (1 <= value <= PROBE_TIMEOUT_MAX):   # NaN fails this comparison too
        return (float(DEFAULT_PROBE_TIMEOUT),
                f"{PROBE_TIMEOUT_ENV}={raw!r} is not a number of seconds between 1 and "
                f"{PROBE_TIMEOUT_MAX}; used {DEFAULT_PROBE_TIMEOUT}")
    return value, None


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_url(url: str) -> tuple[str, bool]:
    """(url without its query and without any user:password, whether either was present).

    The user-info part of a URL is a credential exactly as a query string is:
    a URL of the form `https://user:<password>` + `@host/mcp` reached this file intact until 2026-09-30,
    because only the query was dropped."""
    try:
        s = urlsplit(url)
        host = s.hostname or ""
        port = s.port
    except ValueError:
        # Unparseable: keep only the scheme rather than guess which part of the
        # rest is safe to show, and count it as keyed — the safe side.
        scheme = url.split("://", 1)[0] if "://" in url else ""
        return (scheme + "://…" if scheme.isalpha() else "…"), True
    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    keyed = bool(s.query) or s.username is not None or s.password is not None
    return urlunsplit((s.scheme, netloc, s.path, "", "")), keyed


#: The `type` spellings the six agents use, mapped to the three MCP transports.
WIRE_BY_TYPE = {
    "stdio": "stdio", "local": "stdio",
    "http": "streamable-http", "streamable-http": "streamable-http",
    "streamablehttp": "streamable-http", "remote": "streamable-http",
    "sse": "sse",
}


def wire_of(agent: str, cfg: dict) -> str | None:
    """The MCP transport a declaration uses: stdio, streamable-http or sse.

    An explicit `type` wins. Otherwise each agent's own convention decides: Gemini
    CLI reads `httpUrl` as streamable HTTP and a bare `url` as SSE; every other
    agent here treats a bare `url` as streamable HTTP (Cursor and opencode fall
    back to SSE on their own, which is the client's negotiation, not the
    declaration). A `command` is stdio."""
    declared = str(cfg.get("type") or "").strip().lower()
    if declared in WIRE_BY_TYPE:
        return WIRE_BY_TYPE[declared]
    if agent == "gemini":
        if cfg.get("httpUrl"):
            return "streamable-http"
        if cfg.get("url"):
            return "sse"
    if cfg.get("url") or cfg.get("httpUrl"):
        return "streamable-http"
    if cfg.get("command"):
        return "stdio"
    return None


def classify(name: str, cfg: dict, agent: str = "claude") -> dict:
    """One declaration -> the facts a registry can hold. No values.

    Only names and booleans leave this function: the URL loses its query and its
    user-info, a header block and an environment block become "is there a key",
    and arguments become a count. That is the whole redaction, and it happens
    here, at the reader, so nothing downstream ever holds a value to leak."""
    url = cfg.get("url") or cfg.get("httpUrl") or ""
    cmd = cfg.get("command") or ""
    # opencode spells a stdio server as `command: ["npx", "-y", …]`; Claude and
    # Cursor as `command` + `args`. One shape here: the executable, then args.
    extra_args: list = []
    if isinstance(cmd, list):
        extra_args = [str(a) for a in cmd[1:]]
        cmd = str(cmd[0]) if cmd else ""
    cmd = str(cmd)
    transport = (cfg.get("type") or ("http" if url else "stdio")).lower()
    if transport in ("remote", "sse", "streamable-http"):
        transport = "http"
    if transport == "local":
        transport = "stdio"
    target, key_in_url = (safe_url(str(url)) if url else (pathlib.Path(cmd).name if cmd else "", False))
    # Codex keeps static headers in `http_headers`, headers filled from the
    # environment in `env_http_headers`, and a bearer token's VARIABLE NAME in
    # `bearer_token_env_var`; any of them means the key travels in a header.
    headers = {**(cfg.get("headers") if isinstance(cfg.get("headers"), dict) else {}),
               **(cfg.get("http_headers") if isinstance(cfg.get("http_headers"), dict) else {}),
               **(cfg.get("env_http_headers") if isinstance(cfg.get("env_http_headers"), dict) else {})}
    keyed_header = bool(cfg.get("bearer_token_env_var")) or any(
        k.lower() in ("authorization", "x-api-key", "x-observatory-token")
        or "key" in k.lower() or "token" in k.lower() for k in map(str, headers))
    # opencode names its block `environment`; the others `env`.
    env = {**(cfg.get("env") if isinstance(cfg.get("env"), dict) else {}),
           **(cfg.get("environment") if isinstance(cfg.get("environment"), dict) else {})}
    keyed_env = any(re.search(r"(KEY|TOKEN|SECRET|PASS)", str(k), re.I) for k in env)
    args = extra_args + [str(a) for a in (cfg.get("args") or [])]
    return {
        "name": name, "transport": transport, "wire": wire_of(agent, cfg), "target": target,
        "command": pathlib.Path(cmd).name if cmd else None,
        "args_count": len(args),
        "key_in_url": key_in_url,
        "key_in_header": keyed_header, "key_in_env": keyed_env,
        "command_present": (bool(shutil.which(cmd) or pathlib.Path(cmd).is_file())
                            if cmd else None),
        # Switched off in its own config: opencode `enabled: false`, Codex
        # `enabled = false`, Kiro and Gemini `disabled: true`.
        "disabled": cfg.get("enabled") is False or cfg.get("disabled") is True,
    }


def _tilde(path: pathlib.Path | str, home: pathlib.Path) -> str:
    """A path under the scanned home, spelled `~/…`, so no absolute home reaches a file."""
    text, root = str(path), str(home)
    if text == root:
        return "~"
    return "~/" + text[len(root):].lstrip("/") if text.startswith(root.rstrip("/") + "/") else text


def _load(agent: str, path: pathlib.Path) -> dict:
    if path.suffix == ".toml":
        import tomllib
        with path.open("rb") as fh:
            return tomllib.load(fh)
    return json.loads(path.read_text(encoding="utf-8"))


def _table(agent: str, doc: dict) -> dict:
    if agent == "opencode":
        table = doc.get("mcp")
    elif agent == "codex":
        table = doc.get("mcp_servers")
    else:
        table = doc.get("mcpServers")
    return table if isinstance(table, dict) else {}


def read_sources(home: pathlib.Path | None = None) -> tuple[list[dict], list[dict]]:
    """(declarations, sources): every server each agent's config names, and what
    became of each config file — `read`, `absent` or `unreadable`.

    A file that is missing or does not parse is RECORDED, never skipped in
    silence: an inventory that quietly lacks one agent reads as that agent
    declaring nothing. The reason names the error's kind only; a parser's message
    can quote the line it choked on, and that line can be a key."""
    home = HOME if home is None else home
    rows: list[dict] = []
    sources: list[dict] = []
    for agent, path in sources_for(home).items():
        shown = _tilde(path, home)
        if not path.is_file():
            sources.append({"agent": agent, "file": shown, "state": "absent"})
            continue
        try:
            doc = _load(agent, path)
            if not isinstance(doc, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as exc:     # JSONDecodeError and TOMLDecodeError are ValueErrors
            reason = f"unreadable: {type(exc).__name__}"
            sources.append({"agent": agent, "file": shown, "state": "unreadable", "reason": reason})
            rows.append({"agent": agent, "name": None, "error": type(exc).__name__, "declared_in": shown})
            continue
        sources.append({"agent": agent, "file": shown, "state": "read"})
        for name, cfg in _table(agent, doc).items():
            if isinstance(cfg, dict):
                rows.append({**classify(str(name), cfg, agent), "agent": agent, "scope": "user",
                             "declared_in": shown})
        if agent == "claude":
            for proj, pcfg in (doc.get("projects") or {}).items():
                if not isinstance(pcfg, dict):
                    continue
                for name, cfg in (pcfg.get("mcpServers") or {}).items():
                    if isinstance(cfg, dict):
                        rows.append({**classify(str(name), cfg, agent), "agent": agent,
                                     "scope": f"project:{_tilde(proj, home)}",
                                     "declared_in": f"{shown}#projects"})
    return rows, sources


def read_declarations() -> list[dict]:
    """The declarations alone, for callers that predate `read_sources`."""
    return read_sources()[0]


# `plugin:<plugin>:<server>: https://… - ✔ Connected` — the name itself holds
# colons, so the name is everything before the first ": " (colon-space), not
# the first colon.
LINE = re.compile(r"^(?P<name>.+?): (?P<target>.*?) - (?P<mark>✔|✘|!) (?P<status>.+)$")


def _text(value) -> str:
    """TimeoutExpired carries raw bytes even when the run asked for text."""
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def parse_probe(text: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for raw in text.splitlines():
        m = LINE.match(raw.strip())
        if not m:
            continue
        name = m.group("name").strip()
        mark = m.group("mark")
        status = {"✔": "connected", "✘": "failed", "!": "needs-auth"}[mark]
        plugin = None
        if name.startswith("plugin:"):
            _, plugin, name = name.split(":", 2)
        elif name.startswith("claude.ai "):
            plugin, name = "claude.ai", name[len("claude.ai "):]
        out[name] = {"status": status, "plugin": plugin,
                     "detail": m.group("status").strip()[:60],
                     "wire": probe_wire(m.group("target"))}
    return out


def probe_wire(target: str) -> str:
    """The transport a `claude mcp list` line shows, read and then thrown away.

    The CLI prints a remote server as `<url> (HTTP)` or `<url> (SSE)` and a stdio
    one as its command line. Only the transport is kept: the target itself can
    carry `?token=…` or a `--api-key` argument, which is exactly what must not
    reach a file."""
    t = target.strip()
    if t.endswith("(SSE)"):
        return "sse"
    if t.endswith("(HTTP)") or re.match(r"^https?://", t):
        return "streamable-http"
    return "stdio"


def _group_alive(pgid: int) -> bool:
    return osproc.group_alive(pgid)


def reap_group(pgid: int, grace: float = PROBE_GRACE) -> None:
    """SIGTERM the whole group, then SIGKILL what is still there after `grace`.

    The group outlives its leader: servers `claude mcp list` started stay in it
    after the CLI exits, reparented to launchd (measured: firebase-tools held
    165 MB for two minutes after the probe answered). Killing the group, not the
    leader, is what leaves nothing behind."""
    osproc.stop_group(pgid, grace)


def run_reaped(argv: list[str], timeout: float) -> tuple[int | None, str]:
    """(exit code or None on timeout, everything printed) for a command run in a
    process group of its own that is reaped on every path.

    Output goes to a private temporary file, not a pipe: a server the command
    started inherits its stdout, and a pipe would then stay open — and the read
    would block — for as long as that server lives. Waiting on the leader's exit
    and then killing the group ends the probe the moment the CLI has answered."""
    import tempfile
    with tempfile.TemporaryFile() as sink:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=sink,
                                stderr=subprocess.STDOUT, **osproc.new_group())
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            code = None
        finally:
            reap_group(proc.pid)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        sink.seek(0)
        return code, _text(sink.read())


def claude_probe(timeout: float | None = None) -> tuple[dict[str, dict], str | None, bool]:
    """(name -> {status, plugin, detail}, why degraded or None, whether the list is complete).

    On request only — never from the tick (module docstring)."""
    if not shutil.which("claude"):
        return {}, "claude CLI not on PATH", False
    if timeout is None:
        timeout, _ = probe_timeout()
    try:
        code, text = run_reaped(["claude", "mcp", "list"], timeout)
    except OSError as exc:
        return {}, f"claude mcp list: {type(exc).__name__}", False
    if code is None:
        heard = parse_probe(text)
        return heard, (f"claude mcp list did not finish within {timeout:g} s (it health-checks every "
                       f"server before printing); {len(heard)} server(s) reported before the cut, the "
                       f"rest are not-probed — raise {PROBE_TIMEOUT_ENV} (seconds) on this machine"), False
    out = parse_probe(text)
    if not out:
        return {}, "claude mcp list answered with nothing this scan could parse", False
    return out, None, True


def attribute_liveness(rows: list[dict], probe: dict[str, dict], complete: bool) -> None:
    """Give each declaration Claude's verdict where Claude gave one, and say why not elsewhere."""
    for r in rows:
        if r.get("name") is None:
            continue
        if r["agent"] == "claude" and r["name"] in probe:
            r["liveness"] = probe[r["name"]]["status"]
            r["liveness_detail"] = probe[r["name"]]["detail"]
        elif r["agent"] == "claude" and probe and complete and r["scope"].startswith("project:"):
            # `claude mcp list` reports only the servers active in ITS cwd; a
            # server scoped to another project is out of view, not down.
            r["liveness"] = "not-probed"
        elif r["agent"] == "claude" and probe and complete:
            r["liveness"] = "not-listed"
        else:
            r["liveness"] = "not-probed"


def _age_days(stamp: str | None) -> float:
    try:
        at = datetime.strptime(stamp or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - at).total_seconds() / 86400


def carried(previous: dict | None) -> tuple[dict[str, dict], bool, str | None]:
    """(verdicts, complete, probed_at) of the last on-demand probe, while it is younger
    than CARRY_DAYS; ({}, False, None) when there is none or it is too old."""
    probe = (previous or {}).get("probe") if isinstance(previous, dict) else None
    if not isinstance(probe, dict) or not isinstance(probe.get("servers"), dict):
        return {}, False, None
    at = probe.get("probed_at")
    if _age_days(at) > CARRY_DAYS:
        return {}, False, None
    servers = {str(k): v for k, v in probe["servers"].items()
               if isinstance(v, dict) and v.get("status") in ("connected", "failed", "needs-auth")}
    return servers, bool(probe.get("complete")), at


def scan(home: pathlib.Path | None = None, probe=None, *, declarations_only: bool = False,
         previous: dict | None = None) -> dict:
    """The whole scan as one document: declarations, liveness, sources, degraded.

    `probe` is the liveness source, `claude_probe` unless a caller hands in its
    own (the tests do, so no suite ever runs the real CLI). With
    `declarations_only` nothing is started: the verdicts come from `previous`,
    the last document this collector wrote, and keep that probe's time."""
    home = HOME if home is None else home
    rows, sources = read_sources(home)
    note = None
    degraded: list[dict] = []
    if declarations_only:
        heard, complete, probed_at = carried(previous)
        probe_doc = {"state": "carried" if probed_at else "never", "probed_at": probed_at,
                     "complete": complete, "servers": heard}
    else:
        if probe is None:
            timeout, note = probe_timeout()
            probe = lambda: claude_probe(timeout)                  # noqa: E731
        heard, why, complete = probe()
        probed_at = now()
        probe_doc = {"state": "probed", "probed_at": probed_at, "complete": complete, "servers": heard}
        degraded += [{"source": PROBE_TIMEOUT_ENV, "reason": note}] if note else []
        degraded += [{"source": "claude mcp list", "reason": why}] if why else []
    # An unreadable config is a hole in the inventory, so it degrades the scan. An
    # ABSENT one does not: it is an agent that is not set up here, and a service
    # reporting itself degraded for every agent a machine lacks would never be
    # healthy. It is still recorded in `sources`, and `machine.mcp.inventory`
    # names it to its caller.
    degraded += [{"source": f"{s['agent']}:{s['file']}", "reason": s["reason"]}
                 for s in sources if s["state"] == "unreadable"]
    attribute_liveness(rows, heard, complete)
    if probed_at:
        for r in rows:
            if r.get("name") is not None and r.get("liveness") not in (None, "not-probed"):
                r["liveness_at"] = probed_at
    # Servers Claude reaches that no config here declares: plugin-shipped and
    # claude.ai connectors. Recorded as their own rows so the inventory is the
    # whole picture, never a subset that looks whole.
    declared = {r["name"] for r in rows if r.get("name")}
    for name, pr in heard.items():
        if name not in declared and pr["plugin"]:
            rows.append({"name": name, "agent": "claude",
                         "scope": f"plugin:{pr['plugin']}" if pr["plugin"] != "claude.ai" else "claude.ai",
                         "declared_in": "plugin" if pr["plugin"] != "claude.ai" else "claude.ai connector",
                         "transport": None, "wire": pr.get("wire"), "target": None, "command": None,
                         "args_count": 0, "key_in_url": False, "key_in_header": False,
                         "key_in_env": False, "command_present": None, "disabled": False,
                         "liveness": pr["status"], "liveness_detail": pr["detail"],
                         **({"liveness_at": probed_at} if probed_at else {})})
    rows.sort(key=lambda r: (r.get("agent") or "", r.get("name") or ""))
    return {"scanned_at": now(), "servers": rows, "sources": sources,
            "own_server": OWN_SERVER,
            "own_declared": any(r.get("name") == OWN_SERVER for r in rows),
            "probe": probe_doc,
            "degraded": degraded}


def main(argv: list[str]) -> int:
    import configuration
    if not configuration.enabled("mcp"):
        print("mcp: not configured (integration disabled)")
        return 0
    flags = [a for a in argv[1:] if a.startswith("--")]
    positional = [a for a in argv[1:] if not a.startswith("--")]
    if len(positional) != 1 or set(flags) - {"--declarations-only"}:
        print(__doc__, file=sys.stderr)
        return 2
    out = pathlib.Path(positional[0])
    declarations_only = "--declarations-only" in flags
    previous = None
    if declarations_only:
        try:
            previous = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
    doc = scan(declarations_only=declarations_only, previous=previous)
    rows, degraded = doc["servers"], doc["degraded"]
    atomic.write_json(out, doc)
    by = {}
    for r in rows:
        by[r.get("liveness", "?")] = by.get(r.get("liveness", "?"), 0) + 1
    # The liveness tally follows a dash only when there is one: with no
    # declaration the line used to end in a dangling "— ".
    print(f"mcp: {len(rows)} declaration(s) across "
          f"{len({r.get('agent') for r in rows})} agent(s)"
          + (" — " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())) if by else ""))
    probe = doc["probe"]
    if probe["state"] != "probed":
        print(f"  liveness: {'carried from the probe of ' + probe['probed_at'] if probe['probed_at'] else 'never probed'}"
              " — the tick starts no server; `project-observatory full scan-mcp` probes on request")
    for d in degraded:
        print(f"  degraded {d['source']}: {d['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
