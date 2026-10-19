"""Asking Fabric's hub which Fabric project a folder belongs to (`fabric.project.resolve`).

Fabric owns the canonical project id and serves folder attribution from its hub (the one
every coding agent reaches through `fabric-mcp`; the fabric-agent-contract's DEC-0089 and
Fabric's ADR-0166, "Fabric is the core"). Observatory PULLS: it asks, and records the answer in
its own store. Fabric writes nothing into Observatory, and this module writes nothing into
Fabric beyond the one access request below.

The caller proof, as the hub's own tool descriptions require:

- the hub is found ONLY through `hub.json` in Fabric's application-support folder, and its
  `origin` is used exactly as written, and only when it is `http://127.0.0.1:<port>`: a
  `localhost` that resolves elsewhere, or any other host, is refused;
- before the door token or the binding credential is sent, `hub.json` is read again and the pid it
  names must be alive, must own the socket listening on the hub's port, run as this user and have
  started no later than the hub did (`verify_listener`): while Fabric is down any program can listen
  on its port and a pid can be reused, so anything else is `hub-unverified` and nothing is sent;
- no proxy is ever used for the hub, and a redirect is refused, so the bearer reaches that origin only;
- the door token (Fabric's file, named by `hub.json`) asks once with `fabric.access.request`
  (callee `fabric`, capability `project.resolve`, agentId Observatory's registry id); on the
  operator's own machine Fabric allows it at once and journals it, and the first
  `fabric.access.status` read carries the binding credential, once;
- the credential is kept in an owner-only file (`private_io`) under the workspace state, never
  in an argument, a log line, a URL or an answer, and it is sent to the hub origin only.

Every failure is an answer: `HubUnavailable` carries a reason code (`hub-absent`,
`hub-dead`, `hub-origin-refused`, `hub-unverified`, `access-pending`, `access-refused`, `tool-absent`,
`hub-error`, `budget`) that the
collector names in `degraded`.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import osprivacy
import osproc
import paths
import private_io
import service_identity

HUB_ENV = "OBSERVATORY_FABRIC_HUB_JSON"
CAPABILITY = "project.resolve"
TOOL = "fabric.project.resolve"
#: The resource the access request names. Fabric's tool lands after Fabric 0.3.7 and its
#: resource vocabulary is not published yet; this is the stub's, replaced when it is.
RESOURCE = "projects"
_ORIGIN = re.compile(r"^http://127\.0\.0\.1:[0-9]{1,5}\Z")
PROTOCOL_VERSION = "2025-06-18"
#: After Fabric refuses the access request, the same Fabric process is asked again only this long after.
REASK_SECONDS = 6 * 3600
#: A waiting request whose poll secret was lost and that names no expiry is asked again after this long.
WAIT_SECONDS = 3600


class HubUnavailable(Exception):
    """The hub could not be asked; `code` is the reason the collector reports."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


def hub_file() -> Path:
    """Where Fabric writes `hub.json`. Fabric is a macOS app; elsewhere there is no hub."""
    override = os.environ.get(HUB_ENV)
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "ai.passioncode.fabric" / "hub.json"


def binding_file() -> Path:
    return paths.STATE / "fabric-hub" / "binding.json"


