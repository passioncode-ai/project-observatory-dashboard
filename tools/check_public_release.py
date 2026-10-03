#!/usr/bin/env python3
"""Inspect the public tree/entire Git blob history without printing matches.

Optional --private-denylist reads a LOCAL JSON array of private identifiers.
It must never be committed. This scanner prints categories/counts, not values.
Names this repository publishes on purpose (the author, the author's public
projects) are listed with a reason in tools/public-identifiers.json and are
subtracted from the private list.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_IDENTIFIERS = ROOT / "tools" / "public-identifiers.json"


def public_identifiers(path: Path = PUBLIC_IDENTIFIERS) -> set[str]:
    """Lower-cased tokens published on purpose; each entry must carry a reason."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    out = set()
    for entry in doc["identifiers"]:
        if not str(entry.get("token") or "").strip() or not str(entry.get("reason") or "").strip():
            raise ValueError("every public identifier needs a token and a reason")
        out.add(entry["token"].strip().lower())
    return out
ALLOWED_TOP = {".github", ".claude-plugin", "observatory", "tests", "tools", "docs", "site"}
ALLOWED_ROOT = {".gitignore", "LICENSE", "COMMERCIAL-LICENSE.md", "CLA.md", "README.md", "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "CHANGELOG.md", "pyproject.toml", "AGENTS.md", "CLAUDE.md", "requirements-full.lock",
                ".gitleaksignore",  # reviewed gitleaks false positives, by fingerprint (path:rule:line)
                ".check-private-allow"}  # reviewed org-index check_private findings, by fingerprint (rule:file:hash)
# `.agent-sync/` is agent-sync's local, git-ignored state (run id, lease bookkeeping): it exists on
# every contributor's disk after a lease and is never published, so the walk skips it and a tracked
# copy is refused like any other unlisted path.
SKIP = {".git", ".venv", "__pycache__", "node_modules", "build", "dist", ".agent-sync", ".build", ".swiftpm"}
PUBLIC_IMAGES = {
    "site/assets/credential-copies-cartoon.png": {"8b69fe6ffcf44a4d5f8a32622d5c4d9d847d539c8c673c49fadf132c518be30c"},
    "site/assets/observatory-cover.png": {"70403cdb6ffc4029edcf2febbf63dec88a3118fa6cea9d7d6b17150d853d4833"},
    # Dashboard screenshots rendered by tools/demo_estate.py over a fictional
    # estate (Northwind Labs). The second digest of each is its first render,
    # kept in branch history; both were reviewed and show no private data.
    "site/assets/dashboard-overview-en.png": {"7ebedff4e0f9c0c1377edc722c9695cc01019dbab5649050b5176ed9cf93082c", "b1498815bc5728a8f99883a36bd95d9a7c34bb8b22d5489af105aebdbe728015"},
    "docs/images/dashboard-overview-ru.png": {"32f33bcc34375cc046f5af75214ed5770d83e2fb503dc765f560bb8e980a1eb3", "3a5f1c34649fc56b7d31735b74ad84796b34b64e07a61775004b07b2f6655ebe"},
    "docs/images/dashboard-projects-en.png": {"6e6c683647e48cb1d55911c57805302d7beb4bd6c564c3918ce956cb90a296d5", "d1eb9983ab58d7588e2fe36a6bdd2be4855a561eebc169debe0c12ac20145d91"},
}


def approved_image(relative: str, data: bytes) -> bool:
    return (relative in PUBLIC_IMAGES and data.startswith(b"\x89PNG\r\n\x1a\n")
            and len(data) <= 4 * 1024 * 1024
            and hashlib.sha256(data).hexdigest() in PUBLIC_IMAGES[relative])
