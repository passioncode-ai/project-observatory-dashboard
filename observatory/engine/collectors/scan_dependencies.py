#!/usr/bin/env python3
"""Every dependency each project resolves, read from its lockfiles (OBS-553, CP-06).

`plugins/dependency_weight.py` counts the DIRECT names a manifest declares, and the stack is
guessed from marker files; neither says which version of which package a project would run. A
vulnerability is a fact about a version, so this reads what a resolver already decided: the
lockfile. Standard library only, one reader per format, the same formats osv-scanner reads
(docs/design/control-plane/research.md):

    package-lock.json, npm-shrinkwrap.json (v2, v3)   npm      `packages` map, `link` skipped
    pnpm-lock.yaml, yarn.lock (v1 and Berry)          npm      a narrow line reader
    package.json (no lock beside it)                  npm      direct names, declared ranges
    Pipfile.lock, poetry.lock, uv.lock, pylock.toml   PyPI
    requirements*.txt (`==` pins only)                PyPI
    go.mod (`require` blocks)                         Go
    Cargo.lock                                        crates.io
    Gemfile.lock (`GEM` specs)                        RubyGems
    composer.lock                                     Packagist

**Local stays local (decision D2).** A dependency on a path, a workspace member, a link, a git
URL or a private registry is `local: true` with its `source`, and `tools/vuln_watch.py` never
sends it anywhere. A package a person names as private in `config/private_packages.json`
(`{"names": ["…"], "npm_scopes": ["@example-org"]}`) is marked the same way.

**A file that cannot be read is named, never read as no dependencies.** Its row carries `error`
and the project's `degraded` says which file and why.

**Cheap every tick.** A file whose size and modification time match the previous scan keeps its
previous packages without being parsed again; only the folder walk is repeated.

    scan_dependencies.py store/raw/dependencies.json
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import tomllib
from datetime import datetime, timezone
from urllib.parse import urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

SCHEMA = "dependencies-scan/1"
#: Depth from a project folder: a monorepo keeps `packages/*/package-lock.json` and
#: `apps/*/web/…`; deeper is vendored copies and examples.
MAX_DEPTH = 3
#: Never walked: installed trees (their lockfiles are copies of the root's), build output, and
#: fixtures, whose lockfiles describe nothing the project runs.
SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "env", ".tox", ".nox", "vendor", "Pods", "target", "build",
    "dist", ".next", ".nuxt", ".svelte-kit", ".turbo", ".cache", "__pycache__", "site-packages",
    "bower_components", ".yarn", ".pnpm-store", "examples", "example", "fixtures", "testdata", "third_party"})
#: Lockfile name -> format. `requirements*.txt` and `pylock.*.toml` are matched by pattern.
LOCKS = {
    "package-lock.json": "npm-lock", "npm-shrinkwrap.json": "npm-lock", "pnpm-lock.yaml": "pnpm-lock",
    "yarn.lock": "yarn-lock", "Pipfile.lock": "pipfile-lock", "poetry.lock": "poetry-lock",
    "uv.lock": "uv-lock", "pylock.toml": "pylock", "composer.lock": "composer-lock",
    "Cargo.lock": "cargo-lock", "go.mod": "go-mod", "Gemfile.lock": "gemfile-lock",
}
REQUIREMENTS = re.compile(r"^requirements[\w.-]*\.txt$")
PYLOCK = re.compile(r"^pylock\.[\w-]+\.toml$")
NPM_LOCKS = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock")
ECOSYSTEM = {
    "npm-lock": "npm", "pnpm-lock": "npm", "yarn-lock": "npm", "npm-manifest": "npm",
    "pipfile-lock": "PyPI", "poetry-lock": "PyPI", "uv-lock": "PyPI", "pylock": "PyPI", "requirements": "PyPI",
    "go-mod": "Go", "cargo-lock": "crates.io", "gemfile-lock": "RubyGems", "composer-lock": "Packagist",
}
#: Registries whose packages are public, so their names may go to OSV. Anything resolved from
#: another host is a private registry and stays local.
PUBLIC_HOSTS = frozenset({
    "registry.npmjs.org", "registry.yarnpkg.com", "registry.npmmirror.com", "pypi.org", "files.pythonhosted.org",
    "pypi.python.org", "github.com/rust-lang/crates.io-index", "index.crates.io", "rubygems.org",
    "repo.packagist.org", "packagist.org"})
#: Local sources a package row may carry, besides `registry`.
LOCAL_SOURCES = ("path", "workspace", "link", "git", "private")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(name: str, version: str, ecosystem: str, *, direct=None, source: str = "registry",
         dev: bool = False, pinned: bool = True) -> dict:
    row = {"name": name, "version": version, "ecosystem": ecosystem, "direct": direct,
           "local": source != "registry", "source": source}
    if dev:
        row["dev"] = True
    if not pinned:
        row["pinned"] = False
    return row


def _public_url(url: str) -> bool:
    """Whether a resolved URL points at a public registry; an unparseable one is not public."""
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return host in PUBLIC_HOSTS or f"{host}{parts.path.rstrip('/')}" in PUBLIC_HOSTS


def _npm_spec_source(spec: str) -> str:
    """The source an npm version or resolution names: `file:`, `link:`, `workspace:`, git, or a registry."""
    s = str(spec or "")
    if s.startswith(("file:", "portal:")) or s.startswith(("./", "../", "/")):
        return "path"
    if s.startswith("link:"):
        return "link"
    if s.startswith("workspace:"):
        return "workspace"
    if s.startswith(("git+", "git:", "github:", "gitlab:", "bitbucket:")) or ".git#" in s or s.startswith("git@"):
        return "git"
    if s.startswith(("http:", "https:")):
        return "registry" if _public_url(s) else "private"
    return "registry"


# ---- npm --------------------------------------------------------------------------------------------

def npm_manifest_names(text: str | None) -> tuple[set[str], set[str]]:
    """(runtime names, dev names) a `package.json` declares."""
    if not text:
        return set(), set()
    doc = json.loads(text)
    run, dev = set(), set()
    for key in ("dependencies", "optionalDependencies", "peerDependencies"):
        block = doc.get(key)
        if isinstance(block, dict):
            run |= set(block)
    block = doc.get("devDependencies")
    if isinstance(block, dict):
        dev |= set(block)
    return run, dev


def npm_lock(text: str, siblings: dict) -> list[dict]:
    """package-lock v2/v3: one row per `packages` entry; `link: true` is a symlink, skipped."""
    doc = json.loads(text)
    version = doc.get("lockfileVersion")
    packages = doc.get("packages")
    if not isinstance(packages, dict) or version not in (2, 3):
        raise ValueError(f"lockfileVersion {version!r} is not read (only 2 and 3 carry the `packages` map)")
    root = packages.get("") or {}
    direct = set()
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        if isinstance(root.get(key), dict):
            direct |= set(root[key])
    out = []
    for key, entry in packages.items():
        if not key or not isinstance(entry, dict) or entry.get("link") is True:
            continue
        if "node_modules/" not in key:
            # A workspace member listed by its folder: the project's own code.
            name = entry.get("name") or key.rsplit("/", 1)[-1]
            out.append(_row(name, str(entry.get("version") or "0.0.0"), "npm", direct=None, source="workspace"))
            continue
        name = entry.get("name") or key.rsplit("node_modules/", 1)[-1]
        ver = entry.get("version")
        if not ver:
            continue
        top = key.count("node_modules/") == 1 and key.startswith("node_modules/")
        source = _npm_spec_source(entry.get("resolved") or "") if entry.get("resolved") else "registry"
        out.append(_row(name, str(ver), "npm", direct=(top and name in direct), source=source,
                        dev=entry.get("dev") is True))
    return out


def _split_npm_spec(spec: str) -> tuple[str, str]:
    """`@scope/name@range` -> (`@scope/name`, `range`)."""
    spec = spec.strip().strip('"').strip("'")
    at = spec.find("@", 1)
    return (spec, "") if at < 0 else (spec[:at], spec[at + 1:])


def pnpm_lock(text: str, siblings: dict) -> list[dict]:
    """The `packages:` keys of pnpm-lock v5 (`/name/1.2.3`), v6 (`/name@1.2.3`) and v9 (`name@1.2.3`),
    and `importers` (or v5's top-level `dependencies`) for the direct names. A narrow reader of the
    subset the format uses; anything else in the file is skipped."""
    lines = text.splitlines()
    if not any(line.startswith("lockfileVersion") for line in lines[:5]):
        raise ValueError("no lockfileVersion line: not a pnpm lockfile")
    section, sub = None, None
    direct: set[str] = set()
    out: list[dict] = []
    dev_names: set[str] = set()
    entry: dict | None = None
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0:
            section = line.rstrip(":")
            sub = None
            continue
        if section in ("dependencies", "devDependencies", "optionalDependencies") and indent == 2:
            direct.add(_unquote(line.split(":", 1)[0]))
            if section == "devDependencies":
                dev_names.add(_unquote(line.split(":", 1)[0]))
        elif section == "importers":
            if indent == 4:
                sub = line.rstrip(":")
            elif indent == 6 and sub in ("dependencies", "devDependencies", "optionalDependencies"):
                direct.add(_unquote(line.split(":", 1)[0]))
                if sub == "devDependencies":
                    dev_names.add(_unquote(line.split(":", 1)[0]))
        elif section == "packages":
            if indent == 2 and line.endswith(":"):
                entry = _pnpm_key(_unquote(line[:-1]))
                if entry:
                    out.append(entry)
            elif indent == 4 and entry is not None and line.replace(" ", "") == "dev:true":
                entry["dev"] = True
    for row in out:
        row["direct"] = row["name"] in direct if direct else None
    return out


def _unquote(s: str) -> str:
    return s.strip().strip("'").strip('"')


def _pnpm_key(key: str) -> dict | None:
    key = re.sub(r"\(.*$", "", key)                     # v6/v9 peer suffix: `(react@18.0.0)`
    if key.startswith(("file:", "link:")) or "file:" in key or "link:" in key:
        name, spec = _split_npm_spec(key.lstrip("/"))
        return _row(name, spec or "0.0.0", "npm", source="path" if "file:" in key else "link")
    key = key.lstrip("/")
    v5 = re.match(r"^(@?[^@/]+(?:/[^@/]+)?)/(\d[^/_]*)(?:_.*)?$", key)  # v5: name/1.2.3[_peer@1]
    if v5:
        name, version = v5.group(1), v5.group(2)
    elif "@" in key[1:]:
        name, version = _split_npm_spec(key)            # v6, v9
    else:
        return None
    if not name or not version or not version[0].isdigit():
        return None
    return _row(name, version, "npm")


def yarn_lock(text: str, siblings: dict) -> list[dict]:
    """yarn.lock v1 (`version "1.2.3"`) and Berry (`version: 1.2.3`, `resolution: …`). Workspace,
    file, link and portal entries are local; direct names come from the `package.json` beside it."""
    run, dev = npm_manifest_names(siblings.get("package.json"))
    out: list[dict] = []
    header: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.startswith("#") or raw.startswith("__metadata"):
            if raw.startswith("__metadata"):
                header = None
            continue
        if not raw.startswith(" ") and raw.rstrip().endswith(":"):
            header = raw.rstrip()[:-1]
            continue
        if header is None:
            continue
        stripped = raw.strip()
        m = re.match(r'^version:?\s+"?([^"\s]+)"?$', stripped)
        if not m or raw.startswith("    "):
            continue
        first = header.split(",")[0].strip()
        name, spec = _split_npm_spec(first)
        protocol = spec.split(":", 1)[0] if ":" in spec else ""
        source = {"workspace": "workspace", "file": "path", "link": "link", "portal": "path",
                  "exec": "path", "git": "git", "github": "git"}.get(protocol, "registry")
        if spec.startswith(("http:", "https:")):
            source = "registry" if _public_url(spec) else "private"
        if spec.startswith(("git+", "git@")):
            source = "git"
        version = m.group(1)
        out.append(_row(name, version, "npm", direct=(name in run or name in dev) if (run or dev) else None,
                        source=source, dev=name in dev and name not in run))
        header = None
    return out


def npm_manifest(text: str, siblings: dict) -> list[dict]:
    """A `package.json` with no lockfile beside it: its direct names and DECLARED ranges, `pinned: false`,
    so no advisory is matched to a range (a range is not a version anything runs)."""
    doc = json.loads(text)
    out = []
    for key, dev in (("dependencies", False), ("optionalDependencies", False), ("peerDependencies", False),
                     ("devDependencies", True)):
        block = doc.get(key)
        if not isinstance(block, dict):
            continue
        for name, spec in block.items():
            out.append(_row(name, str(spec), "npm", direct=True, source=_npm_spec_source(str(spec)), dev=dev,
                            pinned=bool(re.fullmatch(r"\d+\.\d+\.\d+[\w.+-]*", str(spec)))))
    return out


# ---- Python -----------------------------------------------------------------------------------------

def _pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pyproject_names(text: str | None) -> set[str]:
    if not text:
        return set()
    doc = tomllib.loads(text)
    out: set[str] = set()
    proj = doc.get("project") or {}
    specs = list(proj.get("dependencies") or [])
    for group in (proj.get("optional-dependencies") or {}).values():
        specs += list(group or [])
    for group in (doc.get("dependency-groups") or {}).values():
        specs += [s for s in group or [] if isinstance(s, str)]
    for spec in specs:
        name = re.split(r"[<>=!~\[; @]", str(spec), maxsplit=1)[0].strip()
        if name:
            out.add(_pep503(name))
    tool = (doc.get("tool") or {}).get("poetry") or {}
    for key in ("dependencies", "dev-dependencies"):
        out |= {_pep503(k) for k in (tool.get(key) or {}) if k.lower() != "python"}
    for group in (tool.get("group") or {}).values():
        out |= {_pep503(k) for k in ((group or {}).get("dependencies") or {})}
    return out


def pipfile_lock(text: str, siblings: dict) -> list[dict]:
    doc = json.loads(text)
    sources = {s.get("name"): s.get("url", "") for s in ((doc.get("_meta") or {}).get("sources") or [])
               if isinstance(s, dict)}
    direct: set[str] = set()
    if siblings.get("Pipfile"):
        pipfile = tomllib.loads(siblings["Pipfile"])
        for key in ("packages", "dev-packages"):
            direct |= {_pep503(k) for k in (pipfile.get(key) or {})}
    out = []
    for section, dev in (("default", False), ("develop", True)):
        for name, entry in (doc.get(section) or {}).items():
            if not isinstance(entry, dict):
                continue
            if entry.get("path") or entry.get("file") or entry.get("editable"):
                out.append(_row(name, str(entry.get("version") or "0").lstrip("="), "PyPI", source="path", dev=dev))
                continue
            if entry.get("git"):
                out.append(_row(name, str(entry.get("ref") or "0"), "PyPI", source="git", dev=dev))
                continue
            version = str(entry.get("version") or "")
            if not version.startswith("=="):
                continue
            index = entry.get("index")
            source = "private" if index and sources.get(index) and not _public_url(sources[index]) else "registry"
            out.append(_row(name, version[2:], "PyPI", direct=(_pep503(name) in direct) if direct else None,
                            source=source, dev=dev))
    return out


def poetry_lock(text: str, siblings: dict) -> list[dict]:
    doc = tomllib.loads(text)
    direct = _pyproject_names(siblings.get("pyproject.toml"))
    out = []
    for pkg in doc.get("package") or []:
        name, version = pkg.get("name"), pkg.get("version")
        if not name or not version:
            continue
        src = pkg.get("source") or {}
        kind = src.get("type")
        source = {"directory": "path", "file": "path", "git": "git", "url": "private"}.get(kind, "registry")
        if kind == "legacy" and not _public_url(src.get("url", "")):
            source = "private"
        dev = pkg.get("category") == "dev"
        out.append(_row(name, str(version), "PyPI", direct=(_pep503(name) in direct) if direct else None,
                        source=source, dev=dev))
    return out


def uv_lock(text: str, siblings: dict) -> list[dict]:
    """uv.lock; its schema is uv's own and may change, so an unknown shape is an error, not a guess."""
    doc = tomllib.loads(text)
    packages = doc.get("package")
    if packages is None and "version" in doc:
        return []                                       # a project with no dependencies writes no table
    if not isinstance(packages, list):
        raise ValueError("no [[package]] table")
    direct: set[str] = set()
    out = []
    for pkg in packages:
        src = pkg.get("source") or {}
        if any(k in src for k in ("editable", "virtual", "directory", "path")):
            for dep in pkg.get("dependencies") or []:
                direct.add(_pep503(dep.get("name", "")))
            for group in (pkg.get("dev-dependencies") or {}).values():
                direct |= {_pep503(d.get("name", "")) for d in group or []}
            for group in (pkg.get("optional-dependencies") or {}).values():
                direct |= {_pep503(d.get("name", "")) for d in group or []}
    for pkg in packages:
        name, version, src = pkg.get("name"), pkg.get("version"), pkg.get("source") or {}
        if not name:
            continue
        if any(k in src for k in ("editable", "virtual", "directory", "path")):
            out.append(_row(name, str(version or "0"), "PyPI", source="path"))
            continue
        if "git" in src:
            out.append(_row(name, str(version or "0"), "PyPI", source="git"))
            continue
        if not version:
            continue
        registry = src.get("registry", "")
        source = "registry" if not registry or _public_url(registry) else "private"
        out.append(_row(name, str(version), "PyPI", direct=_pep503(name) in direct if direct else None,
                        source=source))
    return out


def pylock(text: str, siblings: dict) -> list[dict]:
    """PEP 751 `pylock.toml`: `[[packages]]` with name and version; directory and vcs entries are local."""
    doc = tomllib.loads(text)
    out = []
    for pkg in doc.get("packages") or []:
        name, version = pkg.get("name"), pkg.get("version")
        if not name:
            continue
        if "directory" in pkg:
            out.append(_row(name, str(version or "0"), "PyPI", source="path"))
        elif "vcs" in pkg:
            out.append(_row(name, str(version or "0"), "PyPI", source="git"))
        elif version:
            index = pkg.get("index", "")
            out.append(_row(name, str(version), "PyPI",
                            source="registry" if not index or _public_url(index) else "private"))
    return out


def requirements(text: str, siblings: dict) -> list[dict]:
    """`name==version` pins; a range is not a version anything runs and is skipped, and a path or VCS
    line is local. A pip-compile file (`# via` lines) lists its transitive pins too, so `direct` is
    unknown there."""
    compiled = "# via" in text
    out = []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].split("\t#", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-e ", "--editable")):
            target = line.split(None, 1)[1] if " " in line else ""
            name = re.search(r"#egg=([\w.-]+)", target)
            out.append(_row(name.group(1) if name else target.rsplit("/", 1)[-1], "0", "PyPI",
                            source="git" if target.startswith(("git+", "hg+")) else "path"))
            continue
        if line.startswith("-"):
            continue
        if " @ " in line:
            name, target = line.split(" @ ", 1)
            source = "git" if target.strip().startswith(("git+", "hg+")) else (
                "path" if target.strip().startswith("file:") else "registry" if _public_url(target.strip()) else "private")
            out.append(_row(re.split(r"[\[;]", name)[0].strip(), "0", "PyPI", source=source))
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*===?\s*([^\s;,]+)", line)
        if m:
            out.append(_row(m.group(1), m.group(3), "PyPI", direct=None if compiled else True))
    return out


# ---- Go, Rust, Ruby, PHP -------------------------------------------------------------------------------

def go_mod(text: str, siblings: dict) -> list[dict]:
    """`require` lines and blocks; `// indirect` marks a transitive one; a `replace` onto a path is local.
    The `v` prefix is dropped, as OSV's Go records spell versions without it."""
    rows: dict[str, dict] = {}
    replaced: set[str] = set()
    block = None
    for raw in text.splitlines():
        line = raw.strip()
        comment = ""
        if "//" in line:
            line, comment = line.split("//", 1)
            line = line.strip()
        if not line:
            continue
        if line.endswith("(") and line.split()[0] in ("require", "replace", "exclude", "retract", "tool"):
            block = line.split()[0]
            continue
        if line == ")":
            block = None
            continue
        words = line.split()
        kind = block
        if words[0] in ("require", "replace", "exclude", "retract", "module", "go", "toolchain", "tool"):
            kind, words = words[0], words[1:]
        if kind == "require" and len(words) >= 2:
            rows[words[0]] = _row(words[0], words[1].lstrip("v"), "Go", direct="indirect" not in comment)
        elif kind == "replace" and "=>" in words:
            target = words[words.index("=>") + 1:]
            if target and target[0].startswith(("./", "../", "/")):
                replaced.add(words[0])
    for name in replaced:
        if name in rows:
            rows[name].update({"local": True, "source": "path"})
    return list(rows.values())


def cargo_lock(text: str, siblings: dict) -> list[dict]:
    doc = tomllib.loads(text)
    packages = doc.get("package") or []
    direct: set[str] = set()
    for pkg in packages:
        if not pkg.get("source"):
            direct |= {str(d).split(" ", 1)[0] for d in pkg.get("dependencies") or []}
    out = []
    for pkg in packages:
        name, version, src = pkg.get("name"), pkg.get("version"), str(pkg.get("source") or "")
        if not name or not version:
            continue
        if not src:
            source = "workspace"
        elif src.startswith("git+"):
            source = "git"
        elif src.startswith(("registry+", "sparse+")):
            url = src.split("+", 1)[1]
            source = "registry" if _public_url(url) else "private"
        else:
            source = "private"
        out.append(_row(name, str(version), "crates.io", direct=name in direct if source != "workspace" else None,
                        source=source))
    return out


def gemfile_lock(text: str, siblings: dict) -> list[dict]:
    """`GEM` specs are registry gems; `PATH` and `GIT` specs are local; `DEPENDENCIES` names the direct ones.
    A platform suffix (`1.15.4-arm64-darwin`) is dropped from the version."""
    section, in_specs, remote = None, False, ""
    found: list[tuple[str, str, str]] = []
    direct: set[str] = set()
    for raw in text.splitlines():
        if not raw.strip():
            in_specs = False
            continue
        if not raw.startswith(" "):
            section, in_specs, remote = raw.strip(), False, ""
            continue
        stripped = raw.strip()
        if section == "DEPENDENCIES" and raw.startswith("  ") and not raw.startswith("   "):
            direct.add(re.split(r"[\s(!]", stripped, maxsplit=1)[0])
            continue
        if stripped.startswith("remote:"):
            remote = stripped.split(":", 1)[1].strip()
        if stripped == "specs:":
            in_specs = True
            continue
        if in_specs and raw.startswith("    ") and not raw.startswith("     "):
            m = re.match(r"^([\w.-]+) \(([^)]+)\)$", stripped)
            if m:
                version = m.group(2).split("-", 1)[0]
                source = {"GEM": "registry", "PATH": "path", "GIT": "git"}.get(section, "private")
                if source == "registry" and remote and not _public_url(remote):
                    source = "private"
                found.append((m.group(1), version, source))
    return [_row(n, v, "RubyGems", direct=(n in direct) if direct else None, source=s) for n, v, s in found]


def composer_lock(text: str, siblings: dict) -> list[dict]:
    """`packages` and `packages-dev`; a `path` dist is local. A leading `v` is dropped (Packagist's OSV
    records spell versions without it; not verified for every package)."""
    doc = json.loads(text)
    direct: set[str] = set()
    if siblings.get("composer.json"):
        manifest = json.loads(siblings["composer.json"])
        for key in ("require", "require-dev"):
            direct |= set(manifest.get(key) or {})
    out = []
    for key, dev in (("packages", False), ("packages-dev", True)):
        for pkg in doc.get(key) or []:
            name, version = pkg.get("name"), str(pkg.get("version") or "")
            if not name or not version:
                continue
            dist = (pkg.get("dist") or {}).get("type")
            source = "path" if dist == "path" else "git" if version.startswith("dev-") else "registry"
            if re.match(r"^v\d", version):
                version = version[1:]
            out.append(_row(name, version, "Packagist", direct=(name in direct) if direct else None,
                            source=source, dev=dev))
    return out


PARSERS = {
    "npm-lock": npm_lock, "pnpm-lock": pnpm_lock, "yarn-lock": yarn_lock, "npm-manifest": npm_manifest,
    "pipfile-lock": pipfile_lock, "poetry-lock": poetry_lock, "uv-lock": uv_lock, "pylock": pylock,
    "requirements": requirements, "go-mod": go_mod, "cargo-lock": cargo_lock, "gemfile-lock": gemfile_lock,
    "composer-lock": composer_lock,
}
#: Manifests read beside a lockfile, for its direct names.
SIBLINGS = ("package.json", "pyproject.toml", "Pipfile", "composer.json")


def format_of(name: str, names_here: set[str], npm_locked: bool = False) -> str | None:
    """The format of a file, or None. A `package.json` is read alone only when no npm lockfile sits beside
    it or above it in the folder: a workspace member's manifest is covered by the root's lock."""
    if name in LOCKS:
        return LOCKS[name]
    if REQUIREMENTS.match(name):
        return "requirements"
    if PYLOCK.match(name):
        return "pylock"
    if name == "package.json" and not npm_locked and not any(lock in names_here for lock in NPM_LOCKS):
        return "npm-manifest"
    return None


def lockfiles(folder: pathlib.Path) -> list[tuple[pathlib.Path, str]]:
    """(file, format) for every readable lockfile under `folder`, at most MAX_DEPTH down."""
    found: list[tuple[pathlib.Path, str]] = []
    stack = [(folder, 0, False)]
    while stack:
        d, depth, locked = stack.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        names = {e.name for e in entries if e.is_file(follow_symlinks=False)}
        locked = locked or any(lock in names for lock in NPM_LOCKS)
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                if depth < MAX_DEPTH and e.name not in SKIP_DIRS and not e.name.startswith("."):
                    stack.append((pathlib.Path(e.path), depth + 1, locked))
            elif e.is_file(follow_symlinks=False):
                fmt = format_of(e.name, names, locked)
                if fmt:
                    found.append((pathlib.Path(e.path), fmt))
    return sorted(found)


def private_rules(path: pathlib.Path | None) -> tuple[set[str], tuple[str, ...]]:
    """(names, npm scopes) the person named private; an unreadable file names nothing."""
    if path is None or not path.is_file():
        return set(), ()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), ()
    names = {str(n) for n in doc.get("names") or [] if isinstance(n, str)}
    scopes = tuple(s.rstrip("/") + "/" for s in doc.get("npm_scopes") or [] if isinstance(s, str) and s.startswith("@"))
    return names, scopes


