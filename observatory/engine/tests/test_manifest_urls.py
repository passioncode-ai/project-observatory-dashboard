#!/usr/bin/env python3
"""`tools/check_manifest_urls.py` — a manifest never pins a tag its release does not have.

0.24.0 shipped `fabric-agent.json` with the `grant.check` schemas at `/v0.24.1/`: a tag
that did not exist when 0.24.0 was cut, so a host admitting the 0.24.0 manifest read a
404 until the next release happened to create it (ENG-018). The rule this suite holds:
every `raw.githubusercontent.com/<owner>/<repo>/<ref>/` URL names a version tag no newer
than the release being cut, or a commit sha. Moving refs (`main`, a branch) are refused.

Offline: the resolve half (`--resolve`, after the tag push in the release workflow) is
driven through an injected fetcher, never the network.
"""
from __future__ import annotations
import importlib.util
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402

PY = sys.executable
FAILURES: list[str] = []
RAW = "https://raw.githubusercontent.com/passioncode-ai/project-observatory-dashboard"

#: The `grant.check` capability as `v0.24.0:observatory/engine/fabric-agent.json:368-369,392`
#: published it — verbatim URLs, the rest of the manifest trimmed.
V0240_GRANT_CHECK = {
    "capabilities": [{
        "name": "grant.check",
        "inputSchema": f"{RAW}/v0.24.1/observatory/engine/fabric/schemas/grant-check-input.schema.json",
        "outputSchema": f"{RAW}/v0.24.1/observatory/engine/fabric/schemas/grant-check-output.schema.json",
        "profile": {"probes": [{
            "id": "grant-check-unproven-is-denied",
            "inputFixture": f"{RAW}/v0.24.1/observatory/engine/fabric/fixtures/grant-check-input.json",
            "outputSchema": f"{RAW}/v0.24.1/observatory/engine/fabric/schemas/grant-check-output.schema.json",
        }]},
    }, {
        "name": "project.resolve",
        "inputSchema": f"{RAW}/v0.24.0/observatory/engine/fabric/schemas/project-resolve-input.schema.json",
    }],
}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def load():
    spec = importlib.util.spec_from_file_location("check_manifest_urls", ROOT / "tools/check_manifest_urls.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def write(doc: dict) -> pathlib.Path:
    path = pathlib.Path(tmpdir.mkdtemp()) / "fabric-agent.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(ROOT / "tools/check_manifest_urls.py"), *args],
                          capture_output=True, text=True, timeout=60)


def test_the_0_24_0_manifest_is_refused_for_the_0_24_0_release() -> None:
    m = load()
    problems = m.check(V0240_GRANT_CHECK, "v0.24.0")
    check("the 0.24.0 manifest fails against release v0.24.0", bool(problems), str(problems))
    check("and the refusal names the future tag v0.24.1",
          any("v0.24.1" in p for p in problems), str(problems))
    check("one problem per distinct URL, the v0.24.0 pin is not among them",
          len(problems) == 3 and not any("/v0.24.0/" in p for p in problems), str(problems))
    p = run("--manifest", str(write(V0240_GRANT_CHECK)), "--release", "v0.24.0")
    check("the command exits 1 and says why", p.returncode == 1 and "v0.24.1" in p.stderr,
          f"rc={p.returncode} {p.stderr[-300:]}")


def test_the_same_manifest_passes_once_the_tag_exists() -> None:
    m = load()
    check("against v0.24.1 every pin is at or below the release", m.check(V0240_GRANT_CHECK, "v0.24.1") == [])
    check("against v0.25.0 too", m.check(V0240_GRANT_CHECK, "0.25.0") == [])


def test_moving_refs_are_refused_and_shas_admitted() -> None:
    m = load()
    sha = "982f7841" + "0" * 32
    doc = {"a": f"{RAW}/main/x.json", "b": f"{RAW}/{sha}/y.json", "c": f"{RAW}/982f784/z.json",
           "d": f"{RAW}/v1.2/w.json"}
    problems = m.check(doc, "v0.24.4")
    check("a branch name is refused", any("'main'" in p for p in problems), str(problems))
    check("a malformed tag is refused", any("'v1.2'" in p for p in problems), str(problems))
    check("full and short commit shas are admitted", len(problems) == 2, str(problems))


def test_the_shipped_manifest_passes_against_the_engine_version() -> None:
    m = load()
    import configuration
    problems = m.check(json.loads((ROOT / "fabric-agent.json").read_text()), "v" + configuration.VERSION)
    check("the bundled manifest pins nothing newer than this version", problems == [], str(problems))
    p = run()
    check("the default run (bundled manifest, engine version) exits 0", p.returncode == 0,
          f"rc={p.returncode} {p.stderr[-300:]}")


def test_resolve_reads_every_url_once_and_names_what_did_not_answer() -> None:
    m = load()
    seen: list[str] = []

    def fetch(url: str) -> int:
        seen.append(url)
        return 404 if "grant-check-input.json" in url else 200

    problems = m.resolve(V0240_GRANT_CHECK, fetch)
    check("each distinct URL is fetched once", len(seen) == 4 and len(set(seen)) == 4, str(seen))
    check("the one that answered 404 is named with its status",
          len(problems) == 1 and "404" in problems[0] and "grant-check-input.json" in problems[0], str(problems))


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_a_cdn_blip_is_retried_before_the_url_is_called_unreachable() -> None:
    """One dropped connection or 5xx from the raw CDN must not block a publish."""
    import urllib.error
    m = load()
    calls: list[str] = []
    waits: list[float] = []

    def opener(script):
        def open_(request, timeout=None):
            calls.append(request.full_url)
            outcome = script.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return _Response(outcome)
        return open_

    url = f"{RAW}/v0.24.1/x.json"
    status = m.fetch(url, opener=opener([OSError("reset"), 503, 200]), sleep=waits.append)
    check("two failures then 200 resolves", status == 200 and len(calls) == 3, f"{status} after {len(calls)}")
    check("with a short, growing backoff between tries", waits and len(waits) == 2 and waits[0] < waits[1] <= 5,
          str(waits))
    calls.clear(); waits.clear()
    status = m.fetch(url, opener=opener([OSError("a"), OSError("b"), OSError("c")]), sleep=waits.append)
    check("three failures report unreachable", status == 0 and len(calls) == 3, f"{status} after {len(calls)}")
    calls.clear(); waits.clear()
    err = urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    status = m.fetch(url, opener=opener([err]), sleep=waits.append)
    check("a 404 is an answer, not a blip: no retry", status == 404 and len(calls) == 1 and not waits,
          f"{status} after {len(calls)}")


if __name__ == "__main__":
    print("manifest URLs — no pin ahead of its release\n")
    for fn in (test_the_0_24_0_manifest_is_refused_for_the_0_24_0_release,
               test_the_same_manifest_passes_once_the_tag_exists,
               test_moving_refs_are_refused_and_shas_admitted,
               test_the_shipped_manifest_passes_against_the_engine_version,
               test_resolve_reads_every_url_once_and_names_what_did_not_answer,
               test_a_cdn_blip_is_retried_before_the_url_is_called_unreachable):
        try:
            fn()
        except Exception as exc:                                    # a crash is a failure, named
            check(fn.__name__, False, f"{type(exc).__name__}: {exc}")
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mevery pinned URL names a tag its release has\033[0m")
