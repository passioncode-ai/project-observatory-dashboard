#!/usr/bin/env python3
"""The one tool whose purpose is telling a human, and what it did when it could not.

`tools/notify_findings.py` records delivery in the event store under
`kind='finding.notified'` with `ref='<id>@<severity>'`, and the schema's
`events_dedup UNIQUE(kind, ref)` makes it once-only. That design is right: a
notice every thirty minutes teaches the operator to ignore the channel.

**It recorded a FAILED send the same way, and said so out loud** — "FAILED —
recorded anyway". So one unsuccessful `osascript` marked a critical finding as
notified for ever: the next run finds the ref in `seen` and stays silent. In the
only tool that exists to tell a person something, a failure to tell them was
written down as having told them.

Before the change every recorded notification carried `delivered: true`, so
the path had not bitten where it was first read. It is reachable — a scheduled job with no graphical login session cannot
reach the window server at all — and it is the kind of failure that leaves no other trace.

Nothing here sends a real notification. The delivery function is replaced, which
is also why the failure path can be driven at all.
"""
from __future__ import annotations
import importlib, importlib.util, json, os, pathlib, sqlite3, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
# The board reads a workspace's configuration; the synthetic estate is it.
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
import tmp as tmpdir  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


FINDING = {"id": "domain.hold:domain:example.test", "type": "domain.hold",
           "subject": "domain:example.test", "severity": "critical",
           "title": "example.test is on registrar hold",
           "detail": "a fixture", "action": "none", "evidence": [],
           "first_seen": "2026-09-07"}


