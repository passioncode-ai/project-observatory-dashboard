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
    http://127.0.0.1:47311/.well-known/fabric-service   who answers, which build, is it healthy
    http://127.0.0.1:47311/fabric/v1/events             what happened, as sentences (token)

A FABRIC SERVICE. The last two routes are the `fabric-service/0.1` protocol of
the Fabric Agent Contract, which lets a host such as Fabric Dashboards find,
watch and supervise every local service without knowing any of them in advance:

- **one copy per workspace.** `--run` takes an exclusive lock on the
  workspace's `service.lock` before it does anything else — before the token,
  the heartbeat, the bind. A second copy prints one sentence naming the holder's
  pid and exits 75. Binding a port is not a lock: a second copy on another port
  would otherwise run a second heartbeat over the same receipts;
- **the well-known document** is answered from memory: the heartbeat thread
  leaves a snapshot (`service_health.snapshot`) and the route serves it. It never
  recomputes `/health`, which stays as it was;
- **the events feed** is a view over the event store (`service_events`), and the
  only route here that needs a credential: `Authorization: Bearer <token>`,
  where the token is the 600 file `service.token` in the workspace, created
  after the lock. Everything else stays open to local reads, as before;
- **the descriptor** — the file a host reads to find this server — is written by
  `--install` and removed by `--uninstall`, never by the running process
  (`service_identity.descriptor`).

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

THE HEARTBEAT IS A RECEIPT, WRITTEN ON CHANGE. `store/raw/serverd.json` (through
the atomic writer): pid, port, uptime, the remote summary, open-leak count,
skill versions. `tools/build_findings.py` reads it and raises `server.silent`
when the plist says the daemon should be up but the heartbeat is stale — the
board is where silence becomes visible, same as every other component here.

IDLE MEANS IDLE (lifecycle LC-08). The beat used to re-read ~200 KB of registry
and rewrite the 15 KB receipt every 20 s whatever happened — about 4,300 writes
a day on an estate where nothing moved. Now each input is re-read only when its
file changed (`_memo`), the receipt is written only when its content changed or
KEEPALIVE_SECONDS have passed (that rewrite is what proves the server alive),
and the beat slows from REFRESH_SECONDS to IDLE_SECONDS when no client has asked
anything for CLIENT_WINDOW_SECONDS. The receipt carries `silent_after_s`, the
age past which its readers call it silent, so they never assume the old 20 s.

SECURITY. Binds 127.0.0.1 only. Serves NO secret values anywhere: `/leaks` is
names, places and dates from the register, which never held values to begin
with. GET only; anything else is 405. The events token opens the events feed
and nothing else; it never travels in a URL, an argument or the plist.
"""
from __future__ import annotations
import argparse
import datetime
import http.server
import json
import os
import pathlib
import plistlib
import signal
import socketserver
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic                                                                   
import fabric_service as fs  # noqa: E402 — the vendored fabric-service/0.1 kit
import leak_register  # noqa: E402
import paths                                                                    
import service_events  # noqa: E402
import service_health  # noqa: E402
import service_identity  # noqa: E402

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
#: The beat while a client (a host's probe, the page, an agent) has asked recently.
REFRESH_SECONDS = 20
#: The beat with no client: nothing reads the snapshot sooner than this matters.
IDLE_SECONDS = 120
#: A request within this window keeps the beat at REFRESH_SECONDS.
CLIENT_WINDOW_SECONDS = 300
#: An unchanged receipt is still rewritten this often: its `at` is the liveness proof.
KEEPALIVE_SECONDS = 300
#: Age past which a reader calls the receipt silent: a keepalive plus a slow beat, with room.
SILENT_AFTER_SECONDS = KEEPALIVE_SECONDS + IDLE_SECONDS + 60
#: The server's own launchd logs are rotated at start and at most this often.
LOG_ROTATE_SECONDS = 3600
STARTED = time.time()
# The application version, stated once in configuration.py. A literal here said
# 0.1.0 in /health and in the Server header through every release up to 0.3.3.
VERSION = configuration.VERSION
#: Drain budget on SIGTERM. launchd's `ExitTimeOut` sits above it.
EXIT_TIMEOUT = 30
SURFACES = {"dashboard": {"path": "/", "login": False},
            "events": {"path": "/fabric/v1/events"}}


def now_z() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(p: pathlib.Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


#: name -> (signature of the files it was computed from, the value).
_MEMO: dict[str, tuple[tuple, object]] = {}
_MEMO_LOCK = threading.Lock()


def _signature(files) -> tuple:
    """(path, mtime_ns, size) per file, None for one that is absent: what "changed" means."""
    sig = []
    for f in files:
        try:
            st = os.stat(f)
            sig.append((str(f), st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((str(f), None))
    return tuple(sig)


def _memo(name: str, files, compute):
    """`compute()` again only when one of `files` changed since the last call."""
    sig = _signature(files)
    with _MEMO_LOCK:
        hit = _MEMO.get(name)
        if hit and hit[0] == sig:
            return hit[1]
    value = compute()
    with _MEMO_LOCK:
        _MEMO[name] = (sig, value)
    return value


def refresh_remote() -> dict:
    """The remote axis, summarised from what the collectors last measured.

    Absent is not zero: when the registry cannot be read the summary SAYS so
    instead of reporting an estate with no risk. Re-read only when the file changed.
    """
    reg = paths.REGISTRY / "repositories.json"
    return _memo("remote", [reg], _remote_summary)


def _remote_summary() -> dict:
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


def _leak_register() -> pathlib.Path:
    return pathlib.Path(os.environ.get(
        "OBSERVATORY_VAULT_DIR",
        paths.source_path("secret_store", paths.SECRETS) / "projects")) / "leaks.jsonl"


def refresh_leaks() -> dict:
    """The register's status, re-read only when the register changed."""
    return _memo("leaks", [_leak_register()], _leak_summary)


