"""What is installed here for the coding agents, as one answer for the Installed tools page (OBS-552, CP-05).

Read from what the harness scan wrote (`store/raw/harness.json`, collectors/scan_harness.py): the coding agents of the
contract's catalogue found here with their versions (H16), every skill and plugin of this space (H17, through
`machine_view.skills_section`, so the page and `observatory_machine(section="skills")` agree on what was read and what
was not), and the hooks (H11). Each harness finding the engine raised (`harness.*`, open ones only: an acknowledged
row stays silent here as on the Findings page) is placed beside the plugin, skill, hook or agent it is about; one that
names none of them is listed apart, never dropped. Nothing is scanned here, and an unread or partial scan is named in
`degraded`, never shown as nothing installed.
"""
from __future__ import annotations

import json

import paths

#: The skill scopes in the order the page lists them.
SCOPES = ("user", "project", "plugin")
#: Agents whose records the inventory reads, listed first (scan_harness.CLAUDE_CODE, CODEX).
READ_AGENTS = ("claude-code", "codex")
#: Severity order: the worst first.
RANK = {"critical": 0, "warning": 1, "info": 2}
#: Agent-level findings: what the whole agent's sessions load, not one item.
AGENT_TYPES = {"harness.listing_overflow": "claude-code", "harness.mcp_deferral_off": "claude-code",
               "harness.project_listing_overflow": "claude-code"}


def _catalogue() -> dict[str, str]:
    """{agent id: its product name} from the contract's catalogue the scan reads (DEC-0093); {} when unreadable."""
    try:
        doc = json.loads((paths.ROOT / "fabric" / "node-vectors" / "summary" / "coding-agents.json")
                         .read_text(encoding="utf-8"))
        return {a["id"]: a.get("name") or a["id"] for a in doc.get("agents") or []
                if isinstance(a, dict) and isinstance(a.get("id"), str)}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def _row(f: dict) -> dict:
    """A finding as the page shows it: its id (the Findings page's anchor), severity and title."""
    return {k: f.get(k) for k in ("id", "type", "severity", "title", "title_id", "title_args")}


def _worst(rows: list[dict]) -> int:
    return min((RANK.get((f or {}).get("severity"), 3) for f in rows), default=4)


def _place(f: dict, agents: dict) -> bool:
    """Put `f` beside the item its subject names; False when it names none of them."""
    kind, subject = f.get("type") or "", str(f.get("subject") or "")
    if kind in AGENT_TYPES:
        agent = agents.get(AGENT_TYPES[kind])
        if agent is not None:
            agent["findings"].append(_row(f))
            return True
        return False
    if kind == "harness.instruction_long":
        agent = agents.get("codex" if "/.codex/" in subject else "claude-code")
        if agent is not None:
            agent["findings"].append(_row(f))
            return True
        return False
    hits: list[dict] = []
    if kind == "harness.plugin_behind" and subject.startswith("harness:plugin-behind:"):
        parts = subject[len("harness:plugin-behind:"):].split(":", 3)
        if len(parts) == 4:
            agent, name, scope, tail = parts
            project = tail.rsplit(":", 2)[0]
            for p in (agents.get(agent) or {}).get("plugins") or []:
                where = (p.get("project") or "") + ("." + str(p["projectKey"]) if p.get("projectKey") else "")
                if p.get("name") == name and p.get("scope") == scope and where == project:
                    hits.append(p)
    elif kind == "harness.skill_risk" and subject.startswith("harness:skill:"):
        parts = subject[len("harness:skill:"):].rsplit(":", 2)
        skill = parts[0] if len(parts) == 3 else ""
        if skill.startswith("codex/"):
            hits = [s for s in ((agents.get("codex") or {}).get("skills") or {}).get("user") or []
                    if s.get("name") == skill[len("codex/"):]]
        elif "/" in skill:
            project, name = skill.split("/", 1)
            hits = [s for s in ((agents.get("claude-code") or {}).get("skills") or {}).get("project") or []
                    if s.get("project") == project and s.get("name") == name]
        if not hits:
            skills = (agents.get("claude-code") or {}).get("skills") or {}
            hits = [s for scope in ("user", "plugin") for s in skills.get(scope) or [] if s.get("name") == skill]
    elif kind == "harness.skill_changed" and subject.startswith("harness:changed:"):
        key = subject[len("harness:changed:"):]
        source = key.rsplit("/", 1)[0] if "/" in key else ""
        name = (f.get("title_args") or {}).get("skill")
        skills = (agents.get("claude-code") or {}).get("skills") or {}
        if source == "plain":
            hits = [s for s in skills.get("user") or [] if s.get("name") == name]
        elif source.startswith("plugin:"):
            hits = [s for s in skills.get("plugin") or [] if s.get("name") == name
                    and s.get("plugin") == source[len("plugin:"):]]
    elif kind == "harness.hook_risk" and subject.startswith("harness:hook:"):
        parts = subject[len("harness:hook:"):].rsplit(":", 3)
        if len(parts) == 4:
            source, event, _rule, sha = parts
            hits = [h for h in (agents.get("claude-code") or {}).get("hooks") or []
                    if h.get("source") == source and h.get("event") == event and h.get("command_sha") == sha]
    for item in hits:
        item["findings"].append(_row(f))
    return bool(hits)


