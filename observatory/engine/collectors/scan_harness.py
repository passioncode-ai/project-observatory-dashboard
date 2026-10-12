#!/usr/bin/env python3
"""What every agent session on this machine loads before the first word, and whether it is safe.

    scan_harness.py store/raw/harness.json

docs/design/HARNESS.md (H1–H10). Opt-in, like the MCP scan: it reads nothing until `integrations.harness` is
on and `sources.harness_root` names the home folder whose agent files it reads. Read-only: files are read, never run; no process is started; no secret's
value enters the record. For Claude Code it reads the plain skills (`~/.claude/skills`), the skills of every
enabled plugin, the user instruction file and the two settings that switch off deferred MCP tools; for Codex its `AGENTS.md`. Each skill gets the characters its entry
costs in the session's skill listing, a provenance tier, a content digest, and the deterministic security
rules of the design (hidden Unicode, download-and-execute, a credential source beside an egress sink,
concealment directives in a description, opaque content). The listing budget is
OBSERVATORY_HARNESS_BUDGET_CHARS (default 30,000 characters); over it, descriptions are dropped and those
skills cannot be matched to a request — the first thing this scan reports.

The first sight of a skill records its digest in `store/state/harness-pins.json`; a later change is reported
until `full harness --accept KEY` (tools/harness.py) records the new digest.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic  # noqa: E402
import configuration  # noqa: E402
import paths  # noqa: E402

DEFAULT_BUDGET = 30_000
ENTRY_CAP = 1_536                  # a listing entry's description is cut here (Claude Code)
SPEC_DESCRIPTION_MAX = 1_024       # the Agent Skills standard
TEXT_MAX = 1_000_000               # larger text is itself reported, never skipped
OPAQUE = {".pyc", ".so", ".dylib", ".dll", ".exe", ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".jar",
          ".docm", ".xlsm", ".pptm", ".bin", ".wasm"}
SCRIPT = {".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".ps1", ".pl"}
TEXT = SCRIPT | {".md", ".txt", ".json", ".yaml", ".yml", ".toml"}

# --- rules (docs/design/HARNESS.md H5; the research's C-1, C-2, C-4, C-5) ------------------------------------

HIDDEN = re.compile("[\U000E0000-\U000E007F]{10,}|[‪-‮⁦-⁩]")
DOWNLOAD_EXEC = re.compile(
    r"(?:curl|wget|iwr|Invoke-WebRequest)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b"
    r"|(?:ba|z)?sh\s+<\(\s*(?:curl|wget)"
    r"|base64\s+(?:-d|--decode)[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b"
    r"|eval\s+\"?\$\(\s*(?:curl|wget)"
    r"|unzip\s+-P\s", re.I)
CREDENTIAL_SOURCE = re.compile(
    r"~/\.ssh/|\.ssh/id_|\.aws/credentials|security\s+find-(?:generic|internet)-password|"
    r"Login Data|Cookies\.binarycookies|/Cookies\b|\.claude\.json|\.cursor/mcp\.json|\.codex/auth|"
    r"keychain\s+dump|Library/Keychains")
EGRESS = re.compile(
    r"curl\b[^\n]*(?:\s-d\b|\s--data|\s-F\b|\s--upload-file|\s-T\b)|requests\.post|urllib\.request\.urlopen\([^)]*data=|"
    r"fetch\([^)]*method:\s*['\"]POST|\bnc\s+-?[a-z]*\s*\S+\s+\d+|\bdig\s+[^\n]*\$|smtplib|sendmail", re.I)
CONCEAL = re.compile(
    r"do not (?:tell|mention|inform|reveal)[^.]{0,40}\b(?:user|operator|human)|ignore (?:all )?(?:previous|prior) instructions|"
    r"before using any other tool|<IMPORTANT>|disable (?:the )?(?:approval|sandbox|permission)", re.I)

#: Severity by rule and provenance tier (1 own, 2 pinned third party, 3 anything else). Hidden Unicode and
#: concealment directives have no legitimate reading anywhere, so they are critical in every tier. A download that
#: is run is how most vendors install their own CLI, so it is a warning to read even from an unknown source; a
#: credential read and a send in one script is the theft shape, critical when nobody vouches for the source.
SEVERITY = {
    "hidden-unicode": ("critical", "critical", "critical"),
    "concealment": ("critical", "critical", "critical"),
    "download-exec": ("info", "info", "warning"),
    "credential-egress": ("warning", "warning", "critical"),
    "opaque-content": ("info", "info", "warning"),
}


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def budget() -> int:
    value = os.environ.get("OBSERVATORY_HARNESS_BUDGET_CHARS", "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else DEFAULT_BUDGET


def front_matter(text: str) -> dict:
    """The SKILL.md front matter's flat keys (name, description, when_to_use), folded blocks joined."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict[str, str] = {}
    key = None
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s?(.*)$", line)
        if m and not line.startswith((" ", "\t")):
            key = m.group(1)
            value = m.group(2).strip()
            out[key] = "" if value in (">", ">-", "|", "|-") else value.strip("'\"")
        elif key and line.startswith((" ", "\t")):
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def listing_chars(meta: dict, name: str) -> int:
    description = (meta.get("description") or "") + (meta.get("when_to_use") or "")
    return min(len(description), ENTRY_CAP) + len(name)