def _leak_summary() -> dict:
    reg = _leak_register()
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
    """Shipped skill versions beside what sessions have reported using, re-read on change."""
    files = sorted((ROOT / "skill/plugins/observatory-log/skills").glob("*/SKILL.md"))
    return _memo("skills", [*files, paths.SCRATCH / "skill-sessions.json"], _skill_summary)


def _skill_summary() -> dict:
    # ONE READER of the frontmatter version: `skill_check.shipped_version`,
    # which strips the YAML quotes. A second copy here kept them, so /health
    # and /skills served "\"0.13.1\"" beside the handshake's 0.13.1.
    sys.path.insert(0, str(ROOT / "tools"))
    import skill_check
    shipped: dict[str, str] = {}
    for sk in (ROOT / "skill/plugins/observatory-log/skills").glob("*/SKILL.md"):
        shipped[sk.parent.name] = skill_check.shipped_version(sk.parent.name) or "unversioned"
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


#: (content of the last receipt written, time.time() of that write).
_LAST_RECEIPT: list = [None, 0.0]


def heartbeat() -> dict:
    """The receipt, written only when its content changed or the keepalive is due."""
    lease = _read_json(paths.SCRATCH / "tick-lease.json") or {}
    tick = _read_json(paths.SCRATCH / "tick.json") or {}
    doc = {
        "at": now_z(), "pid": os.getpid(), "port": PORT, "version": VERSION,
        "workspace": str(paths.HOME),
        "uptime_s": int(time.time() - STARTED),
        "silent_after_s": SILENT_AFTER_SECONDS,
        "remote": refresh_remote(),
        "leaks": refresh_leaks(),
        "skills": refresh_skills(),
        "tick": {"last_finished": tick.get("finished_at"),
                 "lease_holder": lease.get("holder"),
                 # Alive or not, judged from outside the tick (tick_health, PB-132).
                 "health": _tick_health()},
    }
    content = json.dumps({k: v for k, v in doc.items() if k not in ("at", "uptime_s")},
                         sort_keys=True, ensure_ascii=False, default=str)
    stamp = time.time()
    if content != _LAST_RECEIPT[0] or stamp - _LAST_RECEIPT[1] >= KEEPALIVE_SECONDS:
        atomic.write_json(RECEIPT, doc)
        _LAST_RECEIPT[0], _LAST_RECEIPT[1] = content, stamp
    return doc


#: time.monotonic() of the last HTTP request, None before the first.
LAST_REQUEST: list = [None]
#: Set by a request that arrives while the beat is slow, so the next snapshot is not 2 minutes away.
WAKE = threading.Event()