def _alive(pid: Any) -> bool:
    """Asked through `osproc.alive`, never by sending a signal: on Windows a signal 0 terminates the process."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    return osproc.alive(pid)


_VERIFIED: dict[tuple, str] = {}


def _iso_epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _run(argv: list[str], timeout: float) -> str | None:
    # The C locale: `ps` and `lsof` print what this reads in the same form in every language.
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _etime_seconds(value: str) -> int | None:
    """`ps -o etime=` (`[[dd-]hh:]mm:ss`) in seconds, or None."""
    m = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", value.strip())
    if not m:
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def verify_listener(pid: int, port: int, started_at: Any, *, timeout: float = 2.0) -> str:
    """"" when the process that owns the listening socket IS the Fabric `hub.json` names, else why not.

    A live pid is not enough: once Fabric has quit its pid can be reused, and any program can listen on
    the hub's port. So the listener on that port must be that pid, run by this user, and started no later
    than the hub says it started (a reused pid starts later). Read with `lsof` and `ps`; where they are
    absent (Windows, a minimal Linux) the hub cannot be verified and nothing is sent.
    """
    key = (pid, port, started_at)
    if key in _VERIFIED:
        return _VERIFIED[key]
    reason = ""
    if os.name == "nt":
        reason = "the hub's listener cannot be verified on Windows"
    else:
        out = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"], timeout)
        owners = {int(line[1:]) for line in (out or "").splitlines() if line[:1] == "p" and line[1:].isdigit()}
        # `etime` ([[dd-]hh:]mm:ss since the process started) is numbers in every locale, unlike `lstart`'s
        # localised date, and needs no local-time conversion.
        info = _run(["ps", "-o", "uid=,etime=", "-p", str(pid)], timeout) or ""
        parts = info.split()
        if out is None:
            reason = "the listener on the hub's port could not be read (lsof)"
        elif owners != {pid}:
            reason = "the process listening on the hub's port is not the one hub.json names"
        elif len(parts) != 2 or parts[0] != osprivacy.account():
            reason = "the hub process does not run as this user"
        else:
            elapsed = _etime_seconds(parts[1])
            began = None if elapsed is None else time.time() - elapsed
            hub_started = _iso_epoch(started_at)
            if began is None or hub_started is None:
                reason = "the hub's start time could not be compared with its process"
            elif began > hub_started + 5:
                reason = "the process with the hub's pid started after the hub did: the pid was reused"
    _VERIFIED[key] = reason
    return reason


def read_hub(*, verify: bool = True) -> dict:
    """The hub as `hub.json` names it, checked: a loopback origin used verbatim, a live pid, and
    (`verify`) that pid owning the listening socket, as this user, since before the hub started."""
    try:
        doc = json.loads(hub_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HubUnavailable("hub-absent", "Fabric's hub.json is absent: Fabric is not running") from None
    except (OSError, ValueError):
        raise HubUnavailable("hub-absent", "Fabric's hub.json could not be read") from None
    if not isinstance(doc, dict):
        raise HubUnavailable("hub-absent", "Fabric's hub.json is not an object")
    origin = doc.get("origin")
    if not isinstance(origin, str) or not _ORIGIN.match(origin):
        raise HubUnavailable("hub-origin-refused", "hub.json names an origin other than http://127.0.0.1:<port>")
    if not _alive(doc.get("pid")):
        raise HubUnavailable("hub-dead", "the pid hub.json names is not running: Fabric is not running")
    if verify:
        why = verify_listener(doc["pid"], int(origin.rsplit(":", 1)[1]), doc.get("startedAt"))
        if why:
            raise HubUnavailable("hub-unverified", why)
    mcp = doc.get("mcp") if isinstance(doc.get("mcp"), str) and str(doc.get("mcp")).startswith("/") else "/mcp"
    return {"origin": origin, "url": origin + mcp, "pid": doc["pid"], "startedAt": doc.get("startedAt"),
            "doorTokenFile": doc.get("doorTokenFile")}


class _Session:
    """One MCP Streamable HTTP session with the hub, under one bearer."""

    def __init__(self, hub: dict, bearer: str, timeout: float, deadline: float | None = None) -> None:
        self.hub, self.bearer, self.timeout, self.deadline = hub, bearer, timeout, deadline
        self.session_id: str | None = None
        self.next_id = 1

    def _post(self, body: dict) -> Any:
        # The URL is built from the checked origin only; the bearer goes nowhere else.
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": "Bearer " + self.bearer, "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(self.hub["url"], data=json.dumps(body).encode("utf-8"),
                                         headers=headers, method="POST")
        # No proxy, ever: urllib's default ProxyHandler would hand the bearer to whatever http_proxy names.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        timeout = self.timeout
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise HubUnavailable("budget", "the pass's time budget ran out")
            timeout = min(timeout, left)
        try:
            with opener.open(request, timeout=timeout) as response:
                self.session_id = response.headers.get("Mcp-Session-Id") or self.session_id
                raw = response.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
                kind = response.headers.get("Content-Type") or ""
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise HubUnavailable("access-refused", f"the hub refused the credential (HTTP {exc.code})") from None
            raise HubUnavailable("hub-error", f"the hub answered HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise HubUnavailable("hub-error", f"the hub did not answer: {type(exc).__name__}") from None
        if "id" not in body:
            return None
        if "text/event-stream" in kind:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    try:
                        message = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(message, dict) and message.get("id") == body["id"]:
                        return message
            raise HubUnavailable("hub-error", "the hub's event stream carried no answer")
        try:
            return json.loads(raw)
        except ValueError:
            raise HubUnavailable("hub-error", "the hub's answer is not JSON") from None

    def open(self) -> "_Session":
        answer = self._post({"jsonrpc": "2.0", "id": self._id(), "method": "initialize",
                             "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                        "clientInfo": {"name": service_identity.SERVICE_ID, "version": "1"}}})
        if not isinstance(answer, dict) or "result" not in answer:
            raise HubUnavailable("hub-error", "the hub refused the MCP handshake")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def tools(self) -> set[str]:
        answer = self._post({"jsonrpc": "2.0", "id": self._id(), "method": "tools/list", "params": {}})
        tools = ((answer or {}).get("result") or {}).get("tools") if isinstance(answer, dict) else None
        return {t.get("name") for t in tools or [] if isinstance(t, dict)}

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id - 1

    def call(self, name: str, arguments: dict) -> tuple[dict, bool]:
        """(structured answer, isError)."""
        answer = self._post({"jsonrpc": "2.0", "id": self._id(), "method": "tools/call",
                             "params": {"name": name, "arguments": arguments}})
        if not isinstance(answer, dict):
            raise HubUnavailable("hub-error", "the hub sent no answer")
        if "error" in answer:
            error = answer.get("error")
            message = str(error.get("message") if isinstance(error, dict) else error or "")[:200]
            if "not found" in message.lower() or "unknown tool" in message.lower():
                raise HubUnavailable("tool-absent", f"this Fabric does not serve {name} yet")
            raise HubUnavailable("hub-error", f"the hub refused {name}")
        result = answer.get("result")
        if not isinstance(result, dict):
            raise HubUnavailable("hub-error", f"the hub's answer to {name} is not an object")
        content = result.get("content") if isinstance(result.get("content"), list) else []
        text = " ".join(str(c.get("text") or "") for c in content if isinstance(c, dict))
        if result.get("isError") and re.search(r"tool\b.*\bnot found|unknown tool", text, re.I):
            # The MCP SDK answers an unknown tool as a tool error ("MCP error -32602: Tool X not found").
            raise HubUnavailable("tool-absent", f"this Fabric does not serve {name} yet")
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            try:
                structured = json.loads(((content or [{}])[0]).get("text") or "{}")
            except (ValueError, AttributeError, IndexError, TypeError):
                structured = {}
        return (structured if isinstance(structured, dict) else {}), bool(result.get("isError"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the bearer to another origin: refuse it."""

    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def _load_binding(hub: dict) -> str | None:
    try:
        doc = json.loads(private_io.read(binding_file()))
    except (OSError, ValueError, RuntimeError):
        return None
    value = doc.get("credential") if isinstance(doc, dict) else None
    return value if isinstance(value, str) and value else None


