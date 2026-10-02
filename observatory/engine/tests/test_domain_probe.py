#!/usr/bin/env python3
"""The only collector that reaches outside this machine, and what it said when it could not.

`collectors/scan_domains.py` asks the DNS through `dig`, the web through `curl`
and the registries through RDAP. Its `dig()` returned `[]` for two different
things — "this host has no such record" and "dig could not run" — and
`http_status()` returned `0` for both "the site is down" and "curl is missing",
with a docstring that admitted the conflation and called it a feature.

**That is not a small conflation.** An empty A record makes a domain `dark`;
`domain.dark` says it "is being paid for and serves nothing"; `site.dead` is
CRITICAL — "a published claim that is currently false". Running the collector
with `dig` and `curl` off PATH once brought **every host back `resolves: false,
dark: true`, with `degraded` naming only RDAP.** A handful of dark domains would
have become nearly all of them, a few critical findings dozens, and the emitter
would have committed every one to the registry before the notifier pushed it at
the operator.

The fix is three outcomes where there were two — resolves, does not resolve, and
NOT MEASURED — plus a preflight that refuses the whole scan when the tools are
absent, because a missing `dig` is one fact about this machine rather than
one claim per host about the estate. Overwriting a good liveness file with those
claims is worse than not writing: the collectors' own rule is that missing input
reads as "not measured this run", never as "measured and empty".

Nothing here reaches the network. The scan runs over the synthetic estate with
the domains integration enabled only for this suite's workspace; the cases that
would reach a real resolver, web server or RDAP service drive `probe()`,
`dig()` and `http_status()` in-process against tools placed on PATH, and the
findings cases plant their own registry.
"""
from __future__ import annotations
import json, os, pathlib, shutil, stat, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
import tmp as tmpdir  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def enable_domains(on: bool = True) -> None:
    """The scan is an integration and is off in a fresh workspace; this suite's
    own synthetic workspace turns it on."""
    settings = pathlib.Path(os.environ["OBSERVATORY_HOME"]) / "config/settings.json"
    doc = json.loads(settings.read_text(encoding="utf-8"))
    doc.setdefault("integrations", {})["domains"] = on
    settings.write_text(json.dumps(doc), encoding="utf-8")


def scan(path_dirs: list[str], dest: pathlib.Path, only: str | None = None) -> tuple[int, str]:
    argv = [PY, "collectors/scan_domains.py", str(dest)]
    if only:
        argv += ["--only", only]
    p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=900,
                       env=dict(os.environ, PATH=":".join(path_dirs)))
    return p.returncode, p.stdout + p.stderr


def fake_tool(d: pathlib.Path, name: str, script: str) -> None:
    f = d / name
    f.write_text(script, encoding="utf-8")
    f.chmod(f.stat().st_mode | stat.S_IEXEC)


# ─────────── a missing tool is one fact, not fifty-eight claims ─────────

def test_a_disabled_integration_scans_nothing() -> None:
    enable_domains(False)
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-probe0-"))
    dest = d / "live.json"
    code, out = scan(["/usr/bin", "/bin"], dest)
    check("with the integration off the scan exits 0 and says so",
          code == 0 and "not configured" in out, out[-200:])
    check("and probes nothing", not dest.is_file())
    enable_domains(True)


def test_the_scan_refuses_when_its_tools_are_absent() -> None:
    enable_domains(True)
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-probe-"))
    (d / "bin").mkdir()
    dest = d / "live.json"
    code, out = scan([str(d / "bin")], dest)
    check("it exits non-zero", code != 0, f"exit {code}")
    check("naming what is missing", "dig" in out and "curl" in out, out[-200:])
    check("and writes NOTHING", not dest.is_file(),
          "a file of unmeasured rows over a good one is worse than no write")
    check("saying why it did not write", "left untouched" in out, out[-200:])


def test_a_refused_scan_leaves_the_previous_file_alone() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-probe2-"))
    (d / "bin").mkdir()
    dest = d / "live.json"
    dest.write_text(json.dumps({"scanned_on": "2026-09-06", "hosts": {"a.test": {}}}),
                    encoding="utf-8")
    before = dest.read_bytes()
    scan([str(d / "bin")], dest)
    check("the last good liveness survives byte for byte",
          dest.read_bytes() == before, "the refusal must not touch it")


def test_the_tick_records_the_refusal_rather_than_only_logging_it() -> None:
    sys.path.insert(0, str(ROOT / "tests"))
    import tick_reader
    src = tick_reader.tick(ROOT)
    at = tick_reader.first_invocation(src, "scan_domains.py")
    check("the domain scan runs through `step`", at != -1 and
          src.splitlines()[at].strip().startswith("step "),
          src.splitlines()[at].strip()[:80] if at != -1 else "not invoked")
    check("so a refusal reaches tick.json",
          "FAILED_STEPS" in src, "otherwise the only trace is one log line")
    # And the freshness test is on the FILE's mtime, so a refusal — which writes
    # nothing — leaves the next tick to try again half an hour later.
    check("the scan is gated on the file's age, so a refusal retries",
          "-mmin -1440" in src, "a refusal must not become permanent silence")