def beat_interval(now: float, last_request: float | None) -> float:
    """REFRESH_SECONDS while a client asked within CLIENT_WINDOW_SECONDS, else IDLE_SECONDS."""
    if last_request is None or now - last_request > CLIENT_WINDOW_SECONDS:
        return IDLE_SECONDS
    return REFRESH_SECONDS


def note_request() -> None:
    now = time.monotonic()
    idle = beat_interval(now, LAST_REQUEST[0]) == IDLE_SECONDS
    LAST_REQUEST[0] = now
    if idle:
        WAKE.set()


def rotate_own_logs() -> None:
    """serverd.err/.out are held open by launchd: copied and truncated, never renamed (LC-12)."""
    try:
        import log_policy
        for f in service_identity.log_files():
            log_policy.rotate(f, copy_truncate=True)
    except Exception as exc:  # noqa: BLE001 — a rotation must never stop the server
        print(f"log rotation: {type(exc).__name__}: {exc}", file=sys.stderr)


class Runtime:
    """What the fabric-service routes answer, held in memory.

    Built once per process after the instance lock. `refresh()` runs on the
    heartbeat thread; the request threads only read what it left, under a lock,
    so the well-known route costs a dictionary copy and never a file read.
    """

    def __init__(self, token: str | None, token_problem: str | None = None) -> None:
        self.started_at = fs.now_iso()
        self.build = service_identity.build()
        self.instance = service_identity.instance()
        self.token = token
        self.token_problem = token_problem
        self._status = "starting"
        self._snapshot: dict | None = None
        self._failures: dict[str, str] = {}  # source -> reason, until that source works again
        self._lock = threading.Lock()

    def _own_degraded(self) -> list[dict]:
        rows = []
        if self.token_problem:
            rows.append({"source": "service-token",
                         "reason": f"the events feed is off: {self.token_problem}"[:300]})
        rows += [{"source": k, "reason": v[:300]} for k, v in sorted(self._failures.items())]
        return rows

    def note_failure(self, source: str, reason: str | None) -> None:
        """Record (or with None, clear) a failing part; it shows in `degraded` until cleared."""
        with self._lock:
            if reason:
                self._failures[source] = reason
            else:
                self._failures.pop(source, None)

    def refresh(self, leaks: dict | None = None) -> dict | None:
        """Rebuild the snapshot. A failure is a degraded source, never an endless `starting`."""
        try:
            snap = service_health.snapshot(leaks if leaks is not None else refresh_leaks(),
                                           extra_degraded=self._own_degraded())
        except Exception as exc:  # noqa: BLE001 — the answer must stay truthful, not stuck
            self.note_failure("health", f"the health snapshot failed: {type(exc).__name__}: {exc}")
            with self._lock:
                if self._status == "starting":
                    self._status = "degraded"
            return None
        self.note_failure("health", None)
        with self._lock:
            self._snapshot = snap
            if self._status in ("starting", "degraded"):
                self._status = "ready"
        return snap

    def stopping(self) -> None:
        with self._lock:
            self._status = "stopping"

    def well_known(self) -> dict:
        with self._lock:
            snap, status = self._snapshot, self._status
        own = self._own_degraded()
        degraded = (snap["degraded"] + [d for d in own if d not in snap["degraded"]]) if snap else own
        return fs.build_well_known(
            service_id=service_identity.SERVICE_ID, instance=self.instance,
            name=service_identity.NAME, version=VERSION, build=self.build,
            started_at=self.started_at, status=status, degraded=degraded,
            summary=snap["summary"] if snap else None, surfaces=SURFACES)


#: Set by `serve()`; None in a process that only imports this module.
RUNTIME: Runtime | None = None


