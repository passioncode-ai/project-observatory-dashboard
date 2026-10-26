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

It also lists, for the node summary an organization receives (OBS-192, contract DEC-0101), the ENABLED plugins by
`name@marketplace` with the version Claude Code recorded, and the coding agents of the contract's catalogue (DEC-0093)
installed here: an agent whose launch command is an executable in a bin folder, its version read from where the
launcher resolves (a versions folder, a Homebrew keg, an npm package.json), never by running it. No path is recorded.
For the same summary it lists each project's own skills (`project_skills`: name, digest, and every spelling of the
project's folder as `~/<path>`, kept here so the summary can judge an organization's work folders, never sent) and Codex's skills
(`codex_skills`: name, source and digest).

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
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic  # noqa: E402
import configuration  # noqa: E402
import osprivacy  # noqa: E402
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


def enabled_plugins(claude: Path) -> list[dict]:
    """Every ENABLED and installed plugin by `name@marketplace`, its source and the version Claude Code recorded when
    it installed it (OBS-192: the node summary's `plugins`). A plugin enabled in the settings but not installed loads
    nothing and is not listed. A plugin whose marketplace Claude Code knows is `marketplace`, one it does not is
    `other`. Its folder is not read and never recorded."""
    settings = read_json(claude / "settings.json")
    enabled = {k for k, v in (settings.get("enabledPlugins") or {}).items() if v is True}
    installed = read_json(claude / "plugins" / "installed_plugins.json").get("plugins") or {}
    markets = read_json(claude / "plugins" / "known_marketplaces.json")
    out = []
    for key in sorted(enabled):
        records = installed.get(key) if isinstance(installed, dict) else None
        record = records[0] if isinstance(records, list) and records else records
        if not isinstance(record, dict) or not record:
            continue
        market = key.split("@", 1)[1] if "@" in key else ""
        row = {"name": key, "source": "marketplace" if market and market in markets else "other"}
        version = record.get("version")
        if isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}", version):
            row["version"] = version
        out.append(row)
    return out


#: The coding-agent catalogue (contract DEC-0093), vendored at the engine's contract pin.
CATALOGUE = ROOT / "fabric" / "node-vectors" / "summary" / "coding-agents.json"
#: Where an installed coding agent's launcher is looked for, besides PATH: the folders install scripts and package
#: managers put one in, under the home folder this scan reads and on the system.
HOME_BIN_DIRS = (".local/bin", "bin", ".npm-global/bin", ".bun/bin", ".cargo/bin", ".deno/bin", ".opencode/bin")
SYSTEM_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
PACKAGE_JSON_MAX = 256 * 1024
#: A version as a release names it: dotted numbers, and at most one prerelease word with numbers after it. Anything
#: else (a folder named after a person, a path piece, a build tag with a name in it) is no version and is omitted.
STRICT_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,3}"
                            r"(?:-(?:alpha|beta|rc|pre|preview|dev|canary|next|nightly)(?:[.-]?[0-9]+){0,3})?")
#: Catalogue launch commands too generic to say which agent is installed (`agent` is Cursor's, `cn` Continue's...):
#: such an agent is reported only when its launcher resolves into the agent's own package or bundle.
GENERIC_COMMANDS = frozenset({"agent", "cmd", "cn", "pi", "omp", "zed"})


