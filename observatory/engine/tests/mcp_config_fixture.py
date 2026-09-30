#!/usr/bin/env python3
"""Planted agent MCP configs, one per format the scan reads, each carrying secrets.

Every value a config can hold that is, or can carry, a credential is filled with
a distinct planted string: URL query strings, the user and password part of a
URL, header values, environment values, command arguments and a Codex bearer
variable. A test that dumps the scan or the inventory and finds none of
`PLANTED` in it has proved the redaction happens at the reader, for every
format, rather than trusted a reviewer's reading of the code.

Names are invented (`alpha-docs`, `beta-search`, …); nothing here describes a real
machine. `write_home(path)` lays the files out the way each agent keeps them
under a home directory and returns what it planted.
"""
from __future__ import annotations
import json
from pathlib import Path

#: Each planted value is unique, so a leak names its own origin.
PLANTED = {
    "claude-query": "planted-claude-query-7f3a91c2",
    "claude-userinfo": "planted-claude-password-2b8e44d0",
    "claude-header": "planted-claude-header-9c1d02ee",
    "claude-env": "planted-claude-env-5e6f7a80",
    "claude-arg": "planted-claude-arg-0a1b2c3d",
    "claude-project-header": "planted-claude-project-header-4d5e6f70",
    "cursor-env": "planted-cursor-env-11aa22bb",
    "cursor-query": "planted-cursor-query-33cc44dd",
    "opencode-header": "planted-opencode-header-55ee66ff",
    "opencode-env": "planted-opencode-env-77aa88bb",
    "codex-env": "planted-codex-env-99cc00dd",
    "codex-header": "planted-codex-header-12ab34cd",
    "codex-query": "planted-codex-query-56ef78ab",
    "gemini-header": "planted-gemini-header-9a8b7c6d",
    "gemini-env": "planted-gemini-env-5f4e3d2c",
    "kiro-env": "planted-kiro-env-1b2a3c4d",
    "kiro-userinfo": "planted-kiro-password-6e5f7a8b",
}

#: (agent kind, server name, wire transport) the scan must report for this home.
EXPECTED = {
    ("claude", "alpha-docs", "streamable-http"),
    ("claude", "beta-events", "sse"),
    ("claude", "gamma-local", "stdio"),
    ("claude", "delta-project", "streamable-http"),
    ("cursor", "alpha-docs", "streamable-http"),
    ("cursor", "epsilon-tool", "stdio"),
    ("opencode", "zeta-remote", "streamable-http"),
    ("opencode", "eta-local", "stdio"),
    ("codex", "theta-stdio", "stdio"),
    ("codex", "iota-http", "streamable-http"),
    ("gemini", "kappa-sse", "sse"),
    ("gemini", "lambda-http", "streamable-http"),
    ("gemini", "mu-local", "stdio"),
    ("kiro", "nu-remote", "streamable-http"),
    ("kiro", "xi-local", "stdio"),
}


def _userinfo_url(user: str, password: str, rest: str) -> str:
    """A URL carrying `user:password@`, assembled at run time so the source file
    itself holds no credential-shaped URL for the release privacy gate to flag."""
    return "https://" + user + ":" + password + "@" + rest


def _json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1), encoding="utf-8")


