#!/usr/bin/env python3
"""The always-on server, driven on an ephemeral port with redirected artefacts.

What is NOT tested here: launchd itself. KeepAlive is a property of the
scheduler, proven by killing the process and watching it come back; a test
cannot own the user's launchd domain without fighting a real daemon over a real
port. What CAN be owned is everything else: every route, the heartbeat receipt,
the honest three states of the health row, and the two board rules that read
the receipt. The server is bound to a port the kernel hands out, so parallel
suites never collide, and the launchd plist the silence rule looks for is
planted inside the sandbox's own HOME.
"""
from __future__ import annotations
import http.client
import socket
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                               # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []

def free_port() -> int:
    """A port nothing holds right now; a fixed number collides with a parallel run."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def get(path: str, port: int = PORT) -> tuple[int, dict | bytes]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    c.close()
    try:
        return r.status, json.loads(body)
    except ValueError:
        return r.status, body


def board(scratch: pathlib.Path) -> list[dict]:
    """Build the findings in a CHILD with the scratch redirected.

    In-process, `paths` may already be imported by an earlier test in this same
    interpreter, and a redirect set after that import binds nothing — an
    accident this repository has recorded more than once. A child imports fresh.
    """
    code = ("import importlib.util, json, pathlib, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            f"spec = importlib.util.spec_from_file_location('bf', {str(ROOT / 'tools/build_findings.py')!r})\n"
            "bf = importlib.util.module_from_spec(spec); spec.loader.exec_module(bf)\n"
            "print(json.dumps(bf.collect()))\n")
    p = subprocess.run([PY, "-c", code], cwd=ROOT,
                       env={**os.environ, "OBSERVATORY_SCRATCH": str(scratch)},
                       capture_output=True, text=True, timeout=900)
    try:
        return json.loads(p.stdout)
    except ValueError:
        check("the findings build ran in the child", False, (p.stdout + p.stderr)[-200:])
        return []


def test_every_route_answers_and_none_serves_a_value() -> None:
    work = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-serverd-"))
    (work / "scratch").mkdir()
    vault = work / "vault"
    (vault).mkdir()
    (vault / "leaks.jsonl").write_text(json.dumps({
        "event": "leaked", "id": "leak:t", "secret": "demo/prod/API_TOKEN",
        "where": "planted", "at": "2026-09-10T00:00:00Z"}) + "\n", encoding="utf-8")
    # THE PAGE DIRECTORY IS REDIRECTED TOO, at an empty one: `/` prefers the
    # split index and falls back to the single page, and a test
    # that redirects only the single page reads the machine's real build —
    # which passes for the wrong reason and would keep passing if the fallback
    # broke. Empty here, so the fallback is what this asserts.
    (work / "pages").mkdir()
    env = {**os.environ, "OBSERVATORY_SCRATCH": str(work / "scratch"),
           "OBSERVATORY_VAULT_DIR": str(vault),
           "OBSERVATORY_DASHBOARD_DIR": str(work / "pages"),
           "OBSERVATORY_DASHBOARD": str(work / "page.html")}
    (work / "page.html").write_text("<html>fixture page</html>", encoding="utf-8")
    p = subprocess.Popen([PY, "tools/serverd.py", "--run", "--port", str(PORT)],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                if get("/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        code, health = get("/health")
        check("/health answers with the heartbeat", code == 200 and health.get("pid") == p.pid,
              str(health)[:120])
        check("and the receipt landed in the redirected scratch",
              (work / "scratch/serverd.json").is_file(),
              "the heartbeat must go through OBSERVATORY_SCRATCH or tests write the live one")
        code, remote = get("/remote")
        check("/remote summarises the sync states", code == 200 and "states" in remote,
              str(remote)[:120])
        check("and counts what is at risk", isinstance(remote.get("at_risk_total"), int))
        code, leaks = get("/leaks")
        check("/leaks reads the redirected register", code == 200 and leaks.get("open") == 1,
              str(leaks)[:120])
        check("and serves NAMES, never values",
              "API_TOKEN" in json.dumps(leaks) and "planted" in json.dumps(leaks)
              and "sk-" not in json.dumps(leaks), str(leaks)[:150])
        code, skills = get("/skills")
        check("/skills reports the shipped versions", code == 200
              and "handling-secrets" in (skills.get("shipped") or {}), str(skills)[:150])
        code, page = get("/")
        check("/ falls back to the single page when the split is not built",
              code == 200 and b"fixture page" in page,
              "the server must serve paths.DASHBOARD_HTML, not a hardcoded path")
        code, err = get("/nope")
        check("an unknown route is 404 with the route list", code == 404
              and "/health" in json.dumps(err), str(err)[:120])
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
        c.request("POST", "/health")
        check("POST is 405 — this server changes nothing", c.getresponse().status == 405)
        c.close()
    finally:
        p.terminate()
        p.wait(timeout=10)


def test_the_health_row_speaks_three_states() -> None:
    """Beating, silent, absent — the page must tell them apart."""
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the page distinguishes 'never ran / off' from 'unreadable'",
          "server_age_s" in src and "-1" in src, "off is a state, not an error")
    # English message ids, translated from the catalog at render time.
    check("and renders all three words", all(w in src for w in
          ('"not running"', '"answered when measured', '"SILENT for')),
          "a silent server must LOOK different")


def test_the_board_rule_fires_on_silence_and_only_with_the_plist() -> None:
    """`server.silent` — installed and quiet is a defect; not installed is a choice."""
    work = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-serverd-b-"))
    (work / "scratch").mkdir()
    (work / "scratch/serverd.json").write_text(json.dumps(
        {"at": "2026-09-01T00:00:00Z", "pid": 1, "port": 1}), encoding="utf-8")
    sys.path.insert(0, str(ROOT / "tools"))
    import install_launchd
    # The sandbox's HOME, never the operator's: the runner points HOME at a
    # fresh directory, so planting the plist here states "installed" for this
    # process tree only.
    plist = (pathlib.Path.home() / "Library/LaunchAgents"
             / f"{install_launchd.instance_label('server')}.plist")
    check("the plist this rule looks for is not installed in the sandbox", not plist.exists(),
          str(plist))
    got = [f for f in board(work / "scratch") if f["type"] == "server.silent"]
    check("with no plist, silence raises nothing — off is a choice",
          not got, str(got)[:120])
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist/>", encoding="utf-8")
    try:
        got = [f for f in board(work / "scratch") if f["type"] == "server.silent"]
        check("a stale heartbeat with the plist present raises server.silent",
              len(got) == 1 and "minutes old" in got[0]["title"], str(got)[:150])
    finally:
        plist.unlink()


def test_the_stale_session_rule_reads_the_handshake() -> None:
    """`skill.stale_session` — driven through a planted handshake receipt."""
    import datetime
    work = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-serverd-s-"))
    (work / "scratch").mkdir()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (work / "scratch/skill-sessions.json").write_text(json.dumps({"sessions": {
        "handling-secrets@0.1.0": {"skill": "handling-secrets", "reported": "0.1.0",
                                   "verdict": "stale", "last_seen": now, "count": 2},
        "handling-secrets@0.0.1": {"skill": "handling-secrets", "reported": "0.0.1",
                                   "verdict": "stale-major",
                                   "last_seen": "2020-01-01T00:00:00Z", "count": 1},
    }}), encoding="utf-8")
    got = [f for f in board(work / "scratch") if f["type"] == "skill.stale_session"]
    check("a recent stale handshake raises skill.stale_session",
          len(got) == 1, str([g["title"] for g in got])[:150])
    check("and an ANCIENT one does not — the session is long over",
          not any("0.0.1" in g["title"] for g in got),
          "a finding must not outlive the session it warns about")
    check("the row explains that sessions snapshot their skills at start",
          bool(got) and "session START" in got[0]["detail"], str(got)[:200])


def test_the_daemon_never_binds_beyond_localhost() -> None:
    src = (ROOT / "tools/serverd.py").read_text(encoding="utf-8")
    check("the only bind address in the file is 127.0.0.1",
          src.count('("127.0.0.1"') >= 1 and '"0.0.0.0"' not in src,
          "a project watcher has no business on the network")


if __name__ == "__main__":
    print("the always-on server — every route, every state, both board rules\n")
    for fn in (test_every_route_answers_and_none_serves_a_value,
               test_the_health_row_speaks_three_states,
               test_the_board_rule_fires_on_silence_and_only_with_the_plist,
               test_the_stale_session_rule_reads_the_handshake,
               test_the_daemon_never_binds_beyond_localhost):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe server answers, and its silence has a finding\033[0m")
