#!/usr/bin/env python3
"""`tools/publish_contract.py` — the bundled wire contract, watched refusing.

The engine no longer publishes the contract itself: schemas ship with the package
release, `--stage`/`--publish` are refused, and the source manifest stays a
portable installation template. What remains to guard is the offline half —
`staged()` names exactly the reviewed schema and fixture files, `local_failures()`
catches a manifest whose hash, URIs or connection no longer match what is
bundled, and `--local-manifest` writes an installation copy only beneath the
private workspace. `--check` performs anonymous HTTP reads and is not driven
here; nothing in this suite reaches the network.
"""
from __future__ import annotations
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402

PY = sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def load():
    spec = importlib.util.spec_from_file_location("publish_contract", ROOT / "tools/publish_contract.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_publishable_set_is_stated_once_and_is_the_contract_surface() -> None:
    pc = load()
    dirs = [src for src, _, _ in pc.PUBLISHABLE]
    check("only fabric/ is bundled as the contract", all(s.startswith("fabric/") for s in dirs), str(dirs))
    check("the manifest the check reads exists", pc.MANIFEST.is_file(), str(pc.MANIFEST))
    files = pc.staged()
    check("the staged set is not empty", bool(files))
    check("and every staged name is a schema or a fixture",
          all(k.split("/", 1)[0] in {"schemas", "fixtures"} for k in files), str(sorted(files))[:200])
    check("and every staged file is JSON", all(json.loads(v) is not None for v in files.values()))


def test_the_bundled_contract_is_consistent() -> None:
    pc = load()
    failures = pc.local_failures()
    check("the shipped manifest, hash, URIs and schema ids agree", failures == [], str(failures)[:300])
    uris = pc.manifest_uris()
    check("every manifest URI names a file of one pinned schema release",
          bool(uris) and all(pc.resolve(u) in pc.staged() for u in uris), str(uris)[:200])
    old = [u for u in uris if u.startswith(pc.PREFIX)]
    check("the v0.2.0 identifiers keep their published address",
          len(old) >= 14 and all("/ssheleg/project-observatory-open-source/v0.2.0/" in u for u in old),
          f"{len(old)} of {len(uris)}")


def test_each_bundled_file_belongs_to_exactly_one_release() -> None:
    pc = load()
    files = pc.staged()
    check("the shipped lock places every bundled file in one release", pc.release_failures(files) == [],
          str(pc.release_failures(files))[:300])
    lock = json.loads(json.dumps(pc.LOCK))
    was = pc.LOCK
    try:
        moved = lock["releases"][1]["files"][0]
        lock["releases"][0]["files"].append(moved)
        pc.LOCK = lock
        got = pc.release_failures(files)
        check("a file listed under two releases is refused", any("2 releases" in f for f in got), str(got)[:200])
        lock = json.loads(json.dumps(was))
        lock["releases"][1]["files"].pop()
        pc.LOCK = lock
        got = pc.release_failures(files)
        check("a bundled file no release publishes is refused", any("0 releases" in f for f in got), str(got)[:200])
        lock = json.loads(json.dumps(was))
        lock["releases"][1]["release"] = "next"
        pc.LOCK = lock
        got = pc.release_failures(files)
        check("a release that is not a version tag is refused", any("version tag" in f for f in got), str(got)[:200])
    finally:
        pc.LOCK = was


def _with_manifest(pc, doc: dict) -> list[str]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-publish-"))
    alt = d / "fabric-agent.json"
    alt.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    was = pc.MANIFEST
    pc.MANIFEST = alt
    try:
        return pc.local_failures()
    finally:
        pc.MANIFEST = was


def test_a_drifted_manifest_is_refused() -> None:
    pc = load()
    original = json.loads(pc.MANIFEST.read_text(encoding="utf-8"))
    from fabric_hash import compute

    edited = json.loads(json.dumps(original))
    edited["provider"]["revision"] = int(edited["provider"]["revision"]) + 1
    got = _with_manifest(pc, edited)
    check("an edited manifest whose hash was not recomputed is refused",
          "manifest content hash differs" in got, str(got)[:200])

    edited = json.loads(json.dumps(original).replace(pc.PREFIX, pc.RAW + "/v0.0.0-unreleased/observatory/engine/fabric/"))
    edited["provider"]["contentHash"] = compute(edited)
    got = _with_manifest(pc, edited)
    check("a URI naming a release other than the pinned one is refused",
          any("manifest URI" in f for f in got), str(got)[:200])

    edited = json.loads(json.dumps(original))
    for cap in edited.get("capabilities", []):
        cap.setdefault("profile", {}).setdefault("connection", {})["executableRef"] = "file:///opt/some/python"
    edited["provider"]["contentHash"] = compute(edited)
    got = _with_manifest(pc, edited)
    check("an installation-specific connection in the source manifest is refused",
          bool(edited.get("capabilities")) and
          any("portable installation template" in f for f in got), str(got)[:200])

    empty = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-publish-empty-"))
    was = pc.ROOT
    pc.ROOT = empty
    try:
        got = pc.local_failures()
    finally:
        pc.ROOT = was
    check("a publication set that matches no file is refused rather than passed empty",
          "schema/fixture publication set is empty" in got, str(got)[:200])


def test_publishing_is_refused_and_the_local_copy_stays_private() -> None:
    pc = load()
    for flag in ("--stage", "--publish"):
        code = None
        try:
            pc.main([flag])
        except SystemExit as exc:
            code = exc.code
        check(f"`{flag}` is refused: schemas ship with the package release", code == 2, str(code))

    base = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-publish-home-")).resolve()
    home = base / "home"
    env = {**os.environ, "OBSERVATORY_HOME": str(home), "HOME": str(base)}
    p = subprocess.run([PY, "observatory.py", "init"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    check("a synthetic workspace initializes", p.returncode == 0, (p.stdout + p.stderr)[-300:])
    before = pc.MANIFEST.read_bytes()

    def local(dest: pathlib.Path) -> subprocess.CompletedProcess:
        return subprocess.run([PY, "tools/publish_contract.py", "--local-manifest", str(dest)],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)

    outside = base / "outside.json"
    p = local(outside)
    check("a local manifest outside the workspace is refused",
          p.returncode == 2 and not outside.exists(), (p.stdout + p.stderr)[-200:])
    inside = home / "fabric-local.json"
    p = local(inside)
    check("a local manifest inside the workspace is written", p.returncode == 0 and inside.is_file(),
          (p.stdout + p.stderr)[-200:])
    if inside.is_file():
        doc = json.loads(inside.read_text(encoding="utf-8"))
        refs = {c["profile"]["connection"]["executableRef"] for c in doc.get("capabilities", [])}
        check("and it names this installation's interpreter, not the template",
              bool(refs) and all(r.startswith("file://") for r in refs), str(refs)[:160])
        check("and says not to publish it", "do not publish" in p.stdout, p.stdout[-160:])
    p = local(inside)
    check("an existing destination is never overwritten", p.returncode == 2, (p.stdout + p.stderr)[-200:])
    check("the source manifest is byte for byte unchanged", pc.MANIFEST.read_bytes() == before)


if __name__ == "__main__":
    print("the wire contract — bundled, consistent, and never published from here\n")
    for fn in (test_the_publishable_set_is_stated_once_and_is_the_contract_surface,
               test_the_bundled_contract_is_consistent,
               test_each_bundled_file_belongs_to_exactly_one_release,
               test_a_drifted_manifest_is_refused,
               test_publishing_is_refused_and_the_local_copy_stays_private):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe contract that ships is the contract that was reviewed\033[0m")