def _save_binding(credential: str, hub: dict) -> None:
    private_io.write(binding_file(), json.dumps({"credential": credential, "pid": hub["pid"],
                                                 "obtained": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))


def forget_binding() -> None:
    try:
        binding_file().unlink()
    except OSError:
        pass


def _pending_file() -> Path:
    return paths.STATE / "fabric-hub" / "request.json"


def _door_token(hub: dict) -> str:
    name = hub.get("doorTokenFile")
    if not isinstance(name, str) or not os.path.isabs(name):
        raise HubUnavailable("access-refused", "hub.json names no door token")
    try:
        token = Path(name).read_text(encoding="utf-8").strip()
    except OSError:
        raise HubUnavailable("access-refused", "Fabric's door token could not be read") from None
    if not token:
        raise HubUnavailable("access-refused", "Fabric's door token is empty")
    return token


def _read_pending() -> dict:
    try:
        doc = json.loads(private_io.read(_pending_file()))
    except (OSError, ValueError, RuntimeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def request_access(hub: dict, *, timeout: float = 5.0, polls: int = 5, deadline: float | None = None) -> str:
    """Ask once through the door; collect the binding credential on Allow. Raises when not allowed.

    `request.json` (owner-only) remembers the request between passes: its id and poll secret while it
    waits, when it expires, and when Fabric refused. A request whose poll secret was lost (Fabric hands it
    out once) is left to expire, then asked again; a refusal by the same Fabric process is asked again after
    `REASK_SECONDS`; a restarted Fabric is asked at once. Nothing gets stuck.
    """
    door = _Session(hub, _door_token(hub), timeout, deadline).open()
    pending = _read_pending()
    same = pending.get("pid") == hub["pid"]
    refused_at = pending.get("refusedAt")
    if same and isinstance(refused_at, (int, float)) and time.time() - refused_at < REASK_SECONDS:
        # Fabric said no to this same process recently: asking every tick would only fill its journal.
        raise HubUnavailable("access-refused", "Fabric refused the access request; asked again after "
                                               f"{REASK_SECONDS // 3600} h or when Fabric restarts")
    # Without an expiry from Fabric, a request whose secret was lost is asked again after `WAIT_SECONDS`.
    asked_at = pending.get("askedAt") if isinstance(pending.get("askedAt"), (int, float)) else 0
    waits_until = _iso_epoch(pending.get("expiresAt")) or asked_at + WAIT_SECONDS
    if same and not pending.get("pollSecret") and waits_until and time.time() < waits_until:
        raise HubUnavailable("access-pending", "a request waits in Fabric and its poll secret was lost; "
                                               "asked again when it expires at " + str(pending.get("expiresAt")))
    if not (same and pending.get("requestId") and pending.get("pollSecret")):
        answer, failed = door.call("fabric.access.request", {
            "agentId": service_identity.SERVICE_ID, "callee": "fabric", "capabilities": [CAPABILITY],
            "resources": [RESOURCE],
            "reason": "Project Observatory reads which Fabric project each of its project folders belongs to, "
                      "and records the answer as an alias in its own store. Read-only; it never creates a project."})
        if failed or not answer.get("requestId") or answer.get("status") == "denied":
            private_io.write(_pending_file(), json.dumps({"refusedAt": time.time(), "pid": hub["pid"]}))
            raise HubUnavailable("access-refused", "Fabric refused the access request"
                                 if answer.get("status") != "denied" else "the operator denied this access in Fabric")
        pending = {"requestId": answer["requestId"], "pollSecret": answer.get("pollSecret"), "pid": hub["pid"],
                   "expiresAt": answer.get("expiresAt"), "askedAt": time.time()}
        private_io.write(_pending_file(), json.dumps(pending))
        if not pending["pollSecret"]:
            raise HubUnavailable("access-pending", "a request waits in Fabric and its poll secret was lost; "
                                                   "asked again when it expires")
    for attempt in range(max(1, polls)):
        status, failed = door.call("fabric.access.status", {"requestId": pending["requestId"],
                                                            "pollSecret": pending["pollSecret"]})
        if failed:
            _pending_file().unlink(missing_ok=True)
            raise HubUnavailable("access-pending", "Fabric does not know the access request any more; asked again")
        state = status.get("status")
        credential = status.get("credential")
        if state == "allowed" and isinstance(credential, str) and credential:
            _save_binding(credential, hub)
            _pending_file().unlink(missing_ok=True)
            return credential
        if state in ("expired",) or (state == "allowed" and not credential):
            _pending_file().unlink(missing_ok=True)  # the next pass asks again
            raise HubUnavailable("access-pending", f"the access request is {state} without a credential; asked again")
        if state == "denied":
            private_io.write(_pending_file(), json.dumps({"refusedAt": time.time(), "pid": hub["pid"]}))
            raise HubUnavailable("access-refused", "the operator denied this access in Fabric")
        if attempt + 1 < polls:
            time.sleep(0.2)
    raise HubUnavailable("access-pending", "the access request waits for the operator in Fabric")


#: The contract's refusals that say where a folder stands (D3): each is an answer, cached and acted on.
#: `ambiguous` is a conflict to report; the others withdraw nothing on their own (see fabric_attribution).
TERMINAL = ("no-project", "detached", "too-broad", "bad-path", "ambiguous")


class Resolver:
    """`fabric.project.resolve` over one hub session, opened on first use.

    `deadline` (a `time.monotonic()` value) bounds every request; past it the call answers `budget`.
    `check_tools` asks the hub's tool list once and answers `tool-absent` when the tool is not on it."""

    def __init__(self, *, timeout: float = 5.0, ask_access: bool = True, deadline: float | None = None,
                 check_tools: bool = True) -> None:
        self.timeout, self.ask_access, self.deadline, self.check_tools = timeout, ask_access, deadline, check_tools
        self.hub: dict | None = None
        self.session: _Session | None = None

    def _open(self) -> _Session:
        hub = read_hub()
        credential = _load_binding(hub)
        if credential is None:
            if not self.ask_access:
                raise HubUnavailable("access-pending", "Observatory holds no Fabric credential yet; the next tick asks")
            credential = request_access(hub, timeout=self.timeout, deadline=self.deadline)
        # Read hub.json again right before the credential leaves: the pid must still be the verified
        # listener and the origin the same one (a restarted Fabric on another port is a new hub).
        again = read_hub()
        if again["origin"] != hub["origin"] or again["pid"] != hub["pid"]:
            raise HubUnavailable("hub-dead", "Fabric restarted while Observatory was asking")
        try:
            session = _Session(again, credential, self.timeout, self.deadline).open()
            if self.check_tools and TOOL not in session.tools():
                raise HubUnavailable("tool-absent", f"this Fabric does not serve {TOOL} yet")
        except HubUnavailable as exc:
            if exc.code == "access-refused":
                forget_binding()  # revoked: the next tick asks again
            raise
        self.session, self.hub = session, again
        return session

    def resolve(self, path: str) -> dict:
        """`{"ok": True, projectId, name, root, how, remoteKey}`, `{"ok": False, "refusal": code}` for one of
        the contract's refusals, or HubUnavailable for anything else (never read as an answer)."""
        session = self.session or self._open()
        try:
            answer, failed = session.call(TOOL, {"path": path})
        except HubUnavailable as exc:
            if exc.code == "access-refused":
                forget_binding()
            raise
        if failed:
            error = answer.get("error") if isinstance(answer.get("error"), dict) else {}
            code = str(error.get("code") or "")
            if code in ("access-required", "unauthorized", "forbidden"):
                forget_binding()
                raise HubUnavailable("access-refused", "Fabric refused the call: access is required")
            if code in TERMINAL:
                return {"ok": False, "refusal": code}
            raise HubUnavailable("hub-error", "the hub answered with an error that is not one of the contract's refusals")
        pid = answer.get("projectId")
        if not isinstance(pid, str) or not re.fullmatch(r"[0-9A-Za-z-]{8,64}", pid):
            raise HubUnavailable("hub-error", "the hub's answer names no project id")
        return {"ok": True, "projectId": pid, "name": answer.get("name"), "root": answer.get("root"),
                "how": answer.get("how"), "remoteKey": answer.get("remoteKey")}