def beat_once(runtime: Runtime) -> None:
    """One heartbeat cycle. The snapshot is refreshed whether or not the receipt could be
    written, so a full disk or a bad collector costs a degraded row, not the answer."""
    leaks = None
    try:
        doc = heartbeat()
        leaks = doc.get("leaks")
        runtime.note_failure("heartbeat", None)
    except Exception as exc:  # noqa: BLE001 — the beat survives a bad cycle
        print(f"heartbeat: {type(exc).__name__}: {exc}", file=sys.stderr)
        runtime.note_failure("heartbeat", f"the heartbeat receipt could not be written: {type(exc).__name__}: {exc}")
    runtime.refresh(leaks)


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

    # region client-disconnect — docs: docs/runs/2026-10-01-client-disconnect/README.md
    def handle(self):
        # Browsers and polling clients may leave while a response is in flight.
        # This ends that connection, not the service; other I/O errors propagate.
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
    # endregion client-disconnect

    #: True while answering HEAD: every route runs as for GET, so the status
    #: and headers (Content-Length included) are the GET answer's, and only the
    #: body is withheld (RFC 9110 §9.3.2). HEAD was 501, so `curl -I`, a link
    #: checker or an uptime probe read a working page as a broken server.
    _head = False

    def _body(self, body: bytes) -> None:
        if not self._head:
            self.wfile.write(body)

    def do_HEAD(self):                                    # noqa: N802
        self._head = True
        try:
            self.do_GET()
        finally:
            self._head = False

    def log_message(self, fmt, *args):                    # quiet by design;
        pass                                              # launchd keeps stderr

    def _json(self, doc, code=200):
        body = json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._body(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _events(self):
        """`GET /fabric/v1/events?after=&limit=` — the token, then a page."""
        rt = RUNTIME
        if rt is None or not rt.token:
            why = rt.token_problem if rt else "the service is starting"
            self._json({"error": f"The events feed is unavailable: {why}"}, 503)
            return
        presented = self.headers.get_all("Authorization") or []
        if len(presented) > 1:
            self._json({"error": "Send one Authorization header."}, 400)
            return
        if not fs.token_matches(presented[0] if presented else None, rt.token):
            body = json.dumps({"error": "The service token is required: "
                               "Authorization: Bearer <token from the descriptor's tokenFile>."}).encode()
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Bearer")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._body(body)
            return
        query = parse_qs(urlsplit(self.path).query)
        try:
            page = service_events.page(paths.DB, (query.get("after") or [None])[0],
                                       fs.parse_limit((query.get("limit") or [None])[0]),
                                       service_health.project_labels())
        except fs.ServiceError as exc:
            self._json({"error": str(exc)}, 400)
            return
        except service_events.FeedError as exc:
            self._json({"error": str(exc)}, 503)
            return
        self._json(page)

    def do_GET(self):                                     # noqa: N802
        note_request()
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
                    self._body(body)
                    return
                self._json({"error": "the pages are not built yet",
                            "build_with": "project-observatory full dashboard"}, 404)
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
                            "build_with": "project-observatory full dashboard"}, 404)
                return
            body = page.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._body(body)
        elif route == "/.well-known/fabric-service":
            if RUNTIME is None:
                self._json({"error": "the service is starting"}, 503)
            else:
                self._json(RUNTIME.well_known())
        elif route == "/fabric/v1/events":
            self._events()
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
                                   "/leaks", "/skills", "/.well-known/fabric-service",
                                   "/fabric/v1/events"]}, 404)

    def do_POST(self):                                    # noqa: N802
        self._json({"error": "GET only — this server changes nothing"}, 405)


class LoopbackServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse lookup in server_bind().

    http.server.HTTPServer.server_bind() calls socket.getfqdn(host) between bind() and
    listen(). A Mac with a slow or broken resolver stalls there with the port bound but
    not listening, so a probe neither connects nor is refused. A loopback service knows
    its own name.
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def serve(port: int) -> int:
    """Lock, token, bind, beat — in that order, and the order is the point.

    The lock comes before ANY side effect (fabric-service/0.1, "one copy"): a
    second copy started by hand, by `full open --serve` or by a stale launchd
    job must leave nothing behind — no token, no receipt, no socket. It exits
    75 with one sentence naming the holder (`fs.hold_single_instance`).
    """
    global RUNTIME
    lock = fs.hold_single_instance(service_identity.lock_dir())
    token, problem = None, None
    try:
        token = fs.ensure_token(service_identity.token_file())
    except (fs.ServiceError, OSError) as exc:
        # The dashboard does not need the token, so a bad token file costs the
        # events feed, said in `degraded`, and not the whole server.
        problem = str(exc) if isinstance(exc, fs.ServiceError) else f"{type(exc).__name__}: {exc.strerror}"
        print(f"events token: {problem}", file=sys.stderr)
    RUNTIME = Runtime(token, problem)
    # After the lock: an update restarts this server without re-running the
    # installer, and the manifest the descriptor points at must describe THIS code.
    stale = service_identity.refresh_installed_manifest()
    if stale:
        print(stale, file=sys.stderr)
    try:
        srv = LoopbackServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        print(f"Cannot listen on 127.0.0.1:{port}: {exc.strerror or exc}. "
              f"Another program holds the port; pass --port.", file=sys.stderr)
        lock.release()
        return 1
    stop = threading.Event()

    def beat():
        rotated = time.monotonic()
        rotate_own_logs()
        while not stop.is_set():
            beat_once(RUNTIME)
            if time.monotonic() - rotated >= LOG_ROTATE_SECONDS:
                rotate_own_logs()
                rotated = time.monotonic()
            WAKE.wait(beat_interval(time.monotonic(), LAST_REQUEST[0]))
            WAKE.clear()

    def on_term(_signum, _frame):
        # SIGTERM is how launchd stops a job: say `stopping`, stop the beat,
        # let in-flight requests finish, release the lock, exit 0. shutdown()
        # waits for serve_forever, which runs on THIS thread, so it goes to
        # another one.
        RUNTIME.stopping()
        stop.set()
        WAKE.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    # A blocked mask survives exec: a server started from a thread that blocks
    # asynchronous signals (the Mac app's bridge did) never saw SIGTERM, so neither
    # `full open --stop` nor launchd could stop it. Unblock what stops us.
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT, signal.SIGHUP})
    threading.Thread(target=beat, daemon=True).start()
    print(f"observatory serverd {VERSION} on http://127.0.0.1:{port} "
          f"(pid {os.getpid()}, instance {RUNTIME.instance})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
        lock.release()
    return 0


def build_plist() -> dict:
    return {
        "Label": LABEL,
        # The venv's interpreter or a keg's `opt` link, never a Cellar path (LC-05).
        "ProgramArguments": [install_launchd.stable_interpreter(sys.executable),
                             str(ROOT / "tools/serverd.py"), "--run", "--port", str(PORT)],
        "RunAtLoad": True,
        "KeepAlive": True,
        # fabric-service/0.1 supervisor rules: a crash loop is bounded, and
        # SIGKILL comes only after the SIGTERM drain has had its time.
        "ThrottleInterval": 10,
        "ExitTimeOut": EXIT_TIMEOUT + 10,
        # STANDARD, NOT BACKGROUND. The server answers Fabric Dashboards' probe and
        # every agent's MCP call while the Mac is busy. As a Background job with
        # Nice 5 and low-priority I/O, macOS starved it under load: on 2026-10-01
        # the host recorded it "not answering" 30 times in a day while the process
        # ran without one restart. The tick stays background work; the server is not.
        "ProcessType": "Standard",
        "StandardErrorPath": str(paths.STATE / "logs/serverd.err"),
        "StandardOutPath": str(paths.STATE / "logs/serverd.out"),
        "EnvironmentVariables": install_launchd.environment(),
        "Umask": 0o077,
    }


def install() -> int:
    """Descriptor, plist, launchd — and proof the job answers as THIS service.

    The descriptor goes first because it is where a port is claimed
    (`fs.write_descriptor` refuses a port another installed service declares),
    and a refused claim must leave no plist behind. Then the kit's
    `launchd_install`: write and lint the plist, `enable`, `bootout` and WAIT for
    the unload, `bootstrap` with retries on launchd's transient I/O error, and
    poll the well-known document until it answers with this id and instance.
    """
    if not install_launchd.scheduler_allowed():
        print("Scheduler disabled: enable features.scheduler before installing background jobs", file=sys.stderr)
        return 1
    if sys.platform != "darwin":
        print("launchd is supported on macOS only", file=sys.stderr)
        return 1
    if PLIST.is_symlink():
        print(f"Not installed: {PLIST} is a symbolic link", file=sys.stderr)
        return 1
    # The plist is linted before anything is written: a refused plist leaves no descriptor.
    try:
        doc = build_plist()
    except configuration.ConfigurationError as exc:
        print(f"Not installed: {exc}", file=sys.stderr)
        return 1
    problems = install_launchd.lint_plist(doc)
    if problems:
        print("Not installed: " + "; ".join(problems), file=sys.stderr)
        return 1
    install_launchd.prepare_logs(("serverd.err", "serverd.out"), paths.STATE / "logs")
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    try:
        # The manifest first: the descriptor points at it, and a host that reads
        # the descriptor must find a manifest it can run, not the template.
        service_identity.write_installed_manifest()
        where = fs.write_descriptor(service_identity.descriptor(PORT, label=LABEL, plist=PLIST))
    except (fs.ServiceError, OSError) as exc:
        print(f"Not installed: {exc}", file=sys.stderr)
        return 1
    try:
        fs.launchd_install(LABEL, PLIST, plistlib.dumps(doc),
                           origin=f"http://127.0.0.1:{PORT}",
                           service_id=service_identity.SERVICE_ID,
                           instance=service_identity.instance())
    except (fs.ServiceError, OSError, subprocess.SubprocessError) as exc:
        print(f"{exc} The descriptor ({where}) and the plist stay in place; "
              f"`python \"$(project-observatory full-path)/tools/serverd.py\" --uninstall` removes both.", file=sys.stderr)
        return 1
    finally:
        # The kit writes the plist 644; it holds no secret, but every other
        # plist this engine writes is 600 and a reader should not have to ask why.
        if PLIST.is_file() and not PLIST.is_symlink():
            PLIST.chmod(0o600)
    print(f"installed and started: {LABEL} (RunAtLoad + KeepAlive) — "
          f"http://127.0.0.1:{PORT}/")
    print(f"  fabric-service descriptor: {where}")
    print(f"  off is `python \"$(project-observatory full-path)/tools/serverd.py\" --uninstall`; off STAYS off until --install")
    return 0


def uninstall() -> int:
    """Off: bootout, the plist, the descriptor. The workspace's data stays."""
    existed = PLIST.exists()
    unloaded = True
    if sys.platform == "darwin":
        fs.launchd_uninstall(LABEL, PLIST)
        # bootout returns before the job is gone: SIGTERM, the drain, then the
        # unload. "Stopped" is said only once launchd no longer knows the label.
        deadline = time.monotonic() + EXIT_TIMEOUT
        while fs.launchd_loaded(LABEL) and time.monotonic() < deadline:
            time.sleep(0.25)
        unloaded = not fs.launchd_loaded(LABEL)
    else:
        PLIST.unlink(missing_ok=True)
    described = fs.remove_descriptor(service_identity.SERVICE_ID, service_identity.instance())
    if not unloaded:
        print(f"the plist is removed but launchd still lists {LABEL}; "
              f"check `launchctl print gui/{os.getuid()}/{LABEL}`", file=sys.stderr)
    print("stopped and removed the launchd agent" if existed else
          "nothing was installed")
    if described:
        print("  removed the fabric-service descriptor")
    return 0 if unloaded else 1


def status() -> int:
    doc = _read_json(RECEIPT)
    if not doc:
        print("no heartbeat receipt — the server has never run here")
        return 1
    age = time.time() - datetime.datetime.strptime(
        doc["at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc).timestamp()
    alive = age < float(doc.get("silent_after_s") or REFRESH_SECONDS * 3)
    print(f"{'UP' if alive else 'SILENT'} — last heartbeat {int(age)}s ago, "
          f"pid {doc.get('pid')}, port {doc.get('port')}, "
          f"uptime {doc.get('uptime_s')}s")
    r = doc.get("remote") or {}
    print(f"  remote watch: {r.get('at_risk_total', '?')} checkout(s) at risk "
          f"across states {r.get('states')}")
    l = doc.get("leaks") or {}
    print(f"  leaks: {l.get('open', '?')} open of {l.get('total', '?')} recorded")
    print(f"  installed: {'yes' if PLIST.exists() else 'no (not always-on)'}")
    descriptor = fs.descriptor_path(service_identity.SERVICE_ID, service_identity.instance())
    print(f"  fabric-service: {service_identity.SERVICE_ID}.{service_identity.instance()}, "
          f"descriptor {'present' if descriptor.is_file() else 'absent'} ({descriptor})")
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