def sandbox() -> tuple[pathlib.Path, dict]:
    """A registry with one critical finding, a real store, and a scratch dir."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-notify-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/findings.json").write_text(json.dumps({
        "schema_version": 1, "built_at": "2026-09-07T00:00:00Z",
        "counts": {"critical": 1, "warning": 0, "info": 0},
        "findings": [FINDING]}), encoding="utf-8")
    os.environ["OBSERVATORY_REGISTRY"] = str(d / "registry")
    os.environ["OBSERVATORY_SCRATCH"] = str(d / "scratch")
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    # No Fabric host in the sandbox unless a test installs one: the operator's own
    # descriptor and app must not decide which channel a fixture takes.
    (d / "services").mkdir()
    os.environ["FABRIC_SERVICES_DIR"] = str(d / "services")
    os.environ["FABRIC_DASHBOARDS_APP"] = str(d / "no-such.app")
    import paths
    importlib.reload(paths)
    from store import db as sdb
    importlib.reload(sdb)
    sdb.connect().close()                      # the schema, so events exists
    env = dict(os.environ)
    return d, env


def load_notifier():
    spec = importlib.util.spec_from_file_location("notify_t", ROOT / "tools/notify_findings.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["notify_t"] = mod
    spec.loader.exec_module(mod)
    return mod


def notified_refs(d: pathlib.Path) -> set[str]:
    con = sqlite3.connect(f"file:{d / 'observatory.db'}?mode=ro", uri=True)
    refs = {r[0] for r in con.execute(
        "SELECT ref FROM events WHERE kind = 'finding.notified'")}
    con.close()
    return refs


# ─────────────── a failed send must not count as a send ─────────────────

def test_a_failed_send_is_not_recorded_as_notified() -> None:
    d, _ = sandbox()
    m = load_notifier()
    m.notify = lambda title, body: (False, "osascript exited 1: no Aqua session")
    rc = m.main([])
    check("the run still exits 0 — the tick reports, it does not abort", rc == 0, str(rc))
    check("NOTHING is recorded as notified", not notified_refs(d), str(notified_refs(d)))

    # And the next run must therefore try again.
    calls = {"n": 0}

    def counting(title, body):
        calls["n"] += 1
        return False, "still no Aqua session"

    m.notify = counting
    m.main([])
    check("so a later run tries again rather than staying silent for ever",
          calls["n"] == 1, str(calls["n"]))


def test_a_delivered_send_is_recorded_once() -> None:
    d, _ = sandbox()
    m = load_notifier()
    sends = {"n": 0}

    def ok(title, body):
        sends["n"] += 1
        return True, "osascript accepted it"

    m.notify = ok
    m.main([])
    refs = notified_refs(d)
    check("a delivered notice is recorded", len(refs) == 1, str(refs))
    # AND BY EPISODE. Severity alone made a rise in severity speak and a
    # RECURRENCE silent for ever: the ref stayed in the event store, so a
    # finding that closed and came back was announced once and never again.
    # `tests/test_delivery.py` drives the full cycle; this asserts the shape of
    # the key the guarantee rests on.
    check("keyed by finding, severity AND episode, so both speak again",
          any(r.startswith("domain.hold:domain:example.test@critical#") for r in refs),
          str(refs))
    m.main([])
    check("and a second run does not send it again", sends["n"] == 1, str(sends["n"]))
    check("with no duplicate row", len(notified_refs(d)) == 1, str(notified_refs(d)))


def test_the_event_id_is_stable_across_processes() -> None:
    """`abs(hash(ref))` is randomised per process by PYTHONHASHSEED."""
    src = (ROOT / "tools/notify_findings.py").read_text(encoding="utf-8")
    check("the id is a digest, not a hash()", "hashlib.sha256(ref.encode())" in src)
    # In the CODE, not in the file. The comment explaining the removal names
    # `abs(hash(ref))`, so forbidding the string outright failed on the prose
    # documenting the fix — the fifth assertion in one sitting to break that
    # way, and the reason `tests/source_reader.py` exists.
    sys.path.insert(0, str(ROOT / "tests"))
    import source_reader
    lines = source_reader.code_lines(src, "abs(hash(ref))")
    check("and `abs(hash(ref))` is gone from the code", not lines,
          f"still at line(s) {lines} — the same fact got a different id every run")

    # Driven: two separate interpreters must agree on the id for one ref.
    code = ("import hashlib,sys;"
            "print('ev:notify:' + hashlib.sha256(sys.argv[1].encode()).hexdigest()[:16])")
    ids = {subprocess.run([PY, "-c", code, "x@critical"], capture_output=True,
                          text=True, env=dict(os.environ, PYTHONHASHSEED=str(seed)),
                          timeout=60).stdout.strip() for seed in (0, 1, 2)}
    check("three interpreters with different hash seeds agree", len(ids) == 1, str(ids))


# ─────────────── a dead channel is itself a finding ─────────────────────

def test_a_failing_channel_is_reported() -> None:
    d, env = sandbox()
    m = load_notifier()
    m.notify = lambda title, body: (
        False, "osascript exited 1: Not authorized to send Apple events")
    m.main([])
    report = json.loads((d / "scratch/notify.json").read_text(encoding="utf-8"))
    check("the failure is written as a fact", report["delivered"] is False, str(report))
    check("naming the reason", "Not authorized" in report["detail"], report["detail"])
    check("and what could not be delivered", len(report["undelivered"]) == 1,
          str(report["undelivered"]))

    p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    try:
        rows = [f for f in json.loads(p.stdout)["findings"]
                if f["type"] == "notify.channel_failing"]
    except (ValueError, KeyError):
        check("findings built", False, (p.stdout + p.stderr)[-300:])
        return
    check("a dead channel is raised as a finding", bool(rows), "silence would be the bug")
    if rows:
        f = rows[0]
        check("as critical, because a critical finding went undelivered",
              f["severity"] == "critical", f["severity"])
        check("saying the findings were kept fresh",
              "NOT marked as notified" in f["detail"], f["detail"][:140])
        check("and naming the usual cause",
              "no Aqua session" in f["detail"], f["detail"][:200])
        check("with somewhere else to read them",
              "dashboard" in f["action"], f["action"])


def test_a_successful_run_raises_no_channel_finding() -> None:
    d, env = sandbox()
    m = load_notifier()
    m.notify = lambda title, body: (True, "osascript accepted it")
    m.main([])
    report = json.loads((d / "scratch/notify.json").read_text(encoding="utf-8"))
    check("the report says it was delivered", report["delivered"] is True, str(report))
    check("and lists nothing undelivered", report["undelivered"] == [], str(report))
    p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    try:
        rows = [f for f in json.loads(p.stdout)["findings"]
                if f["type"] == "notify.channel_failing"]
    except (ValueError, KeyError):
        rows = []
    check("no channel finding when the channel works", not rows,
          "a notice that is always there is furniture")


def test_the_reason_reaches_the_caller() -> None:
    """"It failed" is not actionable; the window-server refusal is."""
    m = load_notifier()
    import inspect
    src = inspect.getsource(m.notify)
    check("notify returns a detail beside the verdict",
          "-> tuple[bool, str]" in src, src.splitlines()[0])
    check("naming the exit code and stderr on a refusal",
          "osascript exited" in src and "r.stderr" in src)
    check("and the exception type on a crash", "type(exc).__name__" in src)


def test_info_findings_still_never_notify() -> None:
    """The rule that keeps the channel worth reading, unchanged by this work."""
    d, _ = sandbox()
    doc = json.loads((d / "registry/findings.json").read_text(encoding="utf-8"))
    doc["findings"] = [{**FINDING, "id": "x:info", "severity": "info"}]
    (d / "registry/findings.json").write_text(json.dumps(doc), encoding="utf-8")
    m = load_notifier()
    sends = {"n": 0}

    def counting(title, body):
        sends["n"] += 1
        return True, "ok"

    m.notify = counting
    m.main([])
    check("an info finding is not pushed", sends["n"] == 0, str(sends["n"]))
    m.main(["--include-info"])
    check("unless the caller asks for it", sends["n"] == 1, str(sends["n"]))
    # AND THE SCHEDULE DOES NOT ASK. That is what makes the board the only
    # surface an info row ever reaches — `tools/review.py digest` is about the
    # ledger's review queue, not the findings — which is in turn why
    # `dashboard/build_dashboard.py` gives every finding TYPE a row rather than
    # cutting the list at forty. Once, ten classes sat below that cut, and none
    # of them was pushed either, so they reached nobody.
    tick = (ROOT / "tools/tick.sh").read_text(encoding="utf-8")
    line = [l for l in tick.splitlines() if "notify_findings.py" in l]
    check("the tick sends without --include-info", line and "--include-info" not in line[0],
          str(line))
    page = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    # The cap became a fold: every open row is carried
    # and a type beyond its floor folds under a named control. The invariant
    # this guards — no class silently absent — holds a fortiori, and the reason
    # still travels with the rule in the builder's own words.
    check("so the page carries every open row, folding rather than omitting, and says why",
          '"folded": n >= FINDINGS_ON_PAGE' in page and "NO OMISSION" in page
          and '"omitted": 0' in page,
          "the reason must travel with the rule, or the next cap re-hides a class")


def test_a_fabric_host_is_the_one_channel() -> None:
    """With its descriptor installed and Fabric Dashboards present, the host delivers
    `finding.opened` from the events feed; a banner of our own would be the same finding
    twice, from a sender that is not the host (Fabric Dashboards ADR-0010)."""
    d, _ = sandbox()
    (d / "services/project-observatory.default.json").write_text("{}", encoding="utf-8")
    app = d / "Fabric Dashboards.app"
    app.mkdir()
    os.environ["FABRIC_DASHBOARDS_APP"] = str(app)
    m = load_notifier()
    calls = {"n": 0}

    def counting(title, body):
        calls["n"] += 1
        return True, "osascript accepted it"

    m.notify = counting
    rc = m.main([])
    check("exits 0", rc == 0, str(rc))
    check("no banner of our own", calls["n"] == 0, str(calls["n"]))
    check("nothing recorded as notified by this tool", not notified_refs(d), str(notified_refs(d)))
    report = json.loads((d / "scratch/notify.json").read_text(encoding="utf-8"))
    check("the channel report names the host, so no channel finding is raised",
          report["delivered"] is True and "Fabric Dashboards" in report["detail"], str(report))
    # Without the app the descriptor alone delivers nothing: the banner is ours again.
    os.environ["FABRIC_DASHBOARDS_APP"] = str(d / "gone.app")
    m.main([])
    check("no host app: the tool notifies itself", calls["n"] == 1, str(calls["n"]))


if __name__ == "__main__":
    print("the notifier — and what it did when it could not tell anyone\n")
    for fn in (test_a_failed_send_is_not_recorded_as_notified,
               test_a_delivered_send_is_recorded_once,
               test_the_event_id_is_stable_across_processes,
               test_a_failing_channel_is_reported,
               test_a_successful_run_raises_no_channel_finding,
               test_the_reason_reaches_the_caller,
               test_info_findings_still_never_notify,
               test_a_fabric_host_is_the_one_channel):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma notice that did not arrive is no longer recorded as arrived\033[0m")