def build(harness: dict | None, skills: dict, findings: list[dict] | None) -> dict:
    """The page's answer from the scan (`harness`, or None when it was not read), the skills section
    (`machine_view.skills_section()`) and the engine's open findings (`None` when they were not built)."""
    doc = harness if isinstance(harness, dict) else {}
    names = _catalogue()
    degraded = [d for d in skills.get("degraded") or [] if isinstance(d, dict)]
    # The scan's own gaps that change what this page lists; a tool's config it could not read is a finding.
    degraded += [d for d in doc.get("degraded") or [] if isinstance(d, dict)
                 and not str(d.get("source", "")).startswith("tool-config:")]
    agents: dict[str, dict] = {}

    def agent(aid: str) -> dict:
        if aid not in agents:
            agents[aid] = {"id": aid, "name": names.get(aid) or aid, "installed": False, "version": None,
                           "findings": [], "plugins": [], "skills": {}, "hooks": []}
        return agents[aid]

    for a in doc.get("coding_agents") or []:
        if isinstance(a, dict) and isinstance(a.get("id"), str):
            row = agent(a["id"])
            row["installed"] = True
            row["version"] = a.get("version") if isinstance(a.get("version"), str) else None
    # The source tier the security scan recorded per skill (H6): 1 this person's own code, 2 a pinned marketplace,
    # 3 anything else. A project's own skills are read at tier 1 (scan_harness.project_skills).
    tiers = {(s.get("source"), s.get("name")): s.get("tier") for s in doc.get("skills") or []
             if isinstance(s, dict) and isinstance(s.get("tier"), int)}
    for space in skills.get("spaces") or []:
        for p in space.get("plugins") or []:
            if isinstance(p, dict) and isinstance(p.get("agent"), str):
                agent(p["agent"])["plugins"].append({**p, "findings": []})
        for s in space.get("skills") or []:
            if not (isinstance(s, dict) and isinstance(s.get("agent"), str)):
                continue
            scope = s.get("scope") if isinstance(s.get("scope"), str) else "other"
            if s["agent"] == "claude-code" and scope == "user":
                tier = tiers.get(("plain", s.get("name")))
            elif s["agent"] == "claude-code" and scope == "plugin":
                tier = tiers.get((f"plugin:{s.get('plugin')}", s.get("name")))
            elif s["agent"] == "claude-code" and scope == "project":
                tier = 1
            else:
                tier = None
            agent(s["agent"])["skills"].setdefault(scope, []).append({**s, "tier": tier, "findings": []})
    for h in doc.get("hooks") or []:
        if isinstance(h, dict):
            agent("claude-code")["hooks"].append({**h, "findings": []})
    other = []
    if findings is None:
        degraded.append({"source": "findings", "code": "not-built",
                         "reason": "the findings were not built, so none is shown beside the items"})
    for f in findings or []:
        if isinstance(f, dict) and str(f.get("type", "")).startswith("harness.") and not _place(f, agents):
            other.append(_row(f))
    for a in agents.values():
        a["plugins"].sort(key=lambda p: (_worst(p["findings"]), not p.get("behind"), p.get("name") or "",
                                         p.get("scope") or "", p.get("project") or ""))
        a["skills"] = {scope: sorted(a["skills"][scope], key=lambda s: (_worst(s["findings"]), s.get("name") or "",
                                                                        s.get("project") or s.get("plugin") or ""))
                       for scope in [*SCOPES, *sorted(set(a["skills"]) - set(SCOPES))] if scope in a["skills"]}
        a["hooks"].sort(key=lambda h: (_worst(h["findings"]), h.get("event") or "", h.get("program") or ""))
    other.sort(key=lambda f: RANK.get(f.get("severity"), 3))
    ordered = sorted(agents.values(), key=lambda a: (READ_AGENTS.index(a["id"]) if a["id"] in READ_AGENTS else 2,
                                                     a["name"].lower()))
    rows = [*ordered]
    return {"measuredAt": skills.get("measuredAt") or doc.get("generated_at"),
            "agents": rows, "other": other,
            "counts": {"agents": len(rows), "plugins": sum(len(a["plugins"]) for a in rows),
                       "behind": sum(1 for a in rows for p in a["plugins"] if p.get("behind")),
                       "skills": sum(len(v) for a in rows for v in a["skills"].values()),
                       "hooks": sum(len(a["hooks"]) for a in rows),
                       "findings": sum(1 for f in findings or [] if isinstance(f, dict)
                                       and str(f.get("type", "")).startswith("harness."))},
            "open": skills.get("open") or [], "degraded": degraded}


def summary(findings: list[dict] | None = None) -> dict:
    """`build` over this workspace's scan, read without scanning."""
    import machine_view
    harness, _problem = machine_view._load("harness.json", "scan-harness")
    return build(harness, machine_view.skills_section(), findings)
