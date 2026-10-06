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
import io
import errno
import socket
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time
from types import SimpleNamespace

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
        # LC-16: a host's probe is not a client an update would interrupt; a page or an
        # agent asking anything else is.
        # Read from the receipt, which the update job reads; the published /health keeps
        # its shape (fabric-service/0.1) and does not carry `clients`.
        receipt = lambda: json.loads((work / "scratch/serverd.json").read_text(encoding="utf-8"))  # noqa: E731
        check("/health keeps its published shape", "clients" not in health, str(sorted(health)))
        check("a probe alone is no client", (receipt().get("clients") or {}).get("recent") is False,
              str(receipt().get("clients")))
        code, remote = get("/remote")
        code2, health = get("/health")
        clients = receipt().get("clients") or {}
        check("a request other than a probe makes the server's clients recent",
              clients.get("recent") is True and clients.get("window_s") == 300, str(clients))
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
        # The frontmatter quotes the scalar (`version: "0.13.1"`); served as
        # read, every version arrived as "\"0.13.1\"" in /health and /skills.
        versions = list((skills.get("shipped") or {}).values())
        check("and each version is the bare number, without the YAML quotes",
              versions and all(v == "unversioned" or v[:1].isdigit() for v in versions), str(versions))
        code, agents = get("/agents")
        check("/agents renders the Agents section now, from the store",
              code == 200 and isinstance(agents, bytes) and b'id="agents"' in agents
              and b"data-live" in agents, str(agents)[:160])
        code, agents_ru = get("/agents?locale=ru")
        check("and in the reader's language",
              code == 200 and "Нужно вам".encode() in agents_ru, str(agents_ru)[:160])
        code, agents_bad = get("/agents?locale=xx")
        check("an unknown language falls back to English, not an error",
              code == 200 and b"Needs you" in agents_bad, str(agents_bad)[:120])
        code, page = get("/")
        check("/ falls back to the single page when the split is not built",
              code == 200 and b"fixture page" in page,
              "the server must serve paths.DASHBOARD_HTML, not a hardcoded path")
        code, err = get("/nope")
        check("an unknown route is 404 with the route list", code == 404
              and "/health" in json.dumps(err), str(err)[:120])
        # HEAD was 501 (`curl -I` on any page): a link checker or an uptime
        # probe saw the server as broken. Same status and headers as GET, no body.
        for path, want in (("/", 200), ("/health", 200), ("/nope", 404)):
            c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
            c.request("HEAD", path)
            r = c.getresponse()
            body = r.read()
            c.close()
            check(f"HEAD {path} answers {want} like GET, with no body",
                  r.status == want and body == b"" and r.getheader("Content-Length") not in (None, "0"),
                  f"{r.status} {r.getheader('Content-Length')} {body[:60]!r}")
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
        c.request("HEAD", "/")
        length = c.getresponse().getheader("Content-Length")
        c.close()
        check("and its Content-Length is the GET body's", length == str(len(page)), f"{length} vs {len(page)}")
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


def test_disconnected_clients_are_not_server_failures() -> None:
    """Drive real HTTP parsing with failures at the socket's response boundary.

    Both headers and bodies use sendall. Inject the peer failure there rather
    than raising from a route; unrelated application errors must still escape.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import serverd

    class Peer:
        def __init__(self, error, fail_at):
            self.error, self.fail_at, self.writes = error, fail_at, 0

        def makefile(self, *args):
            return io.BytesIO(b"GET /nope HTTP/1.0\r\nHost: localhost:43210\r\n\r\n")

        def sendall(self, data):
            self.writes += 1
            if self.writes == self.fail_at:
                raise self.error

    server = SimpleNamespace(server_address=("127.0.0.1", 43210))
    for error in (BrokenPipeError(errno.EPIPE, "peer closed"),
                  ConnectionResetError(errno.ECONNRESET, "peer reset")):
        for fail_at in (1, 2):
            peer = Peer(error, fail_at)
            escaped = None
            try:
                serverd.Handler(peer, ("127.0.0.1", 1), server)
            except Exception as exc:
                escaped = exc
            check(f"{type(error).__name__} during response write {fail_at} closes quietly",
                  escaped is None and peer.writes == fail_at, str(escaped))

    peer = Peer(OSError(errno.EIO, "synthetic storage failure"), 1)
    escaped = None
    try:
        serverd.Handler(peer, ("127.0.0.1", 1), server)
    except OSError as exc:
        escaped = exc
    check("an unrelated I/O failure is not hidden", escaped is peer.error)


def test_a_server_started_with_sigterm_blocked_still_stops() -> None:
    """A parent's blocked signal mask is inherited across exec: the Mac app's bridge
    passed one, and a server it started ignored SIGTERM — `full open --stop` reported
    "still answers" and launchd could not stop it either. The daemon unblocks the
    stop signals itself, whoever started it."""
    import signal as sig
    work = pathlib.Path(tmpdir.mkdtemp(prefix="serverd-mask-"))
    (work / "pages").mkdir()
    port = free_port()
    env = {**os.environ, "OBSERVATORY_SCRATCH": str(work / "scratch"),
           "OBSERVATORY_DASHBOARD_DIR": str(work / "pages"),
           "OBSERVATORY_DASHBOARD": str(work / "page.html")}
    (work / "page.html").write_text("<html>fixture page</html>", encoding="utf-8")
    p = subprocess.Popen([PY, "tools/serverd.py", "--run", "--port", str(port)], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         preexec_fn=lambda: sig.pthread_sigmask(sig.SIG_BLOCK, {sig.SIGTERM, sig.SIGINT}))
    try:
        for _ in range(75):
            try:
                if get("/health", port)[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        p.send_signal(sig.SIGTERM)
        try:
            code = p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            code = None
        check("a server started with SIGTERM blocked stops on SIGTERM", code == 0, f"exit {code}")
    finally:
        if p.poll() is None:
            p.kill(); p.wait()


def test_the_daemons_stderr_lines_carry_a_utc_time() -> None:
    """LC-12: serverd.err lines had no time, so a fault could not be dated."""
    spec = importlib.util.spec_from_file_location("serverd_stamp", ROOT / "tools/serverd.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sink = io.StringIO()
    out = mod.StampedLines(sink, clock=lambda: "2026-10-07T10:00:00Z")
    out.write("heartbeat: OSError: disk full\nTraceback (most recent call last):\n")
    out.write("  File \"x\", line 1")
    out.write("\n\n")
    lines = sink.getvalue().split("\n")
    check("every non-empty line starts with the UTC time",
          lines[0] == "2026-10-07T10:00:00Z heartbeat: OSError: disk full"
          and lines[1].startswith("2026-10-07T10:00:00Z Traceback")
          and lines[2] == '2026-10-07T10:00:00Z   File "x", line 1', repr(lines))
    check("a line written in pieces is stamped once, and a blank line not at all",
          sink.getvalue().count("2026-10-07T10:00:00Z") == 3 and lines[3] == "", repr(lines))


if __name__ == "__main__":
    print("the always-on server — every route, every state, both board rules\n")
    for fn in (test_every_route_answers_and_none_serves_a_value,
               test_the_health_row_speaks_three_states,
               test_the_board_rule_fires_on_silence_and_only_with_the_plist,
               test_the_stale_session_rule_reads_the_handshake,
               test_the_daemon_never_binds_beyond_localhost,
               test_disconnected_clients_are_not_server_failures,
               test_a_server_started_with_sigterm_blocked_still_stops,
               test_the_daemons_stderr_lines_carry_a_utc_time):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe server answers, and its silence has a finding\033[0m")