# ─────────── unmeasured is not dark ────────────────────────────────────

def load_scanner():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sd", ROOT / "collectors/scan_domains.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["sd"] = m
    spec.loader.exec_module(m)
    return m


def test_a_failing_lookup_reads_as_UNMEASURED_not_as_dark() -> None:
    """Driven through `probe()`, the per-host path the scan maps over its hosts.

    The whole scan also asks RDAP over the network after probing, so it is not
    run here; a `dig` that exits non-zero is placed first on PATH, and `curl` is
    a stub that fails the test if it is ever reached — a lookup that could not be
    performed must never go on to fetch the site."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-probe3-"))
    (d / "bin").mkdir()
    fake_tool(d / "bin", "dig", "#!/bin/sh\nexit 9\n")
    fake_tool(d / "bin", "curl", "#!/bin/sh\necho reached > \"$(dirname \"$0\")/curl-was-run\"\necho 200\n")
    m = load_scanner()
    old_path = os.environ["PATH"]
    os.environ["PATH"] = str(d / "bin")
    try:
        hosts = dict(m.probe(h) for h in ("alpha-web.example.com", "beta-api.example.com"))
    finally:
        os.environ["PATH"] = old_path
    unmeasured = {h: v for h, v in hosts.items() if v.get("resolves") is None}
    dark = {h for h, v in hosts.items() if v.get("resolves") is False}
    check("every host is unmeasured", len(unmeasured) == len(hosts),
          f"{len(unmeasured)} of {len(hosts)}")
    check("and NONE is reported dark", not dark, str(sorted(dark)[:4]))
    sample = next(iter(unmeasured.values()))
    check("each carries the reason", "dig exited 9" in sample.get("unmeasured", ""),
          str(sample)[:160])
    check("and no http status is invented", sample.get("http") is None,
          str(sample.get("http")))
    check("and the site was never fetched", not (d / "bin/curl-was-run").exists())


def test_the_probes_report_their_own_failure() -> None:
    """The signature change is the fix: `[]` and `0` cannot carry a reason."""
    m = load_scanner()
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-probe4-"))
    (d / "bin").mkdir()
    old_path = os.environ["PATH"]
    os.environ["PATH"] = str(d / "bin")
    try:
        recs, err = m.dig("example.test", "A")
        status, herr = m.http_status("example.test")
    finally:
        os.environ["PATH"] = old_path
    check("dig returns a reason when it cannot run", recs == [] and err,
          f"{recs} / {err}")
    check("naming the missing tool", "not installed" in (err or ""), str(err))
    check("http_status does too", status == 0 and herr, f"{status} / {herr}")
    check("naming curl", "curl" in (herr or ""), str(herr))


# ─────────── the consumers stopped treating unknown as negative ────────

#: Owned domains and their last probe, planted rather than read: three dark,
#: one that resolves, and a project that publishes one of the dark ones.
DOMAINS = ("alpha-web.example.com", "beta-api.example.com", "gamma-docs.example.com",
           "delta-shop.example.com")
DARK = DOMAINS[:3]


def planted_registry(d: pathlib.Path, unmeasured: bool) -> None:
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/domains.json").write_text(json.dumps({"schema_version": 1, "domains": [
        {"name": n, "ownership": "owned"} for n in DOMAINS]}), encoding="utf-8")
    (d / "registry/projects.json").write_text(json.dumps({"schema_version": 2, "projects": [
        {"id": "project:alpha-web", "name": "Alpha Web",
         "sites": [{"host": DOMAINS[0]}]}]}), encoding="utf-8")
    hosts = []
    for n in DOMAINS:
        h = {"host": n, "checked_on": "2026-01-02"}
        if unmeasured:
            h.update(resolves=None, unmeasured="dig is not installed here")
        else:
            h["resolves"] = n not in DARK
        hosts.append(h)
    (d / "registry/domain-liveness.json").write_text(json.dumps({
        "schema_version": 1, "scanned_on": "2026-01-02", "hosts": hosts, "degraded": []}),
        encoding="utf-8")


def test_findings_do_not_call_an_unmeasured_host_dark() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-probe5-"))
    planted_registry(d, unmeasured=True)
    p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT,
                       env=dict(os.environ, OBSERVATORY_REGISTRY=str(d / "registry"),
                                OBSERVATORY_SCRATCH=str(d / "scratch"),
                                OBSERVATORY_DB=str(d / "absent.db")),
                       capture_output=True, text=True, timeout=600)
    try:
        found = json.loads(p.stdout)["findings"]
    except (ValueError, KeyError):
        check("findings built", False, (p.stdout + p.stderr)[-300:])
        return
    kinds = {f["type"] for f in found}
    check("no host is called dark", "domain.dark" not in kinds, str(sorted(kinds)))
    check("no site is called dead", "site.dead" not in kinds, str(sorted(kinds)))
    unm = [f for f in found if f["type"] == "domain.unmeasured"]
    check("one finding names the whole gap", len(unm) == 1, str(len(unm)))
    if unm:
        check("counting the hosts", "hosts could not be probed" in unm[0]["title"],
              unm[0]["title"])
        check("saying they are NOT dark",
              "no negative answer" in unm[0]["detail"], unm[0]["detail"][:160])
        check("and naming the remedy", "on the PATH" in unm[0]["action"],
              unm[0]["action"])


def test_a_genuinely_dark_host_is_still_reported() -> None:
    """The counterpart: the fix must not silence a real dark domain."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-probe6-"))
    planted_registry(d, unmeasured=False)
    p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT,
                       env=dict(os.environ, OBSERVATORY_REGISTRY=str(d / "registry"),
                                OBSERVATORY_SCRATCH=str(d / "scratch"),
                                OBSERVATORY_DB=str(d / "absent.db")),
                       capture_output=True, text=True, timeout=600)
    try:
        found = json.loads(p.stdout)["findings"]
    except (ValueError, KeyError):
        check("findings built", False, (p.stdout + p.stderr)[-300:])
        return
    dark = [f for f in found if f["type"] == "domain.dark"]
    check("every measured dark domain is still raised",
          sorted(f["subject"] for f in dark) == sorted(f"domain:{n}" for n in DARK),
          str([f["subject"] for f in dark]))
    check("and the site a project publishes on one of them is dead",
          any(f["type"] == "site.dead" and f["subject"] == "project:alpha-web" for f in found),
          str(sorted({f["type"] for f in found})))
    check("and nothing is reported unmeasured when every host was measured",
          not [f for f in found if f["type"] == "domain.unmeasured"],
          "the planted file has measurements")