def digest(folder: Path) -> tuple[str, int, int, list[Path]]:
    """sha256 over every file's relative path and bytes; file and script counts; the files themselves."""
    h = hashlib.sha256()
    files = sorted(p for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts and ".git" not in p.parts)
    scripts = 0
    for f in files:
        h.update(f.relative_to(folder).as_posix().encode() + b"\0")
        try:
            h.update(f.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        scripts += f.suffix.lower() in SCRIPT
    return h.hexdigest(), len(files), scripts, files


def hits_for(name: str, folder: Path, files: list[Path], meta: dict, tier: int) -> list[dict]:
    out = []

    def hit(rule: str, file: Path, host: str | None = None) -> None:
        row = {"skill": name, "file": file.relative_to(folder).as_posix(), "rule": rule,
               "severity": SEVERITY[rule][tier - 1], "tier": tier}
        if host:
            row["host"] = host   # where the download comes from, so a reviewer sees the vendor at once
        out.append(row)

    description = (meta.get("description") or "") + " " + (meta.get("when_to_use") or "")
    if CONCEAL.search(description) or HIDDEN.search(description):
        hit("concealment" if CONCEAL.search(description) else "hidden-unicode", folder / "SKILL.md")
    for f in files:
        suffix = f.suffix.lower()
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if suffix in OPAQUE or (suffix in TEXT and size > TEXT_MAX):
            hit("opaque-content", f)
            continue
        if suffix not in TEXT:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if suffix in (".md", ".txt") and HIDDEN.search(text):
            hit("hidden-unicode", f)
        m = DOWNLOAD_EXEC.search(text)
        if m:
            line = text[text.rfind("\n", 0, m.start()) + 1:text.find("\n", m.end()) if text.find("\n", m.end()) > 0 else None]
            u = re.search(r"https?://([^/\s'\"]+)", line)
            hit("download-exec", f, u.group(1).lower() if u else None)
        if suffix in SCRIPT and CREDENTIAL_SOURCE.search(text) and EGRESS.search(text):
            hit("credential-egress", f)
    return out


def own_roots() -> list[Path]:
    """Where this operator's own code lives: the configured projects folder (and its resolved path)."""
    roots = []
    try:
        projects = configuration.load().get("sources", {}).get("projects")
    except configuration.ConfigurationError:
        projects = None
    for value in filter(None, [projects]):
        p = Path(value).expanduser()
        roots += [p, p.resolve()]
    return roots


def tier_of(folder: Path, marketplace_owner: str | None, roots: list[Path], own_owners: set[str]) -> int:
    real = folder.resolve()
    if any(str(real).startswith(str(r) + os.sep) for r in roots):
        return 1
    if marketplace_owner and marketplace_owner.lower() in own_owners:
        return 1
    if marketplace_owner:
        return 2
    return 3


def read_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def plugin_skill_dirs(claude: Path, degraded: list) -> list[tuple[str, Path, str | None]]:
    """(plugin@marketplace, its skill folder, the marketplace's GitHub owner) for every ENABLED plugin."""
    settings = read_json(claude / "settings.json")
    enabled = {k for k, v in (settings.get("enabledPlugins") or {}).items() if v is True}
    installed = read_json(claude / "plugins" / "installed_plugins.json").get("plugins") or {}
    markets = read_json(claude / "plugins" / "known_marketplaces.json")
    out = []
    for key, records in installed.items():
        if key not in enabled:
            continue
        record = records[0] if isinstance(records, list) and records else records
        path = Path((record or {}).get("installPath") or "")
        if not path.is_dir():
            degraded.append({"source": f"plugin:{key}", "reason": "installed path missing"})
            continue
        market = key.split("@", 1)[1] if "@" in key else ""
        repo = ((markets.get(market) or {}).get("source") or {}).get("repo") or ""
        owner = repo.split("/", 1)[0] if "/" in repo else None
        for skill in sorted((path / "skills").glob("*/SKILL.md")):
            out.append((key, skill.parent, owner))
    return out


#: Hook events that put text into every session's context (a speaker costs context each start) or run on every
#: tool call (a hook there runs thousands of times a day).
CONTEXT_EVENTS = {"SessionStart", "UserPromptSubmit"}
USAGE_DAYS = 30


def hook_rows(claude: Path, degraded: list) -> list[dict]:
    """Every declared hook: the user's settings.json `hooks` and each ENABLED plugin's `hooks/hooks.json`.

    A row keeps the event, the matcher, where it comes from, the program it starts (the command's first word,
    its basename) and a digest of the command — never the command itself, which can carry a token."""
    rows: list[dict] = []

    markets = read_json(claude / "plugins" / "known_marketplaces.json")

    def owner_of(key: str) -> str | None:
        market = key.split("@", 1)[1] if "@" in key else ""
        repo = ((markets.get(market) or {}).get("source") or {}).get("repo") or ""
        return repo.split("/", 1)[0].lower() if "/" in repo else None

    def take(doc: dict, source: str, tier_root: Path | None) -> None:
        for event, groups in ((doc.get("hooks") or {}) if isinstance(doc.get("hooks"), dict) else {}).items():
            for group in groups if isinstance(groups, list) else []:
                for h in (group or {}).get("hooks") or []:
                    command = str((h or {}).get("command") or "")
                    if not command:
                        continue
                    words = command.strip().split()
                    program = Path(words[0].strip("'\"")).name if words else ""
                    rows.append({"event": event, "matcher": (group or {}).get("matcher") or "", "source": source,
                                 "program": program[:80], "command_sha": hashlib.sha256(command.encode()).hexdigest()[:12],
                                 "_command": command, "_root": tier_root,
                                 "_owner": owner_of(source.split(":", 1)[1]) if source.startswith("plugin:") else None})

    take(read_json(claude / "settings.json"), "user", None)
    settings = read_json(claude / "settings.json")
    enabled = {k for k, v in (settings.get("enabledPlugins") or {}).items() if v is True}
    installed = read_json(claude / "plugins" / "installed_plugins.json").get("plugins") or {}
    for key, records in installed.items():
        if key not in enabled:
            continue
        record = records[0] if isinstance(records, list) and records else records
        path = Path((record or {}).get("installPath") or "")
        hooks_file = path / "hooks" / "hooks.json"
        if hooks_file.is_file():
            take(read_json(hooks_file), f"plugin:{key}", path)
    return rows


def hook_hits(rows: list[dict], roots: list[Path], own_owners: set[str] | None = None) -> list[dict]:
    """The skill rules, applied to each hook's command and to the script it names when that script is a file the
    plugin ships. A hook runs without asking, so a download-and-run there is never just "how a vendor installs"."""
    out = []
    for r in rows:
        texts = [r["_command"]]
        for word in r["_command"].replace("${CLAUDE_PLUGIN_ROOT}", str(r["_root"] or "")).split():
            p = Path(word.strip("'\""))
            if p.suffix.lower() in SCRIPT and p.is_file():
                try:
                    texts.append(p.read_text(encoding="utf-8", errors="replace")[:TEXT_MAX])
                except OSError:
                    pass
        text = "\n".join(texts)
        rules = []
        if DOWNLOAD_EXEC.search(text):
            rules.append("download-exec")
        if CREDENTIAL_SOURCE.search(text) and EGRESS.search(text):
            rules.append("credential-egress")
        if HIDDEN.search(text):
            rules.append("hidden-unicode")
        # The user's own settings and this operator's own plugins (by marketplace owner or by living under the
        # projects folder) are tier 1; a hook from anyone else runs code nobody here vouched for.
        own = (r["source"] == "user" or (r.get("_owner") or "") in (own_owners or set())
               or (r["_root"] is not None and any(root == r["_root"].resolve() or root in r["_root"].resolve().parents
                                                  for root in roots)))
        for rule in rules:
            severity = SEVERITY[rule][0 if own else 2]
            if rule == "download-exec" and not own:
                severity = "critical" if r["event"] in CONTEXT_EVENTS or r["event"] == "PreToolUse" else "warning"
            out.append({"source": r["source"], "event": r["event"], "program": r["program"], "rule": rule,
                        "severity": severity, "command_sha": r["command_sha"]})
    return out


def skill_usage(claude: Path, days: int = USAGE_DAYS) -> tuple[dict[str, int], int]:
    """How often each skill was invoked in the last `days`, from the session transcripts: the Skill tool's
    `skill` argument and the `/name` slash commands. ({name: uses}, transcripts read). Only lines that mention
    one of the two are parsed, so a large transcript costs a scan, not a parse."""
    import time
    since = time.time() - days * 86400
    uses: dict[str, int] = {}
    read = 0
    for path in (claude / "projects").glob("*/*.jsonl"):
        try:
            if path.stat().st_mtime < since:
                continue
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        read += 1
        with fh:
            for line in fh:
                if '"Skill"' not in line and "<command-name>" not in line:
                    continue
                for m in re.finditer(r'"name":\s*"Skill",\s*"input":\s*\{[^}]*?"skill":\s*"([^"]{1,120})"', line):
                    uses[m.group(1)] = uses.get(m.group(1), 0) + 1
                for m in re.finditer(r"<command-name>/?([A-Za-z0-9:_.-]{1,120})</command-name>", line):
                    uses[m.group(1)] = uses.get(m.group(1), 0) + 1
    return uses, read


def project_skills(roots: list[Path], user_listing: int, limit: int) -> tuple[list[dict], list[dict]]:
    """A project's own `.claude/skills` load only when a session works in that project, on top of the user-level
    listing. (Per project: name, its skills' listing characters, the session total; rule hits, tier 1 since the
    projects folder is this operator's own.) One level under each projects folder; a project's folder named
    like a worktree is still a project, so it is read the same way."""
    projects, hits = [], []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for skills_dir in sorted(root.glob("*/.claude/skills")):
            real = str(skills_dir.resolve())
            if real in seen:
                continue
            seen.add(real)
            project = skills_dir.parent.parent.name
            rows = []
            for skill in sorted(skills_dir.glob("*/SKILL.md")):
                try:
                    text = skill.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                meta = front_matter(text)
                name = meta.get("name") or skill.parent.name
                invocable = str(meta.get("disable-model-invocation", "")).lower() != "true"
                rows.append({"name": name, "listing_chars": listing_chars(meta, name) if invocable else 0})
                sha, _n, _s, files = digest(skill.parent)
                for h in hits_for(name, skill.parent, files, meta, 1):
                    hits.append({**h, "skill": f"{project}/{name}", "project": project, "digest": sha})
            if not rows:
                continue
            own = sum(r["listing_chars"] for r in rows)
            projects.append({"project": project, "skills": len(rows), "listing_chars": own,
                             "session_listing_chars": user_listing + own,
                             # Only the project's own skills can be blamed for an overflow the user level does not
                             # already cause; otherwise every project would repeat the user-level finding.
                             "pushes_over": user_listing <= limit < user_listing + own})
    return projects, hits


def codex_skills(home: Path, roots: list[Path]) -> tuple[dict, list[dict]]:
    """Codex's harness: the skills it loads from `~/.codex/skills` and the shared `~/.agents/skills`. (Its listing
    — skills and description characters, counted the way the Agent Skills standard truncates — and rule hits,
    named `codex/skill`.) A skill linked into both folders is counted once."""
    seen: set[str] = set()
    rows, hits = [], []
    for folder in (home / ".codex" / "skills", home / ".agents" / "skills"):
        for skill in sorted(folder.glob("*/SKILL.md")):
            real = str(skill.parent.resolve())
            if real in seen:
                continue
            seen.add(real)
            try:
                text = skill.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta = front_matter(text)
            name = meta.get("name") or skill.parent.name
            rows.append(listing_chars(meta, name))
            tier = tier_of(skill.parent, None, roots, set())
            sha, _n, _s, files = digest(skill.parent)
            for h in hits_for(name, skill.parent, files, meta, tier):
                hits.append({**h, "skill": f"codex/{name}", "harness": "codex", "digest": sha})
    return {"skills": len(rows), "listing_chars": sum(rows)}, hits


#: Where agent tools keep provider credentials of their own (OBS-74), and how each file is read. A tool's own login
#: session (Codex's auth.json, Claude Code's OAuth) is not listed: it is the tool's to keep and refresh.
TOOL_CONFIGS = (
    (".hermes/.env", "env"),
    (".local/share/opencode/auth.json", "json"),
    (".config/opencode/opencode.json", "json"),
    (".codex/config.toml", "toml"),
    (".claude.json", "json-mcp"),
    (".gemini/settings.json", "json"),
    (".cursor/mcp.json", "json"),
)
#: Names under which a credential-shaped value is a credential and not an id that merely looks random.
SECRET_NAME = re.compile(r"key|token|secret|password|passwd|auth|bearer|credential", re.I)


def _walk(node, path: str = ""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def tool_plaintext(home: Path) -> list[dict]:
    """Provider credentials kept in plaintext in agent tools' own config files: per file, the names (a key, or
    a dotted path in JSON/TOML) whose value is credential-shaped under a credential-like name. Never a value;
    never the file's other content. `.claude.json` is read only under `mcpServers` (env and headers)."""
    import credential_shape
    out = []
    for rel, kind in TOOL_CONFIGS:
        path = home / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pairs: list[tuple[str, str]] = []
        if kind == "env":
            for line in text.splitlines():
                m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
                if m:
                    pairs.append((m.group(1), m.group(2).strip().strip("'\"")))
        else:
            try:
                if kind == "toml":
                    import tomllib
                    doc = tomllib.loads(text)
                else:
                    doc = json.loads(text)
            except (ValueError, ImportError):
                continue
            if kind == "json-mcp":
                doc = {"mcpServers": doc.get("mcpServers") or {},
                       "projects": {p: {"mcpServers": (v or {}).get("mcpServers") or {}}
                                    for p, v in (doc.get("projects") or {}).items() if isinstance(v, dict)}}
            pairs = list(_walk(doc))
        names = sorted({name for name, value in pairs
                        if SECRET_NAME.search(name.rsplit(".", 1)[-1]) and credential_shape.shaped(value)})
        if names:
            out.append({"file": "~/" + rel, "names": names[:20], "count": len(names)})
    return out


def scan(home: Path, pins_file: Path, own_owners: set[str] | None = None) -> dict:
    own_owners = {o.lower() for o in (own_owners or {"passioncode-ai", "ssheleg"})}
    claude = home / ".claude"
    degraded: list[dict] = []
    roots = own_roots()
    candidates: list[tuple[str, str, Path, str | None]] = []   # (source, name, folder, marketplace owner)
    seen_dirs: set[str] = set()
    for skill in sorted((claude / "skills").glob("*/SKILL.md")):
        candidates.append(("plain", skill.parent.name, skill.parent, None))
    for plugin, folder, owner in plugin_skill_dirs(claude, degraded):
        candidates.append((f"plugin:{plugin}", folder.name, folder, owner))
    skills, security = [], []
    for source, name, folder, owner in candidates:
        real = str(folder.resolve())
        if real in seen_dirs:
            continue
        seen_dirs.add(real)
        try:
            text = (folder / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        except OSError:
            degraded.append({"source": f"skill:{name}", "reason": "SKILL.md unreadable"})
            continue
        meta = front_matter(text)
        skill_name = meta.get("name") or name
        tier = tier_of(folder, owner, roots, own_owners)
        sha, n_files, n_scripts, files = digest(folder)
        invocable = str(meta.get("disable-model-invocation", "")).lower() != "true"
        skills.append({"name": skill_name, "source": source, "tier": tier, "listing_chars": listing_chars(meta, skill_name)
                       if invocable else 0, "description_chars": len(meta.get("description") or ""),
                       "model_invocable": invocable, "files": n_files, "scripts": n_scripts, "digest": sha,
                       "key": f"{source}/{name}"})
        security += hits_for(skill_name, folder, files, meta, tier)
    uses, transcripts = skill_usage(claude)
    for s in skills:
        plugin = s["source"].split(":", 1)[1].split("@", 1)[0] if s["source"].startswith("plugin:") else None
        s["uses_30d"] = uses.get(s["name"], 0) + (uses.get(f"{plugin}:{s['name']}", 0) if plugin else 0)
    limit = budget()
    total = sum(s["listing_chars"] for s in skills)
    dropped: list[str] = []
    if total > limit:
        # The cheapest place to win the budget back: skills nobody invoked in the last 30 days, largest first,
        # then the least used (cost × (1 − use), with use read from the transcripts).
        over = total - limit
        for s in sorted(skills, key=lambda s: (s["uses_30d"] > 0, s["uses_30d"], -s["listing_chars"])):
            if over <= 0:
                break
            dropped.append(s["name"])
            over -= s["listing_chars"]
    projects, project_hits = project_skills(roots, total, limit)
    security += project_hits
    codex, codex_hits = codex_skills(home, roots)
    security += codex_hits
    pins = read_json(pins_file)
    changed = []
    stamp = now()
    for s in skills:
        old = pins.get(s["key"])
        if old is None:
            pins[s["key"]] = {"digest": s["digest"], "first_seen": stamp}
        elif old.get("digest") != s["digest"]:
            changed.append({"skill": s["name"], "key": s["key"], "since": old.get("accepted_at") or old.get("first_seen")})
    pins_file.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(pins_file, pins)
    instructions = []
    for path in (claude / "CLAUDE.md", home / ".codex" / "AGENTS.md"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            instructions.append({"path": str(path).replace(str(home), "~", 1), "lines": text.count("\n") + 1, "chars": len(text)})
    hooks = hook_rows(claude, degraded)
    hooks_found = hook_hits(hooks, roots, own_owners)
    for h in hooks:
        for private in ("_command", "_root", "_owner"):
            h.pop(private, None)
    settings = read_json(claude / "settings.json")
    env = settings.get("env") or {}
    base = str(env.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or "")
    base_host = re.sub(r"^[a-z]+://([^/:@]+@)?([^/:]+).*$", r"\2", base) if base else ""
    betas_off = bool(env.get("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS") or os.environ.get("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"))
    deferral_off = bool(base_host and not base_host.endswith("anthropic.com")) or betas_off
    return {
        "generated_at": stamp,
        "budget_chars": limit,
        "listing_chars": total,
        "skills_offered": sum(1 for s in skills if s["model_invocable"]),
        "dropped": dropped,
        "skills": skills,
        "security": security,
        "changed": changed,
        "instructions": instructions,
        "mcp_deferral": {"off": deferral_off, "base_url_host": base_host or None, "betas_disabled": betas_off},
        "projects": projects,
        "tool_plaintext": tool_plaintext(home),
        "codex": codex,
        "hooks": hooks,
        "hook_security": hooks_found,
        "usage": {"days": USAGE_DAYS, "transcripts": transcripts,
                  "unused_offered": sum(1 for s in skills if s["model_invocable"] and not s["uses_30d"])},
        "degraded": degraded,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    if not configuration.enabled("harness"):
        print("harness: not configured (integration disabled)")
        return 0
    home = paths.source_path("harness_root", paths.HOME / "disabled/harness")
    doc = scan(home, paths.STATE / "harness-pins.json")
    out = Path(argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(out, doc)
    print(f"harness: {doc['skills_offered']} skills offered, listing {doc['listing_chars']}/{doc['budget_chars']} chars, "
          f"{len(doc['dropped'])} descriptions over budget, {len(doc['security'])} rule hits, {len(doc['changed'])} changed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
