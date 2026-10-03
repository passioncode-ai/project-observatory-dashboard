#!/usr/bin/env python3
"""Leave at most the current and the previous release of every build artefact (lifecycle LC-15).

    python tools/prune_builds.py                  # dist/: newest two releases of the wheel and sdist
    python tools/prune_builds.py --keep 1         # only the newest
    python tools/prune_builds.py --clean-caches   # also remove build caches past their cap

WHY. A build directory that only ever grows is how a build machine fills its disk;
the organisation's lifecycle contract makes the build itself prune, not a later
chore. `macos/scripts/build-app.sh` runs this after every app build, and the wheel
build command in AGENTS.md runs it after `pip wheel`.

What counts as a release here: `project_observatory-<version>-*.whl` and
`project_observatory-<version>.tar.gz` in `dist/`. Anything else in `dist/`
(`SHA256SUMS`, notes) is left alone. Caches are not releases: each has a cap
(CACHE_CAPS), and only `--clean-caches` removes one that is past it — they rebuild
on the next build. Runs on the macOS system python too (3.9): no newer syntax.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
RELEASE = re.compile(r"^project_observatory-(?P<version>\d+(?:\.\d+)*)(?:-.+\.whl|\.tar\.gz)$")
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
              "/Support/lsregister")
#: Build caches and their caps in bytes; the clean command brings each back to zero.
CACHE_CAPS: Dict[Path, int] = {
    ROOT / "macos" / ".build": 2 * 1024 ** 3,     # SwiftPM scratch for the app (build-app.sh)
    ROOT / "build": 200 * 1024 ** 2,              # setuptools' intermediate tree from `pip wheel`
}


def _key(version: str):
    return tuple(int(x) for x in version.split("."))


def prune_dist(dist: Path, keep: int = 2) -> List[Path]:
    """Remove every release file whose version is older than the newest `keep`; return them."""
    found: Dict[str, List[Path]] = {}
    for p in Path(dist).iterdir() if Path(dist).is_dir() else []:
        m = RELEASE.match(p.name)
        if m and p.is_file():
            found.setdefault(m.group("version"), []).append(p)
    old = sorted(found, key=_key, reverse=True)[max(keep, 1):]
    removed = [p for v in old for p in sorted(found[v])]
    for p in removed:
        p.unlink()
    return removed


def unregister_app(app: Path) -> None:
    """Forget a bundle in LaunchServices, so no stale copy answers an `open`."""
    if sys.platform == "darwin" and Path(LSREGISTER).exists():
        subprocess.run([LSREGISTER, "-u", str(app)], capture_output=True, timeout=60)


#: Only bundles of this identifier are ever removed: `build-app.sh` takes any output
#: folder, and a folder of other people's apps must come out of a build untouched.
BUNDLE_ID = "ai.passioncode.observatory"


def bundle_id(app: Path) -> Optional[str]:
    import plistlib
    try:
        return plistlib.loads((app / "Contents" / "Info.plist").read_bytes()).get("CFBundleIdentifier")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def prune_apps(out: Path, current: Path,
               unregister: Callable[[Path], None] = unregister_app) -> List[Path]:
    """Remove every other Observatory bundle the build left in `out`, unregistering each first."""
    removed = []
    for p in sorted(Path(out).glob("*.app")) if Path(out).is_dir() else []:
        if p.resolve() == Path(current).resolve() or p.is_symlink() or bundle_id(p) != BUNDLE_ID:
            continue
        unregister(p)
        shutil.rmtree(p)
        removed.append(p)
    return removed


def _size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def caches_over_cap(caps: Optional[Dict[Path, int]] = None) -> List[dict]:
    out = []
    for path, cap in (CACHE_CAPS if caps is None else caps).items():
        if path.is_dir() and not path.is_symlink():
            size = _size(path)
            if size > cap:
                out.append({"path": path, "bytes": size, "cap": cap})
    return out


def clean_caches(over: List[dict]) -> None:
    for row in over:
        shutil.rmtree(row["path"], ignore_errors=True)


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--dist", type=Path, default=ROOT / "dist")
    ap.add_argument("--keep", type=int, default=2)
    ap.add_argument("--app", type=Path, help="the bundle just built; other bundles beside it are removed")
    ap.add_argument("--clean-caches", action="store_true")
    args = ap.parse_args(argv)
    for p in prune_dist(args.dist, args.keep):
        print(f"removed old release file {p.name}")
    if args.app:
        for p in prune_apps(args.app.parent, args.app):
            print(f"removed and unregistered old bundle {p}")
    over = caches_over_cap()
    for row in over:
        verb = "removed" if args.clean_caches else "over its cap"
        print(f"cache {verb}: {row['path']} ({row['bytes'] // 1024 ** 2} MB, cap {row['cap'] // 1024 ** 2} MB)")
    if args.clean_caches:
        clean_caches(over)
    elif over:
        print("  run `python tools/prune_builds.py --clean-caches` to bring them back under the cap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
