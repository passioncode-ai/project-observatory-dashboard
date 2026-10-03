#!/usr/bin/env python3
"""Findings for the rule that every credential an agent uses comes from the vault.

Three things the rule can be broken by, each measured, never inferred from a name:

* `secret.seen_in_agent_memory` — a write into agent memory carried one of the
  workspace's KNOWN secret values. The write was stored redacted
  (`memory_redact`), so nothing leaked into the store; but the value was in an
  agent's hands and its transcript, which is an exposure to record.
* `agent.secret_outside_vault` — a project an agent works for holds a secret in its
  own `.env` that the vault does not hold. The agent then reads it from the file,
  which is the copy the rule exists to remove.
* `agent.secret_fallback_used` — a run for such a project resolved a name from a
  `.env` instead of the vault (`use_secret.py` records where each value came
  from). `--vault-only` would have refused it.

WHICH PROJECTS ARE AGENTS' is measured two ways: a project a workflow declared a
credential for (agent memory), and a project whose local folder carries a Fabric
agent manifest (`fabric-agent.json`). A project an agent touched without either is
not judged: guessing would turn every repository into an agent.

Each finding names keys and files, never values, and carries the command that
moves the key into the vault or puts the exposure on the register.
"""
from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
_DASHBOARD = str(_HERE.parent / "dashboard")
if _DASHBOARD not in sys.path:
    sys.path.append(_DASHBOARD)
from finding_types import titled  # noqa: E402

VAULT_CMD = 'python "$(project-observatory full-path)/tools/vault.py"'
SLOT = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
LISTED = 8


def agent_projects(db: pathlib.Path | None, projects: list[dict]) -> dict[str, str]:
    """{project folder or id: why it is an agent's} — measured, two sources."""
    out: dict[str, str] = {}
    if db is not None and pathlib.Path(db).is_file():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT l.body_json FROM ledger l JOIN (SELECT memory_id, MAX(revision) r"
                " FROM ledger WHERE kind = 'checkpoint' GROUP BY memory_id) m"
                " ON m.memory_id = l.memory_id AND m.r = l.revision").fetchall()
            conn.close()
        except sqlite3.Error:
            rows = []
        for (raw,) in rows:
            try:
                body = json.loads(raw or "{}")
            except ValueError:
                continue
            for c in body.get("credentials", []) or []:
                name = str(c.get("project", "")).split(":", 1)[-1]
                if name:
                    out.setdefault(name, "a workflow declares its credentials")
    for p in projects:
        for folder in p.get("local_folders") or []:
            if (pathlib.Path(folder) / "fabric-agent.json").is_file():
                out.setdefault(pathlib.Path(folder).name, "it carries a Fabric agent manifest")
    return out


def seen_in_memory(rows: list[dict]) -> list[dict]:
    names: dict[str, list[dict]] = {}
    for r in rows:
        for n in r.get("names") or []:
            names.setdefault(n, []).append(r)
    if not names:
        return []
    listed = sorted(names)[:LISTED]
    return [{
        "type": "secret.seen_in_agent_memory",
        "subject": "estate:agent-memory",
        "severity": "critical",
        **titled("{n} known secret values were written into agent memory", n=len(names)),
        "detail": ("Each was replaced before it was stored, so the store holds the slot's "
                   "name and not the value; but an agent held the value, and its transcript "
                   "does too: " + "; ".join(
                       f"{n} in {names[n][-1].get('kind')} of {names[n][-1].get('workflowId')}"
                       f" at {names[n][-1].get('at')}" for n in listed)
                   + (f" and {len(names) - LISTED} more" if len(names) > LISTED else "") + "."),
        "action": (f"record each with `{VAULT_CMD} leak <project> <env> <NAME> --where \"agent "
                   f"memory, <workflow>\"`, rotate it, and give the agent the key by name "
                   f"(`use_secret.py run --vault-only`)"),
    }]


def outside_vault(env_doc: dict | None, vault: dict[str, set[str]],
                  agents: dict[str, str]) -> list[dict]:
    """Secrets in an agent project's env files with no vault slot of that name."""
    if not env_doc or not agents:
        return []
    missing: dict[str, set[str]] = {}
    for f in env_doc.get("files", []) or []:
        project = f.get("project")
        if project not in agents:
            continue
        for v in f.get("variables", []) or []:
            if v.get("class") == "secret" and v.get("name") not in vault.get(project, set()):
                missing.setdefault(project, set()).add(v["name"])
    out = []
    for project, names in sorted(missing.items()):
        first = sorted(names)[0]
        out.append({
            "type": "agent.secret_outside_vault",
            "subject": f"project-folder:{project}",
            "severity": "warning",
            **titled("{project}: an agent's secrets are outside the vault", project=project),
            "detail": (f"{project} is an agent's project ({agents[project]}) and keeps "
                       f"{len(names)} secret(s) in its own env files that the vault does not "
                       f"hold: {', '.join(sorted(names)[:LISTED])}. Every credential an "
                       f"agent uses comes from Observatory, by name."),
            "action": (f"`{VAULT_CMD} put {project} local {first} < <protected file>` for each, "
                       f"then remove it from the env file and run the agent with "
                       f"`OBSERVATORY_VAULT_ONLY=1`"),
        })
    return out


def fallback_used(audit_rows: list[dict], agents: dict[str, str]) -> list[dict]:
    """Runs for an agent's project that took a value from a `.env`."""
    used: dict[str, set[str]] = {}
    for r in audit_rows:
        if r.get("action") not in ("use", "serve"):
            continue
        project = str(r.get("subject", "")).split(":", 1)[0]
        if project not in agents:
            continue
        for name, where in (r.get("from") or {}).items():
            if str(where).startswith("env:"):
                used.setdefault(project, set()).add(name)
    return [{
        "type": "agent.secret_fallback_used",
        "subject": f"project-folder:{project}",
        "severity": "warning",
        **titled("{project}: an agent's run read a key from a .env", project=project),
        "detail": (f"use_secret resolved {', '.join(sorted(names)[:LISTED])} for {project} "
                   f"from an env file, not the vault ({agents[project]})."),
        "action": (f"put each into the vault (`{VAULT_CMD} put {project} local <NAME>`) and "
                   f"set `OBSERVATORY_VAULT_ONLY=1` for the agent, so the fallback is refused"),
    } for project, names in sorted(used.items())]


def _jsonl(path: pathlib.Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def findings(*, db: pathlib.Path, state: pathlib.Path, env_doc: dict | None,
             vault_dir: pathlib.Path, projects: list[dict]) -> list[dict]:
    """Everything above, from what is on disk now."""
    agents = agent_projects(db, projects)
    vault: dict[str, set[str]] = {}
    if vault_dir.is_dir():
        for slot in vault_dir.glob("*/*/*"):
            # A SLOT, as use_secret reads one: its meta file and a rotation's
            # archive sit beside it and are not slots.
            if slot.is_file() and SLOT.fullmatch(slot.name):
                vault.setdefault(slot.parent.parent.name, set()).add(slot.name)
    return (seen_in_memory(_jsonl(state / "logs" / "memory-redactions.jsonl"))
            + outside_vault(env_doc, vault, agents)
            + fallback_used(_jsonl(state / "logs" / "secret-use.jsonl"), agents))
