#!/usr/bin/env python3
"""Every MCP server an agent on this machine is told to reach, and whether it does.

    scan_mcp.py store/raw/mcp.json

WHY. The gateway that used to hold every MCP declaration in one place was
switched off on 2026-09-14; declarations now live in each agent's own config
(`~/.claude.json` and its project scopes, `~/.cursor/mcp.json`,
`~/.config/opencode/opencode.json`) plus the plugins Claude Code ships. Nothing
tracked them, so a server declared in one agent and not another, a server that
had stopped answering, or a token sitting in a URL were all invisible until a
session failed. The operator's rule: MCP works without the gateway AND is
             

WHAT IS NEVER WRITTEN. A URL is recorded as scheme+host+path with its QUERY
STRING DROPPED, and a header or env block as a boolean — `claude mcp list`
itself echoes `?token=…` verbatim, which is how one reached a transcript today.
This file reports THAT a key is in a URL (a finding) and never the key.

Liveness comes from Claude Code's own probe (`claude mcp list`), parsed for the
servers this scan knows; Cursor and opencode have no equivalent probe, so their
rows say `liveness: not-probed` rather than borrowing Claude's answer for a
different process.

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
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths  # per-user configuration and private state
import atomic                                                       # noqa: E402

HOME = paths.source_path("mcp_config_root", paths.HOME / "disabled/mcp")
SOURCES = {
    "claude": HOME / ".claude.json",
    "cursor": HOME / ".cursor/mcp.json",
    "opencode": HOME / ".config/opencode/opencode.json",
}
#: The observatory's own server, which must be declared somewhere or its nine
#: tools are unreachable (credentials audit G14).
OWN_SERVER = "observatory"
#: Seconds `claude mcp list` may take. 60 and then 120 were measured too short on
#: machines with many servers; the CLI checks each one before printing.
DEFAULT_PROBE_TIMEOUT = 180
PROBE_TIMEOUT_ENV = "OBSERVATORY_MCP_PROBE_TIMEOUT"
PROBE_TIMEOUT_MAX = 3600


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
    """(url without its query, whether a query was present)."""
    try:
        s = urlsplit(url)
    except ValueError:
        return url.split("?")[0], "?" in url
    return urlunsplit((s.scheme, s.netloc, s.path, "", "")), bool(s.query)


def classify(name: str, cfg: dict) -> dict:
    """One declaration -> the facts a registry can hold. No values."""
    url = cfg.get("url") or ""
    cmd = cfg.get("command") or ""
    # opencode spells a stdio server as `command: ["npx", "-y", …]`; Claude and
    # Cursor as `command` + `args`. One shape here: the executable, then args.
    extra_args: list = []
    if isinstance(cmd, list):
        extra_args = [str(a) for a in cmd[1:]]
        cmd = str(cmd[0]) if cmd else ""
    transport = (cfg.get("type") or ("http" if url else "stdio")).lower()
    if transport in ("remote", "sse", "streamable-http"):
        transport = "http"
    target, key_in_url = (safe_url(url) if url else (pathlib.Path(cmd).name if cmd else "", False))
    headers = cfg.get("headers") or {}
    keyed_header = any(k.lower() in ("authorization", "x-api-key", "x-observatory-token")
                       or "key" in k.lower() or "token" in k.lower() for k in headers)
    env = cfg.get("env") or {}
    keyed_env = any(re.search(r"(KEY|TOKEN|SECRET|PASS)", k, re.I) for k in env)
    args = extra_args + [str(a) for a in (cfg.get("args") or [])]
    return {
        "name": name, "transport": transport, "target": target,
        "command": pathlib.Path(cmd).name if cmd else None,
        "args_count": len(args),
        "key_in_url": key_in_url,
        "key_in_header": keyed_header, "key_in_env": keyed_env,
        "command_present": (bool(shutil.which(cmd) or pathlib.Path(cmd).is_file())
                            if cmd else None),
    }


def read_declarations() -> list[dict]:
    rows: list[dict] = []
    for agent, path in SOURCES.items():
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            rows.append({"agent": agent, "name": None, "error": f"{type(exc).__name__}"})
            continue
        table = doc.get("mcpServers") if agent != "opencode" else doc.get("mcp")
        for name, cfg in (table or {}).items():
            if isinstance(cfg, dict):
                rows.append({**classify(name, cfg), "agent": agent, "scope": "user",
                             "declared_in": str(path).replace(str(HOME), "~")})
        if agent == "claude":
            for proj, pcfg in (doc.get("projects") or {}).items():
                for name, cfg in (pcfg.get("mcpServers") or {}).items():
                    if isinstance(cfg, dict):
                        rows.append({**classify(name, cfg), "agent": agent,
                                     "scope": f"project:{proj.replace(str(HOME), '~')}",
                                     "declared_in": "~/.claude.json#projects"})
    return rows


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
                     "detail": m.group("status").strip()[:60]}
    return out


def claude_probe(timeout: float | None = None) -> tuple[dict[str, dict], str | None, bool]:
    """(name -> {status, plugin, detail}, why degraded or None, whether the list is complete)."""
    if not shutil.which("claude"):
        return {}, "claude CLI not on PATH", False
    if timeout is None:
        timeout, _ = probe_timeout()
    try:
        p = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        heard = parse_probe(_text(exc.stdout) + _text(exc.stderr))
        return heard, (f"claude mcp list did not finish within {timeout:g} s (it health-checks every "
                       f"server before printing); {len(heard)} server(s) reported before the cut, the "
                       f"rest are not-probed — raise {PROBE_TIMEOUT_ENV} (seconds) on this machine"), False
    except OSError as exc:
        return {}, f"claude mcp list: {type(exc).__name__}", False
    out = parse_probe(p.stdout + p.stderr)
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


def main(argv: list[str]) -> int:
    import configuration
    if not configuration.enabled("mcp"):
        print("mcp: not configured (integration disabled)")
        return 0
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    rows = read_declarations()
    timeout, note = probe_timeout()
    probe, why, complete = claude_probe(timeout)
    degraded = [{"source": PROBE_TIMEOUT_ENV, "reason": note}] if note else []
    degraded += [{"source": "claude mcp list", "reason": why}] if why else []
    attribute_liveness(rows, probe, complete)
    # Servers Claude reaches that no config here declares: plugin-shipped and
    # claude.ai connectors. Recorded as their own rows so the inventory is the
    # whole picture, never a subset that looks whole.
    declared = {r["name"] for r in rows if r.get("name")}
    for name, pr in probe.items():
        if name not in declared and pr["plugin"]:
            rows.append({"name": name, "agent": "claude",
                         "scope": f"plugin:{pr['plugin']}" if pr["plugin"] != "claude.ai" else "claude.ai",
                         "declared_in": "plugin" if pr["plugin"] != "claude.ai" else "claude.ai connector",
                         "transport": None, "target": None, "command": None, "args_count": 0,
                         "key_in_url": False, "key_in_header": False, "key_in_env": False,
                         "command_present": None,
                         "liveness": pr["status"], "liveness_detail": pr["detail"]})
    rows.sort(key=lambda r: (r.get("agent") or "", r.get("name") or ""))
    atomic.write_json(argv[1], {"scanned_at": now(), "servers": rows,
                                "own_server": OWN_SERVER,
                                "own_declared": any(r.get("name") == OWN_SERVER for r in rows),
                                "degraded": degraded})
    by = {}
    for r in rows:
        by[r.get("liveness", "?")] = by.get(r.get("liveness", "?"), 0) + 1
    print(f"mcp: {len(rows)} declaration(s) across "
          f"{len({r.get('agent') for r in rows})} agent(s) — "
          + ", ".join(f"{k} {v}" for k, v in sorted(by.items())))
    for d in degraded:
        print(f"  degraded {d['source']}: {d['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
