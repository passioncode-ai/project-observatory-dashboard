#!/usr/bin/env python3
"""The desktop app's release files: names, receipts, and the unsigned-build rule (W9, PL-10).

    python tools/desktop_release.py preflight --tag v0.22.0 --signing false
    python tools/desktop_release.py package --platform windows --arch x64 --signing true \
        --signatures report.json --bundle desktop/src-tauri/target/release/bundle --out dist/release

`feed` writes `latest.json`, the update feed the app reads (fabric-workspace platforms.md PL-04): one entry
per platform the release ships, each naming its file under this tag and carrying that file's own minisign
signature (`tauri signer sign`); `check` refuses a feed missing a platform or naming another release's file.

`preflight` refuses a tag that is not `v<pyproject version>`, a CHANGELOG without its `## X.Y.Z`
section, and an unsigned Windows release whose section does not say so (fabric-workspace
platforms.md PL-03). `package` copies each built package under the organization's file names
(`ProjectObservatory-<version>-<platform>-<arch>…`, our architecture names, never the tool's) and
writes the receipt every platform job writes (PL-10): version, commit, arch, windows_authenticode,
checks.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNSIGNED_LINE = "Windows installers are not Authenticode-signed yet"


def version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def changelog_section(ver: str) -> str | None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(ver)}\b.*?(?=^## |\Z)", text, re.M | re.S)
    return m.group(0) if m else None


def preflight(tag: str, signing: bool) -> list[str]:
    ver = version()
    problems = []
    if re.fullmatch(r"v\d+\.\d+\.\d+(-rc\.\d+)?", tag) is None or tag.split("-rc.")[0] != f"v{ver}":
        problems.append(f"tag {tag} does not name the version in pyproject.toml ({ver})")
    section = changelog_section(ver)
    if section is None:
        problems.append(f"CHANGELOG.md has no '## {ver}' section")
    elif not signing and UNSIGNED_LINE not in section:
        problems.append(f"Windows signing is off, so the '## {ver}' section must say: "
                        f"\"{UNSIGNED_LINE}; SmartScreen warns once. Verify them with SHA256SUMS.\"")
    return problems


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package(platform: str, arch: str, signing: bool, bundle: Path, out: Path,
            signatures: Path | None, checks: dict) -> dict:
    ver = version()
    out.mkdir(parents=True, exist_ok=True)
    base = f"ProjectObservatory-{ver}-{platform}-{arch}"
    files = {}
    if platform == "windows":
        [setup] = sorted((bundle / "nsis").glob("*-setup.exe"))
        files[f"{base}-setup.exe"] = setup
    else:
        [image] = sorted((bundle / "appimage").glob("*.AppImage"))
        [deb] = sorted((bundle / "deb").glob("*.deb"))
        files[f"{base}.AppImage"] = image
        files[f"{base}.deb"] = deb
    for name, source in files.items():
        shutil.copyfile(source, out / name)
    receipt = {
        "product": "Project Observatory desktop",
        "version": ver,
        "commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip(),
        "platform": platform,
        "arch": arch,
        "files": {name: {"sha256": sha256(out / name), "bytes": (out / name).stat().st_size} for name in files},
        "checks": checks,
    }
    if platform == "windows":
        if signing:
            receipt["windows_authenticode"] = "SIGNED"
            receipt["signatures"] = json.loads(signatures.read_text(encoding="utf-8-sig")) if signatures else []
        else:
            receipt["windows_authenticode"] = "NOT_SIGNED"
            receipt["authenticode"] = {"status": "NOT_SIGNED",
                                       "reason": "Windows signing (Azure Artifact Signing) is switched off: the "
                                                 "release environment variable AZURE_SIGNING_ENABLED is not true."}
    (out / f"{base}-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


REPOSITORY = "passioncode-ai/project-observatory-dashboard"
#: Tauri's platform keys, and the file each one updates from (a .deb does not update itself).
FEED = {
    "windows-x86_64": "windows-x64-setup.exe",
    "windows-aarch64": "windows-arm64-setup.exe",
    "linux-x86_64": "linux-x64.AppImage",
    "linux-aarch64": "linux-arm64.AppImage",
}


def feed(tag: str, folder: Path, notes: str = "") -> dict:
    ver = tag.removeprefix("v").split("-rc.")[0]
    platforms = {}
    for key, suffix in FEED.items():
        name = f"ProjectObservatory-{ver}-{suffix}"
        sig = folder / f"{name}.sig"
        if (folder / name).is_file() and sig.is_file():
            platforms[key] = {"signature": sig.read_text(encoding="utf-8").strip(),
                              "url": f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"}
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"version": ver, "notes": notes[:4000], "pub_date": stamp, "platforms": platforms}


def check_feed(doc: dict, tag: str, folder: Path) -> list[str]:
    problems = []
    ver = tag.removeprefix("v").split("-rc.")[0]
    if doc.get("version") != ver:
        problems.append(f"latest.json announces {doc.get('version')}, the tag is {tag}")
    for key, suffix in FEED.items():
        entry = (doc.get("platforms") or {}).get(key)
        name = f"ProjectObservatory-{ver}-{suffix}"
        if not entry:
            problems.append(f"latest.json has no {key}: the release ships it, so an installed copy would never update")
            continue
        if entry.get("url") != f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}":
            problems.append(f"{key} names {entry.get('url')}, not this release's {name}")
        sig = folder / f"{name}.sig"
        if not sig.is_file() or sig.read_text(encoding="utf-8").strip() != entry.get("signature"):
            problems.append(f"{key}'s signature is not {name}.sig's")
    return problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pre = sub.add_parser("preflight")
    pre.add_argument("--tag", required=True)
    pre.add_argument("--signing", choices=["true", "false"], required=True)
    pkg = sub.add_parser("package")
    pkg.add_argument("--platform", choices=["windows", "linux"], required=True)
    pkg.add_argument("--arch", choices=["x64", "arm64"], required=True)
    pkg.add_argument("--signing", choices=["true", "false"], default="false")
    pkg.add_argument("--bundle", type=Path, required=True)
    pkg.add_argument("--out", type=Path, required=True)
    pkg.add_argument("--signatures", type=Path)
    pkg.add_argument("--check", action="append", default=[], help="NAME=PASS|FAIL")
    fd = sub.add_parser("feed")
    fd.add_argument("--tag", required=True)
    fd.add_argument("--dir", type=Path, required=True)
    ck = sub.add_parser("check")
    ck.add_argument("--tag", required=True)
    ck.add_argument("--dir", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd in ("feed", "check"):
        path = args.dir / "latest.json"
        if args.cmd == "feed":
            section = changelog_section(version()) or ""
            path.write_text(json.dumps(feed(args.tag, args.dir, section), indent=2) + "\n", encoding="utf-8")
        problems = check_feed(json.loads(path.read_text(encoding="utf-8")), args.tag, args.dir)
        for p in problems:
            print(f"desktop_release: {p}", file=sys.stderr)
        return 1 if problems else 0
    if args.cmd == "preflight":
        problems = preflight(args.tag, args.signing == "true")
        for p in problems:
            print(f"desktop_release: {p}", file=sys.stderr)
        return 1 if problems else 0
    checks = dict(c.split("=", 1) for c in args.check)
    receipt = package(args.platform, args.arch, args.signing == "true", args.bundle, args.out, args.signatures, checks)
    print(json.dumps({"files": list(receipt["files"]), "windows_authenticode": receipt.get("windows_authenticode")}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
