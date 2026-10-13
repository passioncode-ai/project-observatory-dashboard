#!/usr/bin/env python3
"""Every pinned URL in `fabric-agent.json` names a tag its release has, or a commit.

A host admits a capability by reading the schemas the manifest pins at
`https://raw.githubusercontent.com/<owner>/<repo>/<ref>/...`. A `<ref>` newer than the
release that ships the manifest answers 404 until some later release creates the tag:
0.24.0 pinned `grant.check` at `v0.24.1` (ENG-018). So:

- offline (the default, run by the engine suite): every `<ref>` is a `vX.Y.Z` tag no
  newer than `--release` (default: this engine's version), or a commit sha (7-40 hex).
  A branch or any other moving ref is refused. A hex ref is admitted by its SHAPE here:
  offline there is no repository to ask, so a branch named like a sha would pass this
  half — the resolve half is what proves the commit exists, because raw.githubusercontent
  answers 404 for a ref the repository does not have;
- `--resolve` (the release workflow, after the tag is pushed): every distinct URL is also
  read anonymously and must answer 200, with two retries (1 s, 3 s) on a reset, a 429 or
  a 5xx before it is reported unreachable.

`tools/publish_contract.py --check` compares the published bytes with the bundled ones;
this check is the narrower one a release can run before anything is published.
Exit 0 clean, 1 with problems on stderr (one per line), 2 on unusable input.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import sys
import time
from typing import Callable, Iterable
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "fabric-agent.json"
URL = re.compile(r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$")
TAG = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
SHA = re.compile(r"[0-9a-f]{7,40}")


def version(value: str) -> tuple[int, int, int]:
    m = TAG.fullmatch(value.strip())
    if not m:
        raise ValueError(f"not a release version: {value!r}")
    return tuple(int(x) for x in m.groups())                         # type: ignore[return-value]


def urls(doc) -> list[str]:
    """Every distinct raw.githubusercontent URL anywhere in the document, in first-seen order."""
    out: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and node.startswith("https://raw.githubusercontent.com/"):
            if node not in out:
                out.append(node)

    walk(doc)
    return out


def check(doc, release: str) -> list[str]:
    """One problem per URL whose ref is ahead of `release`, moving, or unreadable."""
    ceiling = version(release)
    problems = []
    for url in urls(doc):
        m = URL.match(url)
        if not m:
            problems.append(f"not an owner/repo/ref URL: {url}")
            continue
        ref = m.group(3)
        if SHA.fullmatch(ref):
            continue
        if not ref.startswith("v") or not TAG.fullmatch(ref):
            problems.append(f"ref {ref!r} is neither a vX.Y.Z tag nor a commit sha (a moving ref): {url}")
            continue
        if version(ref) > ceiling:
            problems.append(f"pins tag {ref}, newer than the release v{'.'.join(map(str, ceiling))} "
                            f"that ships this manifest; it answers 404 until {ref} exists: {url}")
    return problems


#: Waits before the second and third try. A raw-CDN blip (a reset, a 5xx, a 429) must not
#: block a publish; an answer such as 404 is not a blip and is never retried.
BACKOFF = (1.0, 3.0)


def fetch(url: str, opener=urllib.request.urlopen, sleep=time.sleep) -> int:
    """The URL's HTTP status after at most 1 + len(BACKOFF) tries; 0 means unreachable."""
    status = 0
    for attempt in range(len(BACKOFF) + 1):
        if attempt:
            sleep(BACKOFF[attempt - 1])
        request = urllib.request.Request(url, method="GET", headers={"User-Agent": "observatory-manifest-check"})
        try:
            with opener(request, timeout=20) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        except OSError:
            status = 0
        if status and status != 429 and status < 500:
            return status
    return status


def resolve(doc, reader: Callable[[str], int] = fetch) -> list[str]:
    problems = []
    for url in urls(doc):
        status = reader(url)
        if status != 200:
            problems.append(f"does not resolve (HTTP {status or 'unreachable'}): {url}")
    return problems


def engine_version() -> str:
    sys.path.insert(0, str(ROOT))
    import configuration
    return configuration.VERSION


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--release", help="the release being cut, vX.Y.Z (default: this engine's version)")
    parser.add_argument("--resolve", action="store_true",
                        help="also read every URL once; run after the tag is pushed (needs the network)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        doc = json.loads(args.manifest.read_text(encoding="utf-8"))
        release = args.release or engine_version()
        version(release)
    except (OSError, ValueError) as exc:
        print(f"check_manifest_urls: {exc}", file=sys.stderr)
        return 2
    problems = check(doc, release)
    if args.resolve:
        problems += resolve(doc)
    for problem in problems:
        print(problem, file=sys.stderr)
    print(json.dumps({"manifest": args.manifest.name, "release": release, "urls": len(urls(doc)),
                      "resolved": bool(args.resolve), "problems": len(problems)}))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