def read_file(path: pathlib.Path, fmt: str, private: tuple[set[str], tuple[str, ...]] = (set(), ())) -> list[dict]:
    """The packages one lockfile resolves. Raises on a file that cannot be read or parsed."""
    text = path.read_text(encoding="utf-8")
    siblings = {}
    for name in SIBLINGS:
        sib = path.parent / name
        if sib != path and sib.is_file():
            try:
                siblings[name] = sib.read_text(encoding="utf-8")
            except OSError:
                pass
    rows = PARSERS[fmt](text, siblings)
    names, scopes = private
    for row in rows:
        if not row["local"] and (row["name"] in names or (scopes and row["name"].startswith(scopes))):
            row.update({"local": True, "source": "private"})
    return rows


def scan_folder(folder: pathlib.Path, label: str, previous: dict[tuple[str, str], dict],
                private=(set(), ())) -> tuple[list[dict], list[dict]]:
    """(file rows, degraded) for one project folder. A file unchanged since `previous` is not parsed."""
    files, degraded = [], []
    for path, fmt in lockfiles(folder):
        rel = path.relative_to(folder).as_posix()
        try:
            st = path.stat()
        except OSError as exc:
            degraded.append({"source": "dependencies", "file": f"{label}/{rel}",
                             "reason": f"cannot be read: {type(exc).__name__}"})
            continue
        row = {"folder": label, "path": rel, "format": fmt, "ecosystem": ECOSYSTEM[fmt],
               "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        old = previous.get((label, rel))
        if old and old.get("size") == st.st_size and old.get("mtime_ns") == st.st_mtime_ns \
                and old.get("format") == fmt and "packages" in old:
            row["packages"] = old["packages"]
        else:
            try:
                row["packages"] = read_file(path, fmt, private)
            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError,
                    IndexError, tomllib.TOMLDecodeError) as exc:
                row["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                degraded.append({"source": "dependencies", "file": f"{label}/{rel}",
                                 "reason": f"{fmt} could not be parsed ({row['error']}); its dependencies "
                                           f"are unknown, not absent"})
        files.append(row)
    return files, degraded