def write_home(home: Path, *, skip: tuple[str, ...] = ()) -> dict[str, str]:
    """Write every agent's config under `home`, except the agents in `skip`."""
    p = PLANTED
    if "claude" not in skip:
        _json(home / ".claude.json", {
            "numStartups": 3,
            "mcpServers": {
                "alpha-docs": {"type": "http",
                               "url": _userinfo_url("alpha-user", p["claude-userinfo"], "docs.example.com/mcp")
                                      + f"?token={p['claude-query']}",
                               "headers": {"Authorization": f"Bearer {p['claude-header']}"}},
                "beta-events": {"type": "sse", "url": "https://events.example.com/sse"},
                "gamma-local": {"type": "stdio", "command": "/opt/example/bin/gamma-mcp",
                                "args": ["--api-key", p["claude-arg"]],
                                "env": {"GAMMA_TOKEN": p["claude-env"]}},
            },
            "projects": {
                str(home / "work/alpha-web"): {
                    "mcpServers": {
                        "delta-project": {"type": "http", "url": "https://delta.example.com/mcp",
                                          "headers": {"X-Api-Key": p["claude-project-header"]}},
                    }},
            },
        })
    if "cursor" not in skip:
        _json(home / ".cursor/mcp.json", {"mcpServers": {
            "alpha-docs": {"url": f"https://docs.example.com/mcp?key={p['cursor-query']}"},
            "epsilon-tool": {"command": "npx", "args": ["-y", "epsilon-mcp"],
                             "env": {"EPSILON_SECRET": p["cursor-env"]}},
        }})
    if "opencode" not in skip:
        _json(home / ".config/opencode/opencode.json", {"mcp": {
            "zeta-remote": {"type": "remote", "url": "https://zeta.example.com/mcp",
                            "headers": {"Authorization": f"Bearer {p['opencode-header']}"}},
            "eta-local": {"type": "local", "command": ["uvx", "eta-mcp"],
                          "environment": {"ETA_KEY": p["opencode-env"]}, "enabled": False},
        }})
    if "codex" not in skip:
        path = home / ".codex/config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'model = "example-model"\n\n'
            '[mcp_servers.theta-stdio]\n'
            'command = "theta-mcp"\n'
            'args = ["serve"]\n'
            f'env = {{ THETA_TOKEN = "{p["codex-env"]}" }}\n\n'
            '[mcp_servers.iota-http]\n'
            f'url = "https://iota.example.com/mcp?sig={p["codex-query"]}"\n'
            'bearer_token_env_var = "IOTA_BEARER"\n'
            f'http_headers = {{ "X-Iota" = "{p["codex-header"]}" }}\n',
            encoding="utf-8")
    if "gemini" not in skip:
        _json(home / ".gemini/settings.json", {"theme": "Default", "mcpServers": {
            "kappa-sse": {"url": "https://kappa.example.com/sse",
                          "headers": {"Authorization": f"Bearer {p['gemini-header']}"}},
            "lambda-http": {"httpUrl": "https://lambda.example.com/mcp"},
            "mu-local": {"command": "python3", "args": ["-m", "mu_mcp"],
                         "env": {"MU_API_KEY": p["gemini-env"]}},
        }})
    if "kiro" not in skip:
        _json(home / ".kiro/settings/mcp.json", {"mcpServers": {
            "nu-remote": {"url": _userinfo_url("nu", p["kiro-userinfo"], "nu.example.com/mcp")},
            "xi-local": {"command": "xi-mcp", "env": {"XI_TOKEN": p["kiro-env"]}, "disabled": True},
        }})
    return dict(PLANTED)


def leaked(text: str) -> list[str]:
    """Which planted values appear in `text`, by their label."""
    return sorted(label for label, value in PLANTED.items() if value in text)


def make_workspace(home: Path, *, mcp_root: Path | None = None) -> None:
    """A minimal initialized workspace; with `mcp_root`, the mcp integration reads it."""
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "registry").mkdir(exist_ok=True)
    (home / "store/raw").mkdir(parents=True, exist_ok=True)
    (home / "workspace.json").write_text(json.dumps(
        {"format_version": 1, "minimum_reader": "0.2.0", "minimum_writer": "0.2.0"}))
    settings = {"schema_version": 1, "sources": {}, "integrations": {}, "features": {}}
    if mcp_root is not None:
        settings["sources"]["mcp_config_root"] = str(mcp_root)
        settings["integrations"]["mcp"] = True
    (home / "config/settings.json").write_text(json.dumps(settings))


#: A stand-in `claude` CLI. It prints the probe lines below and, when the
#: `OBSERVATORY_TEST_TRACE_FILE` variable names a file, writes the trace context
#: it was started with there — the proof that a job hands its span to the process
#: it calls. `OBSERVATORY_TEST_CLAUDE_SLEEP` makes it slow, for cancellation.
STUB_CLAUDE = """#!{py}
import os, sys, time
trace = os.environ.get("OBSERVATORY_TEST_TRACE_FILE")
if trace:
    with open(trace, "w") as fh:
        fh.write((os.environ.get("TRACEPARENT") or "") + "\\n" + (os.environ.get("TRACESTATE") or ""))
time.sleep(float(os.environ.get("OBSERVATORY_TEST_CLAUDE_SLEEP") or 0))
print("alpha-docs: https://docs.example.com/mcp (HTTP) - \\u2714 Connected", flush=True)
print("beta-events: https://events.example.com/sse (SSE) - \\u2718 Failed to connect", flush=True)
print("gamma-local: /opt/example/bin/gamma-mcp --api-key planted-probe-arg-8d7c6b5a - \\u2714 Connected", flush=True)
print("plugin:omega:omega-api: https://omega.example.com/mcp?token=planted-probe-query-4a3b2c1d (HTTP)"
      " - ! Needs authentication", flush=True)
"""


def stub_claude(directory: Path) -> Path:
    """Write the stand-in `claude` into `directory` and return its path."""
    import sys
    directory.mkdir(parents=True, exist_ok=True)
    fake = directory / "claude"
    fake.write_text(STUB_CLAUDE.format(py=sys.executable), encoding="utf-8")
    fake.chmod(0o755)
    return fake
