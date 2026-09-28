#!/usr/bin/env python3
"""The observatory's always-on local server: live like the memory worker is live.

    tools/serverd.py --run            # foreground (launchd calls this)
    tools/serverd.py --install       # launchd plist: RunAtLoad + KeepAlive
    tools/serverd.py --uninstall     # off, and STAYS off until --install
    tools/serverd.py --status        # is it up, and what it knows
    tools/serverd.py --once          # one refresh cycle, receipt, exit (tests)

WHAT IT IS. The tick is a pulse: every thirty minutes it measures and stops. A
person or an agent between pulses talks to files. This daemon is the third shape
— a process that is UP by default, dies only when told to, and answers now:

    http://127.0.0.1:47311/          the dashboard page, always the newest build
    http://127.0.0.1:47311/health    pid, uptime, versions, tick lease, last tick
    http://127.0.0.1:47311/remote    every project's remote-sync state, summarised
    http://127.0.0.1:47311/leaks     the vault's leak register status (names only)
    http://127.0.0.1:47311/skills    shipped skill versions vs what sessions report

ALWAYS LIVE, THE SAME WAY THE MEMORY WORKER IS. `--install` writes a launchd
agent with `RunAtLoad` and `KeepAlive`, so the daemon starts at login and is
restarted if it dies. `--uninstall` boots it out AND removes the plist — off is
a state, not a pause. There is no in-between: a server that is sometimes up
teaches its readers to check files anyway.

WHAT IT WATCHES, AT THIS LEVEL. The remote axis: for all ~160 projects, which
checkouts are `ahead`, hold a `local-only-branch`, are `unpushed-and-remote-moved`
or have `diverged` — work that exists on one disk only. The daemon does NOT run
`git fetch` itself: the tick's collectors measure and write the registry, and
this process notices the write (mtime) within seconds and re-summarises. That
split is deliberate — one measurer, one live reader — and it is the extension
point: a future watcher adds a `refresh_*` function and a route, never a second
scanner. It also does not spend: no model, no network beyond localhost.

THE HEARTBEAT IS A RECEIPT. Every cycle writes `store/raw/serverd.json` (through
the atomic writer): pid, port, uptime, the remote summary, open-leak count,
skill versions. `tools/build_findings.py` reads it and raises `server.silent`
when the plist says the daemon should be up but the heartbeat is stale — the
board is where silence becomes visible, same as every other component here.

SECURITY. Binds 127.0.0.1 only. Serves NO secret values anywhere: `/leaks` is
names, places and dates from the register, which never held values to begin
with. GET only; anything else is 405.
"""
from __future__ import annotations
import argparse
import datetime
import http.server
import json
import os
import pathlib
import plistlib
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic                                                                   
import leak_register  # noqa: E402
import paths                                                                    

PORT = int(os.environ.get("OBSERVATORY_SERVER_PORT", "47311"))
RECEIPT = paths.SCRATCH / "serverd.json"
import configuration
try:
    from tools import install_launchd
except ImportError:
    import install_launchd
LABEL = install_launchd.instance_label("server")
PLIST = install_launchd.plist_path(LABEL)
#: Sync states that mean work exists on this disk only. Mirrors the board's
#: `AT_RISK`; spelled here because the daemon must not import the findings
#: builder (it reads receipts the builder writes — a cycle).
AT_RISK = ("ahead", "local-only-branch", "unpushed-and-remote-moved", "diverged")
REFRESH_SECONDS = 20
STARTED = time.time()
# The application version, stated once in configuration.py. A literal here said
# 0.1.0 in /health and in the Server header through every release up to 0.3.3.
VERSION = configuration.VERSION