def scan(projects: list[dict], data_root: pathlib.Path, previous: dict | None = None,
         private=(set(), ())) -> dict:
    """The scan document over every project's local folders."""
    before: dict[tuple[str, str], dict] = {}
    for p in (previous or {}).get("projects") or []:
        for f in p.get("files") or []:
            before[(f.get("folder"), f.get("path"))] = f
    out, degraded, seen = [], [], set()
    import step_budget
    complete = True
    for p in projects:
        if not isinstance(p, dict) or not isinstance(p.get("id"), str):
            continue
        if step_budget.remaining() < 5:
            complete = False
            degraded.append({"source": "dependencies",
                             "reason": "the step's time ran out; the projects not read keep the previous scan"})
            kept = [q for q in (previous or {}).get("projects") or [] if q.get("project") == p["id"]]
            out.extend(kept)
            continue
        files, pdeg = [], []
        for folder in p.get("local_folders") or []:
            if not isinstance(folder, str):
                continue
            path = data_root / folder
            if not path.is_dir():
                continue
            real = str(path.resolve())
            if real in seen:
                continue
            seen.add(real)
            got, bad = scan_folder(path, folder, before, private)
            files += got
            pdeg += bad
        if files or pdeg:
            out.append({"project": p["id"], "files": files, "degraded": pdeg})
    return {"schema": SCHEMA, "scanned_at": now(), "complete": complete, "projects": out, "degraded": degraded}


def main(argv: list[str]) -> int:
    import atomic
    import paths
    out = pathlib.Path(argv[1]) if len(argv) > 1 else paths.SCRATCH / "dependencies.json"
    try:
        projects = json.loads((paths.REGISTRY / "projects.json").read_text(encoding="utf-8")).get("projects") or []
    except (OSError, ValueError) as exc:
        print(f"dependencies: the project registry cannot be read ({type(exc).__name__}); nothing written",
              file=sys.stderr)
        return 1
    previous = None
    if out.is_file():
        try:
            previous = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
    doc = scan(projects, paths.DATA, previous, private_rules(paths.config_file("private_packages.json")))
    atomic.write_json(out, doc)
    rows = [r for p in doc["projects"] for f in p["files"] for r in f.get("packages") or []]
    bad = sum(len(p["degraded"]) for p in doc["projects"])
    print(f"dependencies: {len(doc['projects'])} project(s), "
          f"{sum(len(p['files']) for p in doc['projects'])} lockfile(s), {len(rows)} package row(s), "
          f"{sum(1 for r in rows if r['local'])} local; {bad} file(s) unreadable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
