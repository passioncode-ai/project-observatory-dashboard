#!/usr/bin/env python3
"""Validate the bundled MCP wire schemas or check their public release bytes.

The manifest in the source tree is portable. --local-manifest writes a private
installation-specific copy beneath OBSERVATORY_HOME; never publish that copy.
--check performs anonymous HTTP reads. --local checks bundled files offline.
External host admission is unverified; these checks establish local consistency
and publication equality only. The package release pipeline publishes schemas.
"""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import configuration
import workspace
from fabric_hash import compute

MANIFEST = ROOT / "fabric-agent.json"
LOCK = json.loads((ROOT / "fabric-contract.lock.json").read_text())
PUBLIC_NWO = "ssheleg/project-observatory-open-source"
RAW = "https://raw.githubusercontent.com/" + PUBLIC_NWO
SCHEMA_RELEASE = LOCK["schemaRelease"]
RELEASE_PATTERN = r"v[0-9]+\.[0-9]+\.[0-9]+"
if not re.fullmatch(RELEASE_PATTERN, SCHEMA_RELEASE):
    raise ValueError("Invalid pinned schema release")
#: The schema set of the FIRST release, v0.2.0. Its identifiers are published and
#: never rewritten (README, "Repository name and existing installations"), which
#: is why it keeps the repository's old address.
PREFIX = f"{RAW}/{SCHEMA_RELEASE}/observatory/engine/fabric/"
PUBLISHABLE = (("fabric/schemas", "schemas", "*.json"),
               ("fabric/fixtures", "fixtures", "*.json"))


def releases() -> list[dict]:
    """Every schema release the manifest pins, each with the files it published.

    ONE PIN PER FILE, NOT ONE PIN FOR ALL. A capability added after v0.2.0 cannot
    point at v0.2.0, whose tag does not hold its schema, and moving the old
    schemas to a new tag would rewrite identifiers hosts already admitted. So the
    lock lists releases, and each file belongs to exactly one of them: the release
    whose tag first published it, under the repository's name at that time. A
    lock without `releases` is the original single-release shape."""
    listed = LOCK.get("releases")
    if not listed:
        return [{"release": SCHEMA_RELEASE, "repository": PUBLIC_NWO, "files": sorted(staged())}]
    return listed


def prefix_for(release: dict) -> str:
    return (f"https://raw.githubusercontent.com/{release['repository']}/{release['release']}"
            f"/observatory/engine/fabric/")


def home_of() -> dict[str, str]:
    """Bundled file path below fabric/ -> the URL prefix of the release that publishes it."""
    out: dict[str, str] = {}
    for release in releases():
        for name in release.get("files", []):
            out.setdefault(name, prefix_for(release))
    return out


def resolve(uri: str) -> str | None:
    """The bundled path a pinned URI names, or None when no release publishes it."""
    for name, prefix in home_of().items():
        if uri == prefix + name:
            return name
    return None


def revision() -> int:
    return int(json.loads(MANIFEST.read_text())["provider"]["revision"])


def staged(rev: int | None = None) -> dict[str, str]:
    """Reviewed schema and fixture text keyed by its path below fabric/."""
    return {f"{dest}/{path.name}": path.read_text(encoding="utf-8")
            for source, dest, pattern in PUBLISHABLE
            for path in sorted((ROOT / source).glob(pattern))}


def manifest_uris() -> list[str]:
    return sorted(set(re.findall(r'"(https://raw\.githubusercontent\.com/[^"]+)"',
                                 MANIFEST.read_text(encoding="utf-8"))))


def release_failures(files: dict[str, str]) -> list[str]:
    failures = []
    seen: dict[str, int] = {}
    for release in releases():
        if not re.fullmatch(RELEASE_PATTERN, str(release.get("release", ""))):
            failures.append(f"a pinned release is not a version tag: {release.get('release')!r}")
        if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", str(release.get("repository", ""))):
            failures.append(f"a pinned release names no repository: {release.get('release')!r}")
        for name in release.get("files", []):
            seen[name] = seen.get(name, 0) + 1
            if name not in files:
                failures.append(f"a pinned release lists a file that is not bundled: {name}")
    for name in files:
        if seen.get(name, 0) != 1:
            failures.append(f"a bundled file belongs to {seen.get(name, 0)} releases instead of one: {name}")
    return failures


def local_failures() -> list[str]:
    doc = json.loads(MANIFEST.read_text())
    files = staged()
    failures = []
    if not files or not manifest_uris():
        failures.append("schema/fixture publication set is empty")
    else:
        failures += release_failures(files)
    if doc.get("provider", {}).get("contentHash") != compute(doc):
        failures.append("manifest content hash differs")
    for uri in manifest_uris():
        name = resolve(uri)
        if name is None or name not in files:
            failures.append("manifest URI does not name a bundled release schema or fixture")
    prefixes = home_of()
    for name, text in files.items():
        value = json.loads(text)
        if name.startswith("schemas/") and value.get("$id") != prefixes.get(name, PREFIX) + name:
            failures.append(f"schema identity differs: {name}")
    for cap in doc.get("capabilities", []):
        if cap.get("profile", {}).get("connection", {}).get("executableRef") != "observatory-install:mcp-server":
            failures.append("source manifest must remain a portable installation template")
    return failures


def fetch(url: str) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "observatory-schema-check"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, ""
    except OSError:
        return 0, ""


def cmd_check() -> int:
    failures = local_failures()
    prefixes = home_of()
    for name, wanted in staged().items():
        status, actual = fetch(prefixes.get(name, PREFIX) + name)
        if status != 200 or actual != wanted:
            failures.append(f"published file differs or is unavailable: {name}; HTTP {status}")
    for failure in failures:
        print(failure, file=sys.stderr)
    print(json.dumps({"publication_identical": not failures, "external_host_admission": "unverified"}))
    return 1 if failures else 0


def local_manifest(destination: Path) -> None:
    base = configuration.home().absolute()
    destination = destination.expanduser().absolute()
    configuration.validate_workspace(base, required=True)
    workspace.reject_symlinks(destination)
    if base not in destination.parents:
        raise configuration.ConfigurationError("Local manifest must remain beneath OBSERVATORY_HOME")
    if destination.exists():
        raise configuration.ConfigurationError("Local manifest destination already exists")
    doc = copy.deepcopy(json.loads(MANIFEST.read_text()))
    for cap in doc["capabilities"]:
        cap["profile"]["connection"] = {
            "mode": "stdio", "executableRef": Path(sys.executable).resolve().as_uri(),
            "args": [str(ROOT / "mcp/server.py"), "--stdio"]}
    doc["provider"]["contentHash"] = compute(doc)
    workspace.write_json(destination, doc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--local", action="store_true")
    group.add_argument("--check", action="store_true")
    group.add_argument("--local-manifest", type=Path)
    group.add_argument("--stage", action="store_true")
    group.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    if args.stage or args.publish:
        parser.error("Schemas ship with the package release; use --local to validate, --check after publication")
    if args.local_manifest:
        try:
            local_manifest(args.local_manifest)
        except (configuration.ConfigurationError, OSError):
            print("Local manifest refused; check initialized private home and unused destination", file=sys.stderr)
            return 2
        print("Private installation manifest written; do not publish it")
        return 0
    if args.check:
        return cmd_check()
    failures = local_failures()
    for failure in failures:
        print(failure, file=sys.stderr)
    print(json.dumps({"bundled_schema_references_valid": not failures, "external_host_admission": "unverified"}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