def now_z() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(p: pathlib.Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def refresh_remote() -> dict:
    """The remote axis, summarised from what the collectors last measured.

    Absent is not zero: when the registry cannot be read the summary SAYS so
    instead of reporting an estate with no risk.
    """
    doc = _read_json(paths.REGISTRY / "repositories.json")
    if not doc:
        return {"readable": False, "measured_from": None, "states": {},
                "at_risk": [], "note": "registry unreadable — no claim about remotes"}
    states: dict[str, int] = {}
    risky: list[dict] = []
    for r in doc.get("repositories", []):
        lo = r.get("local") or {}
        checkouts = [lo] + list(lo.get("extra_checkouts") or [])
        for c in checkouts:
            s = c.get("sync")
            if not s:
                continue
            states[s] = states.get(s, 0) + 1
            if s in AT_RISK:
                risky.append({"repo": r.get("name_with_owner"),
                              "folder": c.get("folder") or lo.get("path"),
                              "branch": c.get("branch"), "sync": s})
    try:
        mtime = (paths.REGISTRY / "repositories.json").stat().st_mtime
        measured = datetime.datetime.fromtimestamp(
            mtime, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        measured = None
    return {"readable": True, "measured_from": measured, "states": states,
            "at_risk_total": len(risky),
            "at_risk": sorted(risky, key=lambda x: (x["sync"], x["repo"] or ""))[:100]}


def refresh_leaks() -> dict:
    reg = pathlib.Path(os.environ.get(
        "OBSERVATORY_VAULT_DIR",
        paths.source_path("secret_store", paths.SECRETS) / "projects")) / "leaks.jsonl"
    if not reg.is_file():
        return {"register": False, "open": 0, "total": 0}
    rows, settled = [], set()
    try:
        for line in reg.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("event") == "settled":
                if leak_register.settles(r):
                    settled.add(r.get("of"))
            elif r.get("event") == "leaked":
                rows.append(r)
    except (OSError, ValueError):
        return {"register": True, "readable": False}
    open_rows = [r for r in rows if r.get("id") not in settled]
    return {"register": True, "readable": True, "total": len(rows),
            "open": len(open_rows),
            # The PLACE travels with the name: "which secret" without "seen
            # where" cannot be judged, and the register never held values, so
            # there is nothing here to withhold.
            "open_secrets": [{"secret": r.get("secret"), "where": r.get("where"),
                              "at": r.get("at")} for r in open_rows]}


def refresh_skills() -> dict:
    """Shipped skill versions beside what sessions have reported using."""
    shipped: dict[str, str] = {}
    for sk in (ROOT / "skill/plugins/observatory-log/skills").glob("*/SKILL.md"):
        body = sk.read_text(encoding="utf-8")
        v = None
        for line in body.splitlines():
            if line.strip().startswith("version:"):
                v = line.split(":", 1)[1].strip()
                break
        shipped[sk.parent.name] = v or "unversioned"
    sessions = _read_json(paths.SCRATCH / "skill-sessions.json") or {}
    return {"shipped": shipped, "sessions": sessions.get("sessions", {})}


def _tick_health() -> dict:
    try:
        import configuration
        import tick_health
        return tick_health.health(paths.STATE, paths.SCRATCH,
                                  scheduler_enabled=configuration.enabled("scheduler", "features"))
    except Exception as exc:  # the heartbeat must not fail because this check did
        return {"verdict": "unknown", "why": f"{type(exc).__name__}: {exc}"}


def heartbeat() -> dict:
    lease = _read_json(paths.SCRATCH / "tick-lease.json") or {}
    tick = _read_json(paths.SCRATCH / "tick.json") or {}
    doc = {
        "at": now_z(), "pid": os.getpid(), "port": PORT, "version": VERSION,
        "workspace": str(paths.HOME),
        "uptime_s": int(time.time() - STARTED),
        "remote": refresh_remote(),
        "leaks": refresh_leaks(),
        "skills": refresh_skills(),
        "tick": {"last_finished": tick.get("finished_at"),
                 "lease_holder": lease.get("holder"),
                 # Alive or not, judged from outside the tick (tick_health, PB-132).
                 "health": _tick_health()},
    }
    atomic.write_json(RECEIPT, doc)
    return doc


def local_request(host: str, origin: str | None, fetch_site: str | None, port: int) -> bool:
    """Loopback binding alone does not prevent a rebinding origin reading state."""
    if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
        return False
    if fetch_site == "cross-site":
        return False
    if origin:
        try:
            parsed = urlsplit(origin)
            if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
                    or parsed.port != port or parsed.username is not None
                    or parsed.password is not None or parsed.path not in {"", "/"}
                    or parsed.query or parsed.fragment):
                return False
        except ValueError:
            return False
    return True


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"observatory-serverd/{VERSION}"

    def log_message(self, fmt, *args):                    # quiet by design;
        pass                                              # launchd keeps stderr

    def _json(self, doc, code=200):
        body = json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def do_GET(self):                                     # noqa: N802
        if not local_request(self.headers.get("Host", ""), self.headers.get("Origin"),
                             self.headers.get("Sec-Fetch-Site"), self.server.server_address[1]):
            self._json({"error": "only local browser origins are accepted"}, 403)
            return
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        # THE SPLIT PAGES: `/dashboard/<name>.html` from the built
        # directory, names from the shell's whitelist only — a path with `..`
        # or a name the shell does not know is 404, never a file read.
        if route.startswith("/dashboard/"):
            name = route[len("/dashboard/"):]
            sys.path.insert(0, str(ROOT / "dashboard"))
            import shell
            kind = {shell.ASSET_CSS: "text/css", shell.ASSET_JS: "text/javascript"}.get(name)
            if kind or (name.endswith(".html") and name[:-5] in shell.NAMES):
                page = paths.DASHBOARD_DIR / name
                if page.is_file():
                    body = page.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", f"{kind or 'text/html'}; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._json({"error": "the pages are not built yet",
                            "build_with": "./observatory.py dashboard"}, 404)
                return
            self._json({"error": "no such page", "pages": list(shell.NAMES)}, 404)
            return
        if route in ("/", "/dashboard"):
            # The split pages load app.css/app.js as relative siblings, so the
            # index must be addressed under /dashboard/; served at / its assets
            # would resolve to /app.css and 404, leaving an unstyled, dead page.
            if (paths.DASHBOARD_DIR / "index.html").is_file():
                self.send_response(302)
                self.send_header("Location", "/dashboard/index.html")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            page = paths.DASHBOARD_HTML
            if not page.is_file():
                self._json({"error": "the dashboard is not built yet",
                            "build_with": "./observatory.py dashboard"}, 404)
                return
            body = page.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif route == "/health":
            self._json(heartbeat())
        elif route == "/remote":
            self._json(refresh_remote())
        elif route == "/leaks":
            self._json(refresh_leaks())
        elif route == "/skills":
            self._json(refresh_skills())
        else:
            self._json({"error": "no such route",
                        "routes": ["/", "/dashboard/<page>.html", "/health", "/remote",
                                   "/leaks", "/skills"]}, 404)

    def do_POST(self):                                    # noqa: N802
        self._json({"error": "GET only — this server changes nothing"}, 405)


def serve(port: int) -> int:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)

    def beat():
        while True:
            try:
                heartbeat()
            except Exception as exc:                       # noqa: BLE001 — the
                print(f"heartbeat: {type(exc).__name__}: {exc}",  # beat survives
                      file=sys.stderr)                     # a bad cycle
            time.sleep(REFRESH_SECONDS)

    threading.Thread(target=beat, daemon=True).start()
    print(f"observatory serverd {VERSION} on http://127.0.0.1:{port} "
          f"(pid {os.getpid()})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def build_plist() -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable,
                             str(ROOT / "tools/serverd.py"), "--run", "--port", str(PORT)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardErrorPath": str(paths.STATE / "logs/serverd.err"),
        "StandardOutPath": str(paths.STATE / "logs/serverd.out"),
        "EnvironmentVariables": install_launchd.environment(),
        "Umask": 0o077,
        "LowPriorityIO": True,
        "Nice": 5,
    }