def test_a_dark_host_carries_the_day_it_was_first_seen_dark() -> None:
    """The number `site.dead` could not say, because nothing recorded it.

    `registry/domain-liveness.json` held `resolves` and `checked_on` — the LAST
    probe — so the estate could say a host was dark and never for how long, which
    is the difference between an incident and a decision to stop publishing.

    THE FIELD IS `dark_first_seen`, not `dark_since`: the system knows when it
    first SAW the host dark, not when the host went dark, and on the day the
    field began every dark host stood on it.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import build_findings as B
    B._DARK_EPOCH.clear()
    hosts = [{"host": "old.example", "dark_first_seen": "2026-01-01"},
             {"host": "new.example", "dark_first_seen": "2026-09-01"},
             {"host": "alive.example"}]
    old = B._dark_for(hosts, "old.example")
    check("the earliest sighting says the estate only started then",
          "began recording it" in old and "far longer" in old, old)
    new = B._dark_for(hosts, "new.example")
    check("a later one is a real measurement",
          "First seen dark on 2026-09-01" in new and "day(s) ago" in new, new)
    check("and does not repeat the caveat", "began recording" not in new, new)
    check("a host that resolves says nothing", B._dark_for(hosts, "alive.example") == "",
          repr(B._dark_for(hosts, "alive.example")))
    B._DARK_EPOCH.clear()


def test_an_unmeasured_probe_neither_starts_nor_clears_the_clock() -> None:
    """The unmeasured edge, on the new field. `resolves: None` means the probe
    could not run — reading it as "serves nothing" is what once turned a handful
    of dark domains into nearly all of them — and it must not start the clock. It must not clear one
    either: a probe that could not look has not seen the host come back."""
    src = (ROOT / "collectors/emit_registry.py").read_text(encoding="utf-8")
    sys.path.insert(0, str(ROOT / "tests"))
    import source_reader
    code = source_reader.code_keeping_strings(src)
    check("the clock starts only on a measured False",
          "_seen is False" in code,
          "`not resolves` would start it on an unmeasured host")
    check("and an unmeasured probe carries the previous sighting forward",
          "_seen is None and was_dark" in code,
          "a probe that could not look has not seen the host return")
    check("the value read for the clock is not the defaulted one",
          'h.get("resolves")\n' in code or "_seen = h.get(\"resolves\")" in code,
          "the row defaults `resolves` to False for the reader, which would start "
          "the clock on a malformed receipt")


if __name__ == "__main__":
    print("the domain probe — three answers where there were two\n")
    for fn in (test_a_disabled_integration_scans_nothing,
               test_the_scan_refuses_when_its_tools_are_absent,
               test_a_refused_scan_leaves_the_previous_file_alone,
               test_the_tick_records_the_refusal_rather_than_only_logging_it,
               test_a_failing_lookup_reads_as_UNMEASURED_not_as_dark,
               test_the_probes_report_their_own_failure,
               test_findings_do_not_call_an_unmeasured_host_dark,
               test_a_genuinely_dark_host_is_still_reported,
               test_a_dark_host_carries_the_day_it_was_first_seen_dark,
               test_an_unmeasured_probe_neither_starts_nor_clears_the_clock):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma question that could not be asked no longer has a negative answer\033[0m")
