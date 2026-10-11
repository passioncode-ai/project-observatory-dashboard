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
    limit = budget()
    total = sum(s["listing_chars"] for s in skills)
    dropped: list[str] = []
    if total > limit:
        # Which descriptions go is decided by usage the scan cannot see; the largest entries are named, since
        # they are the cheapest place to win the budget back.
        over = total - limit
        for s in sorted(skills, key=lambda s: -s["listing_chars"]):
            if over <= 0:
                break
            dropped.append(s["name"])
            over -= s["listing_chars"]
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