def install() -> int:
    if not install_launchd.scheduler_allowed():
        print("Scheduler disabled: enable features.scheduler before installing background jobs", file=sys.stderr)
        return 1
    if sys.platform != "darwin":
        print("launchd is supported on macOS only", file=sys.stderr)
        return 1
    install_launchd.prepare_logs(("serverd.err", "serverd.out"), paths.STATE / "logs")
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(PLIST, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(plistlib.dumps(build_plist()))
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(PLIST)],
                   capture_output=True, timeout=60)                               
    p = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        print(f"launchctl bootstrap failed: {p.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"installed and started: {LABEL} (RunAtLoad + KeepAlive) — "
          f"http://127.0.0.1:{PORT}/")
    print(f"  off is `tools/serverd.py --uninstall`; off STAYS off until --install")
    return 0


def uninstall() -> int:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(PLIST)],
                   capture_output=True, timeout=60)
    existed = PLIST.exists()
    PLIST.unlink(missing_ok=True)
    print("stopped and removed the launchd agent" if existed else
          "nothing was installed")
    return 0


def status() -> int:
    doc = _read_json(RECEIPT)
    if not doc:
        print("no heartbeat receipt — the server has never run here")
        return 1
    age = time.time() - datetime.datetime.strptime(
        doc["at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc).timestamp()
    alive = age < REFRESH_SECONDS * 3
    print(f"{'UP' if alive else 'SILENT'} — last heartbeat {int(age)}s ago, "
          f"pid {doc.get('pid')}, port {doc.get('port')}, "
          f"uptime {doc.get('uptime_s')}s")
    r = doc.get("remote") or {}
    print(f"  remote watch: {r.get('at_risk_total', '?')} checkout(s) at risk "
          f"across states {r.get('states')}")
    l = doc.get("leaks") or {}
    print(f"  leaks: {l.get('open', '?')} open of {l.get('total', '?')} recorded")
    print(f"  installed: {'yes' if PLIST.exists() else 'no (not always-on)'}")
    return 0 if alive else 1


def main(argv: list[str]) -> int:  # noqa: PLW0603 — PORT set via globals()
    ap = argparse.ArgumentParser(description=(__doc__ or "Serve the private Project Observatory dashboard.").splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run", action="store_true")
    g.add_argument("--once", action="store_true")
    g.add_argument("--install", action="store_true")
    g.add_argument("--uninstall", action="store_true")
    g.add_argument("--status", action="store_true")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args(argv[1:])
    globals()["PORT"] = a.port
    if a.once:
        doc = heartbeat()
        print(json.dumps({k: doc[k] for k in ("at", "pid", "port")},
                         ensure_ascii=False))
        return 0
    if a.run:
        return serve(a.port)
    if a.install:
        return install()
    if a.uninstall:
        return uninstall()
    return status()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