def bin_dirs(home: Path) -> list[Path]:
    dirs = [home / d for d in HOME_BIN_DIRS]
    dirs += [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    dirs += [Path(p) for p in SYSTEM_BIN_DIRS]
    seen, out = set(), []
    for d in dirs:
        if str(d) not in seen:
            seen.add(str(d))
            out.append(d)
    return out


def launcher_version(launcher: Path) -> str | None:
    """A coding agent's version, read from where its launcher resolves, never by running it: a Homebrew keg or cask
    (`Cellar/<name>/<version>/`, `Caskroom/<name>/<version>/`), a `versions/<version>` folder (Claude Code's own
    installer), or the `package.json` of the npm package that holds it. None when none of those says."""
    try:
        real = launcher.resolve()
    except (OSError, RuntimeError):
        return None
    parts = real.parts
    shape = STRICT_VERSION
    for i, part in enumerate(parts[:-1]):
        if part in ("Cellar", "Caskroom") and i + 2 < len(parts):
            version = re.sub(r"_[0-9]+$", "", parts[i + 2])          # a keg's revision suffix is not the version
            return version if shape.fullmatch(version) else None
        if part == "versions":
            return parts[i + 1] if shape.fullmatch(parts[i + 1]) else None
    for folder in list(real.parents)[:6]:
        if "node_modules" not in folder.parts:
            break
        candidate = folder / "package.json"
        try:
            doc = json.loads(osprivacy.read_regular(candidate, PACKAGE_JSON_MAX))
        except (OSError, ValueError):
            continue
        version = doc.get("version") if isinstance(doc, dict) else None
        return version if isinstance(version, str) and shape.fullmatch(version) else None
    return None


def own_package(launcher: Path, agent: dict) -> bool:
    """Whether a launcher resolves into the agent's own package or bundle: a path piece named as its catalogue id, its
    app bundle (`<id>.app`), its npm package's folders (`@scope/name`), or its Homebrew keg or cask."""
    try:
        parts = [p.lower() for p in launcher.resolve().parts[:-1]]   # the launcher's own file name proves nothing
    except (OSError, RuntimeError):
        return False
    agent_id = str(agent.get("id") or "").lower()
    if agent_id and (agent_id in parts or agent_id + ".app" in parts):
        return True

    def runs(pieces: list[str]) -> bool:
        return any(parts[i:i + len(pieces)] == pieces for i in range(len(parts) - len(pieces) + 1))
    installs = agent.get("install") if isinstance(agent.get("install"), list) else []
    for install in (i for i in installs if isinstance(i, dict) and isinstance(i.get("package"), str)):
        package = install["package"].lower()
        if install.get("via") == "npm" and runs(["node_modules", *package.split("/")]):
            return True
        if install.get("via") in ("brew", "brew-cask"):
            name = package.rsplit("/", 1)[-1]
            if runs(["cellar", name]) or runs(["caskroom", name]) or name + ".app" in parts:
                return True
    return False


def coding_agents(home: Path, degraded: list, dirs: list[Path] | None = None) -> list[dict]:
    """The coding agents of the contract's catalogue (DEC-0093) installed here: an agent is installed when its
    catalogue launch command is an executable file in one of `bin_dirs` (for a generic command, `GENERIC_COMMANDS`, only
    when it resolves into the agent's own package or bundle). Its id is the catalogue's; its version is
    `launcher_version`'s (`STRICT_VERSION`), or absent. Nothing is run, and no path is recorded."""
    try:
        catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8"))
        agents = [a for a in catalogue.get("agents") or [] if isinstance(a, dict)]
    except (OSError, ValueError, AttributeError):
        degraded.append({"source": "coding-agent catalogue", "reason": "unreadable: installed coding agents not read"})
        return []
    dirs = bin_dirs(home) if dirs is None else dirs
    out = []
    for agent in agents:
        argv = (agent.get("launch") or {}).get("argv") if isinstance(agent.get("launch"), dict) else None
        command = argv[0] if isinstance(argv, list) and argv and isinstance(argv[0], str) else None
        if not isinstance(agent.get("id"), str) or not command or "/" in command or "\\" in command:
            continue
        for folder in dirs:
            launcher = folder / command
            try:
                found = launcher.is_file() and os.access(launcher, os.X_OK)
            except OSError:
                found = False
            if found and command in GENERIC_COMMANDS and not own_package(launcher, agent):
                continue                                # a generic name alone says nothing about which agent it is
            if found:
                row = {"id": agent["id"]}
                version = launcher_version(launcher)
                if version:
                    row["version"] = version
                out.append(row)
                break
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


def home_relatives(folder: Path, home: Path | None) -> list[str]:
    """Every spelling of `folder` as `~/<path>` inside the scanned home (how the MCP scan spells a folder): the path as
    found and, when a link leads elsewhere in the home, the path it resolves to. Empty outside the home. The node
    summary matches them against an organization's `workspace.folders`, which are relative to the home, and never
    sends them."""
    if home is None:
        return []
    out: list[str] = []
    for base in dict.fromkeys((home, home.resolve())):
        for candidate in dict.fromkeys((folder, folder.resolve())):
            try:
                rel = candidate.relative_to(base).as_posix()
            except ValueError:
                continue
            if rel not in ("", ".") and "~/" + rel not in out:
                out.append("~/" + rel)
    return out


def project_skills(roots: list[Path], user_listing: int, limit: int,
                   home: Path | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """A project's own `.claude/skills` load only when a session works in that project, on top of the user-level
    listing. (Per project: name, its skills' listing characters, the session total; rule hits, tier 1 since the
    projects folder is this operator's own.) One level under each projects folder; a project's folder named
    like a worktree is still a project, so it is read the same way.

    The third list is one row per skill for the node summary (OBS-192): its name, every spelling of the project's
    folder as `~/<path>` inside the scanned home (`folders`, empty outside it) and its content digest. A project is
    read once by the folder it resolves to, so a project reached through a link is one project, and every spelling it
    was reached by is kept: whichever of them sits in an organization's `workspace.folders` admits it (DEC-0118). The
    folders stay on this machine: the summary reads them only for that judgement."""
    projects, hits, rows_out = [], [], []
    seen: dict[str, list[dict]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for skills_dir in sorted(root.glob("*/.claude/skills")):
            real = str(skills_dir.resolve())
            spellings = home_relatives(skills_dir.parent.parent, home)
            if real in seen:
                for row in seen[real]:
                    row["folders"] += [f for f in spellings if f not in row["folders"]]
                continue
            seen[real] = held = []
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
                held.append({"name": name, "folders": list(spellings), "digest": sha})
                rows_out.append(held[-1])
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
    return projects, hits, rows_out


def codex_skills(home: Path, roots: list[Path]) -> tuple[dict, list[dict], list[dict]]:
    """Codex's harness: the skills it loads from `~/.codex/skills` and the shared `~/.agents/skills`. (Its listing
    — skills and description characters, counted the way the Agent Skills standard truncates — and rule hits,
    named `codex/skill`.) A skill linked into both folders is counted once. The third list is one row per skill for
    the node summary (OBS-192): its name, `source` `user` (both folders are the person's own) and its content
    digest; no path."""
    seen: set[str] = set()
    rows, hits, skill_rows = [], [], []
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
            skill_rows.append({"name": name, "source": "user", "digest": sha})
            for h in hits_for(name, skill.parent, files, meta, tier):
                hits.append({**h, "skill": f"codex/{name}", "harness": "codex", "digest": sha})
    return {"skills": len(rows), "listing_chars": sum(rows)}, hits, skill_rows


#: Where agent tools keep provider credentials of their own (OBS-74), and how each file is read. A tool's own login
#: session (Codex's auth.json, Claude Code's OAuth) is not listed: it is the tool's to keep and refresh.
TOOL_CONFIGS = (
    (".hermes/.env", "env"),
    (".local/share/opencode/auth.json", "json"),
    (".config/opencode/opencode.json", "json"),
    (".codex/config.toml", "toml"),
    (".claude.json", "json-mcp"),
    (".claude/settings.json", "json-env"),
    (".gemini/settings.json", "json"),
    (".cursor/mcp.json", "json"),
)
#: Names under which a credential-shaped value is a credential and not an id that merely looks random.
SECRET_NAME = re.compile(r"key|token|secret|password|passwd|auth|bearer|credential", re.I)


def _walk(node, path: str = ""):
    """Bound both nesting and width before deriving credential field names."""
    stack = [(node, path, 0)]
    count = 0
    while stack:
        node, path, depth = stack.pop()
        count += 1
        if depth > 64 or count > 10_000 or len(path) > 4096:
            raise ValueError("configuration traversal limit")
        if isinstance(node, dict):
            if len(node) + len(stack) + count > 10_000:
                raise ValueError("configuration traversal limit")
            stack.extend((v, f"{path}.{k}" if path else str(k), depth + 1) for k, v in node.items())
        elif isinstance(node, list):
            if len(node) + len(stack) + count > 10_000:
                raise ValueError("configuration traversal limit")
            stack.extend((v, f"{path}[{i}]", depth + 1) for i, v in enumerate(node))
        elif isinstance(node, str):
            yield path, node


def tool_plaintext(home: Path, degraded: list[dict] | None = None,
                   roots: list[Path] | None = None) -> list[dict]:
    """Report names, parse coverage and tracking, never values or parser bodies.

    A name recovered from a broken file is still evidence. It does not turn a
    partial parse into complete coverage; its degraded row remains beside it.
    """
    import credential_shape
    import safe_git
    degraded = degraded if degraded is not None else []
    roots = roots if roots is not None else own_roots()
    out = []
    project_ids = {}
    registered = read_json(paths.REGISTRY / "projects.json").get("projects", [])
    for project in registered if isinstance(registered, list) else []:
        if not isinstance(project, dict) or not isinstance(project.get("id"), str):
            continue
        folders = project.get("local_folders")
        for folder in folders if isinstance(folders, list) else []:
            if isinstance(folder, str):
                for root in roots:
                    project_ids[str((root / folder).resolve())] = project["id"]

    def project_name(path):
        path = Path(path)
        value = project_ids.get(str(path.resolve()), path.name)
        return value if value.startswith("project:") else "project:" + value

    def problem(label, code, reason):
        degraded.append({"source": "tool-config:" + label, "code": code, "reason": reason})

    candidates = [(home / rel, kind, "~/" + rel) for rel, kind in TOOL_CONFIGS]
    for root in dict.fromkeys(p.resolve() for p in roots):
        try:
            with os.scandir(root) as entries:
                for index, entry in enumerate(entries):
                    if index >= 10_000:
                        problem("configured-projects", "discovery-limit", "project configuration discovery reached its entry limit")
                        break
                    if entry.is_dir(follow_symlinks=False):
                        project = Path(entry.path)
                        candidates.append((project / ".mcp.json", "json", project_name(project) + "/.mcp.json"))
        except FileNotFoundError:
            continue
        except OSError:
            problem("configured-projects", "unreadable", "project configuration discovery could not list its root")

    def tracked(path, label):
        try:
            marker = False
            for parent in path.parents:
                try:
                    (parent / ".git").lstat()
                    marker = True
                    break
                except FileNotFoundError:
                    continue
            if not marker:
                return False
            result = safe_git.run(["ls-files", "--error-unmatch", "--", path.name], cwd=path.parent, timeout=5)
            if result.returncode in (0, 1):
                return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
        problem(label, "tracking-unknown", "Git tracking could not be measured; review whether this config is committed")
        return None

    def object_field(doc, key):
        value = doc.get(key, {})
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value

    def fallback(text, kind):
        section = ""
        for line in text.splitlines():
            if kind == "toml":
                table = re.fullmatch(r"\s*\[([A-Za-z0-9_.-]+)\]\s*", line)
                if table:
                    section = table[1] + "."
            # Do not copy surrounding lines, parser context or project path keys.
            for match in re.finditer(r"[\"']?([A-Za-z_][A-Za-z0-9_.-]*)[\"']?\s*[:=]\s*[\"']([^\"'\n]*)[\"']", line):
                yield section + match[1], match[2]

    for path, kind, label in candidates:
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                problem(label, "unreadable", "not a regular config file; excluded without reading")
                continue
            if info.st_size > TEXT_MAX:
                problem(label, "size-limit", "config exceeds the text size limit; not read")
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        except OSError:
            problem(label, "unreadable", "config could not be read")
            continue
        parsed = True
        pairs = []
        if kind == "env":
            for line in text.splitlines():
                match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
                if match:
                    pairs.append((match[1], match[2].strip().strip("'\"")))
        else:
            try:
                if kind == "toml":
                    import tomllib
                    doc = tomllib.loads(text)
                else:
                    doc = json.loads(text)
                if not isinstance(doc, dict):
                    raise ValueError("object required")
                if kind == "json-mcp":
                    projects = object_field(doc, "projects")
                    if not isinstance(projects, dict):
                        raise ValueError("projects object required")
                    pairs = list(_walk(object_field(doc, "mcpServers"), "mcpServers"))
                    for folder, config in projects.items():
                        if not isinstance(config, dict):
                            raise ValueError("project object required")
                        pairs.extend(_walk(object_field(config, "mcpServers"), project_name(folder) + ".mcpServers"))
                elif kind == "json-env":
                    pairs = list(_walk(object_field(doc, "env"), "env"))
                else:
                    pairs = list(_walk(doc))
            except (ValueError, ImportError, RecursionError):
                parsed = False
                problem(label, "parse-error", "config could not be parsed; recovered names are incomplete")
                pairs = list(fallback(text, kind))
        names = sorted({name for name, value in pairs
                        if (SECRET_NAME.search(name.rsplit(".", 1)[-1])
                            or ".headers." in name or ".http_headers." in name)
                        and credential_shape.shaped(value)})
        if names:
            out.append({"file": label, "names": names[:20], "count": len(names),
                        "parsed": parsed, "tracked": tracked(path, label)})
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
    projects, project_hits, project_rows = project_skills(roots, total, limit, home)
    security += project_hits
    codex, codex_hits, codex_rows = codex_skills(home, roots)
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
        "tool_plaintext": tool_plaintext(home, degraded, roots),
        "codex": codex,
        "project_skills": project_rows,
        "codex_skills": codex_rows,
        "hooks": hooks,
        "hook_security": hooks_found,
        "plugins": enabled_plugins(claude),
        "coding_agents": coding_agents(home, degraded),
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