EXTENSIONS = {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".html", ".css", ".js", ".svg", ".txt", ".xml"}
# Compose token-shape patterns so the detector's own source is not a fixture hit.
PATTERNS = {
    "provider-token-shape": re.compile(r"\b(?:" + "gh[pousr]_" + r"[A-Za-z0-9]{30,}|" + "github_pat_" + r"[A-Za-z0-9_]{40,}|" + "sk-" + r"(?:proj-|or-v1-|ant-api03-)?[A-Za-z0-9_-]{30,}|" + "AKIA" + r"[A-Z0-9]{16}|" + "AIza" + r"[A-Za-z0-9_-]{30,})"),
    "private-key-block": re.compile("-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "personal-machine-path": re.compile(r"(?<![A-Za-z0-9_.:/-])(?:file://)?/(?:" + "Users" + r"|home)/[A-Za-z0-9_.-]+/"),
    "credential-url": re.compile(r"https?://[^\s/@:]+:[^\s/@]+@"),
}


@functools.lru_cache(maxsize=8)
def _deny_pattern(deny: tuple[str, ...]) -> re.Pattern | None:
    """One alternation for the whole private list, longest first, compiled once.

    A regex per value per blob made a history scan with a few thousand values
    take longer than the CI job it guards. Pass/fail is the same; a count may
    differ only where two listed values match at one position.
    """
    values = sorted({v for v in deny if v}, key=len, reverse=True)
    if not values:
        return None
    # Factor common prefixes instead of trying thousands of alternatives at
    # every candidate position. Length buckets preserve longest-first matching,
    # including case-insensitive branches that happen to overlap.
    def factor(words: list[str]) -> str:
        trie = {}
        for word in words:
            node = trie
            for char in word:
                node = node.setdefault(char, {})
            node[None] = {}

        def emit(node: dict) -> str:
            prefix = ""
            while len(node) == 1 and None not in node:
                char, node = next(iter(node.items()))
                prefix += char
            branches = [re.escape(char) + emit(child) for char, child in node.items()
                        if char is not None]
            if None in node:
                branches.append("")
            suffix = branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"
            return re.escape(prefix) + suffix

        return emit(trie)

    buckets: dict[int, list[str]] = {}
    for value in values:
        buckets.setdefault(len(value), []).append(value)
    alternatives = "|".join(factor(words) for words in buckets.values())
    return re.compile(r"(?<![A-Za-z0-9_.-])(?:" + alternatives + r")(?:\.git)?(?![A-Za-z0-9_.-])",
                      re.IGNORECASE)


def scan_text(text: str, deny: list[str]) -> dict[str, int]:
    hits = {kind: len(rx.findall(text)) for kind, rx in PATTERNS.items()}
    rx = _deny_pattern(tuple(deny))
    hits["private-identifier"] = len(rx.findall(text)) if rx else 0
    return {kind: n for kind, n in hits.items() if n}


def scan_path(relative: str, deny: list[str]) -> dict[str, int]:
    hits = scan_text(relative, deny)
    for kind, count in scan_text(Path(relative).stem, deny).items():
        hits[kind] = max(hits.get(kind, 0), count)
    return hits


# Reviewed native source and synthetic tests only; no binaries, signing material,
# Xcode user state or SwiftPM caches are admitted by this list.
NATIVE_SOURCES = {
    "macos/Package.swift", "macos/scripts/build-app.sh",
    "macos/scripts/make-icon.swift", "macos/scripts/install-app.sh",
    "macos/Sources/ObservatoryCore/Bridge.swift", "macos/Sources/ObservatoryCore/Navigation.swift",
    "macos/Sources/ObservatoryCore/Palette.swift",
    "macos/Sources/ObservatoryApp/App.swift", "macos/Sources/ObservatoryApp/Model.swift",
    "macos/Sources/ObservatoryApp/Assistant.swift", "macos/Sources/ObservatoryApp/Dashboard.swift",
    "macos/Sources/ObservatoryApp/Theme.swift",
    "macos/Tests/ObservatoryCoreTests/BridgeTests.swift", "macos/Tests/ObservatoryCoreTests/ModelTests.swift",
    "macos/Tests/ObservatoryCoreTests/NavigationTests.swift", "macos/Tests/ObservatoryCoreTests/PaletteTests.swift",
    "macos/Tests/ObservatoryCoreTests/DashboardWindowTests.swift",
}


def allowed_path(rel: Path) -> bool:
    if rel.as_posix() in NATIVE_SOURCES:
        return True
    if rel.as_posix() in PUBLIC_IMAGES:
        return True
    # Reviewed offline browser-behavior test; do not admit arbitrary CommonJS files.
    if rel.as_posix() == "tools/check_site_interactions.cjs":
        return True
    # The coordination config of the repository standard (F12): lease backend, guarded files and
    # gates, no credential (the record plane is `fs`; `.env.agent-sync` stays ignored). Every other
    # file under `.claude/` — permission grants above all — stays private.
    if rel.as_posix() == ".claude/agent-sync.json":
        return True
    if any(x in SKIP or x.endswith(".egg-info") for x in rel.parts):
        return False
    if rel.parts[:2] == ("observatory", "engine"):
        engine = Path(*rel.parts[2:])
        # Runtime source is public; a runtime database or credential directory never is.
        if any(x.startswith(".env") or x in {"registry", "secrets", "raw", "backups", "logs"} for x in engine.parts):
            return False
        if engine.parts and engine.parts[0] == "store":
            return len(engine.parts) == 2 and (engine.suffix == ".py" or str(engine) == "store/schema.sql")
        if engine.suffix == ".sh":
            return str(engine) in {"tools/tick.sh", "tools/gate.sh", "skill/plugins/observatory-log/hooks/record-turn.sh", "skill/plugins/observatory-log/hooks/session-start.sh"}
        if any(x.startswith(".") for x in engine.parts):
            return str(engine) in {"skill/.claude-plugin/marketplace.json", "skill/plugins/observatory-log/.claude-plugin/plugin.json"}
        return bool(engine.suffix in EXTENSIONS or engine.suffix == ".mjs")
    allowed = (len(rel.parts) == 1 and str(rel) in ALLOWED_ROOT) or (len(rel.parts) > 1 and rel.parts[0] in ALLOWED_TOP)
    return bool(allowed and not any(x.startswith(".env") or x in {"registry", "secrets", "store"} for x in rel.parts)
                and (rel.suffix in EXTENSIONS or str(rel) in ALLOWED_ROOT or rel.name in {"_headers", "_redirects", "robots.txt", "CODEOWNERS"}))


def audit(root: Path, deny: list[str], history: bool, refs: tuple[str, ...] = ("--all",)) -> dict:
    findings = []
    files = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(x in SKIP or x.endswith(".egg-info") for x in rel.parts):
            continue
        if p.is_symlink():
            findings.append({"file_index": len(files), "kind": "symlink", "count": 1})
            continue
        if not p.is_file():
            continue
        index = len(files)
        files.append(str(rel))
        for kind, n in scan_path(rel.as_posix(), deny).items():
            findings.append({"file_index": index, "kind": "path:" + kind, "count": n})
        if not allowed_path(rel):
            findings.append({"file_index": index, "kind": "outside-public-allowlist", "count": 1})
        if rel.as_posix() in PUBLIC_IMAGES:
            if not approved_image(rel.as_posix(), p.read_bytes()):
                findings.append({"file_index": index, "kind": "unreviewed-image", "count": 1})
            continue
        if p.stat().st_size > 2 * 1024 * 1024:
            findings.append({"file_index": index, "kind": "oversized-public-file", "count": 1})
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            findings.append({"file_index": index, "kind": "unreadable-or-binary", "count": 1})
            continue
        for kind, n in scan_text(content, deny).items():
            findings.append({"file_index": index, "kind": kind, "count": n})
    blobs = 0
    if (root / ".git").exists():
        tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True)
        if tracked.returncode:
            findings.append({"kind": "tracked-inventory-unavailable", "count": 1})
        else:
            for rel in tracked.stdout.split("\0"):
                if rel and not allowed_path(Path(rel)):
                    findings.append({"kind": "tracked-outside-public-allowlist", "count": 1})
    if history:
        paths = subprocess.run(["git", "-C", str(root), "log", *refs, "--name-only", "--format=", "-z"], capture_output=True, text=True)
        if paths.returncode:
            findings.append({"kind": "historical-paths-unavailable", "count": 1})
        else:
            for rel in set(x.strip("\n") for x in paths.stdout.split("\0") if x.strip("\n")):
                if not allowed_path(Path(rel)):
                    findings.append({"kind": "history:forbidden-path", "count": 1})
                for category, n in scan_path(rel, deny).items():
                    findings.append({"kind": "history-path:" + category, "count": n})
        run = subprocess.run(["git", "-C", str(root), "rev-list", "--objects", *refs], capture_output=True, text=True)
        if run.returncode:
            findings.append({"kind": "git-history-unavailable", "count": 1})
        else:
            seen = set()
            for line in run.stdout.splitlines():
                oid, _, rel = line.partition(" ")
                if oid in seen:
                    continue
                seen.add(oid)
                kind = subprocess.run(["git", "-C", str(root), "cat-file", "-t", oid], capture_output=True, text=True)
                object_kind = kind.stdout.strip()
                if kind.returncode:
                    findings.append({"kind": "history:unreadable-object", "count": 1})
                    continue
                if object_kind in {"commit", "tag"}:
                    metadata = subprocess.run(["git", "-C", str(root), "cat-file", object_kind, oid], capture_output=True, text=True)
                    if metadata.returncode:
                        findings.append({"kind": "history:unreadable-metadata", "count": 1})
                    for category, n in scan_text(metadata.stdout, deny).items():
                        findings.append({"kind": "git-metadata:" + category, "count": n})
                if object_kind != "blob":
                    continue
                if not rel or not allowed_path(Path(rel)):
                    findings.append({"kind": "history:outside-public-allowlist", "count": 1})
                size = subprocess.run(["git", "-C", str(root), "cat-file", "-s", oid], capture_output=True, text=True)
                size_limit = 4 if rel in PUBLIC_IMAGES else 2
                if size.returncode or int(size.stdout.strip()) > size_limit * 1024 * 1024:
                    findings.append({"kind": "history:oversized-or-unreadable", "count": 1})
                    continue
                blob = subprocess.run(["git", "-C", str(root), "cat-file", "blob", oid], capture_output=True)
                if blob.returncode:
                    findings.append({"kind": "history:unreadable-blob", "count": 1})
                    continue
                blobs += 1
                if rel in PUBLIC_IMAGES:
                    if not approved_image(rel, blob.stdout):
                        findings.append({"kind": "history:unreviewed-image", "count": 1})
                    continue
                try:
                    text = blob.stdout.decode("utf-8")
                except UnicodeError:
                    findings.append({"kind": "binary-git-blob", "count": 1})
                    continue
                for category, n in scan_text(text, deny).items():
                    findings.append({"kind": "history:" + category, "count": n})
            messages = subprocess.run(["git", "-C", str(root), "log", *refs, "--format=%B"], capture_output=True, text=True)
            if messages.returncode:
                findings.append({"kind": "commit-messages-unavailable", "count": 1})
            for category, n in scan_text(messages.stdout, deny).items():
                findings.append({"kind": "commit-message:" + category, "count": n})
    return {"gate": "public-release-privacy", "passed": not findings, "files_checked": len(files),
            "history_blobs_checked": blobs, "private_denylist_entries": len(deny),
            "findings": findings,
            "limits": "Pattern/identifier checks cannot prove absence of every secret or private fact. Review source, metadata and release scope independently. Match values and paths are never printed."}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--private-denylist", type=Path)
    p.add_argument("--history", action="store_true")
    p.add_argument("--history-ref", action="append", default=[],
                   help="scan only history reachable from REF (repeatable); default is every ref. "
                        "CI passes HEAD so an unmerged branch cannot fail another pull request")
    a = p.parse_args()
    deny = []
    if a.private_denylist:
        try:
            deny = json.loads(a.private_denylist.read_text())
            if not isinstance(deny, list) or any(not isinstance(x, str) for x in deny):
                raise ValueError()
        except (ValueError, OSError):
            print(json.dumps({"passed": False, "error": "Private denylist must be a readable local JSON string array."}))
            return 2
    allowed = 0
    if deny:
        try:
            public = public_identifiers()
        except (OSError, ValueError, KeyError) as exc:
            print(json.dumps({"passed": False, "error": f"tools/public-identifiers.json is unreadable: {type(exc).__name__}"}))
            return 2
        kept = [x for x in deny if x.strip().lower() not in public]
        allowed, deny = len(deny) - len(kept), kept
    if any(ref.startswith("-") for ref in a.history_ref):
        print(json.dumps({"passed": False, "error": "--history-ref takes a ref name, not an option."}))
        return 2
    report = audit(a.root, deny, a.history, tuple(a.history_ref) or ("--all",))
    report["public_identifiers_subtracted"] = allowed
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
