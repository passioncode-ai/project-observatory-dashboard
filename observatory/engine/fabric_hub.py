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
- before the binding credential is sent, `hub.json` is read again and the pid it names must be
  alive: while Fabric is down any program can listen on its port, so a dead pid means "Fabric
  is not running" and nothing is sent;
- the door token (Fabric's file, named by `hub.json`) asks once with `fabric.access.request`
  (callee `fabric`, capability `project.resolve`, agentId Observatory's registry id); on the
  operator's own machine Fabric allows it at once and journals it, and the first
  `fabric.access.status` read carries the binding credential, once;
- the credential is kept in an owner-only file (`private_io`) under the workspace state, never
  in an argument, a log line, a URL or an answer, and it is sent to the hub origin only.

Every failure is an answer: `HubUnavailable` carries a reason code (`hub-absent`,
`hub-dead`, `hub-origin-refused`, `access-pending`, `access-refused`, `hub-error`) that the
collector names in `degraded`.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import paths
import private_io
import service_identity

HUB_ENV = "OBSERVATORY_FABRIC_HUB_JSON"
CAPABILITY = "project.resolve"
TOOL = "fabric.project.resolve"
#: The resource the access request names. Fabric's tool lands after Fabric 0.3.7 and its
#: resource vocabulary is not published yet; this is the stub's, replaced when it is.
RESOURCE = "projects"
REFUSALS = ("no-project", "ambiguous", "too-broad", "detached", "bad-path")
_ORIGIN = re.compile(r"^http://127\.0\.0\.1:[0-9]{1,5}\Z")
PROTOCOL_VERSION = "2025-06-18"
#: After Fabric refuses the access request, the same Fabric process is asked again only this long after.
REASK_SECONDS = 6 * 3600


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
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_hub() -> dict:
    """The hub as `hub.json` names it, checked: a loopback origin used verbatim and a live pid."""
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
    mcp = doc.get("mcp") if isinstance(doc.get("mcp"), str) and str(doc.get("mcp")).startswith("/") else "/mcp"
    return {"origin": origin, "url": origin + mcp, "pid": doc["pid"], "startedAt": doc.get("startedAt"),
            "doorTokenFile": doc.get("doorTokenFile")}


class _Session:
    """One MCP Streamable HTTP session with the hub, under one bearer."""

    def __init__(self, hub: dict, bearer: str, timeout: float) -> None:
        self.hub, self.bearer, self.timeout = hub, bearer, timeout
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
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=self.timeout) as response:
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
            message = str((answer.get("error") or {}).get("message") or "")[:200]
            if "not found" in message.lower() or "unknown tool" in message.lower():
                raise HubUnavailable("tool-absent", f"this Fabric does not serve {name} yet")
            raise HubUnavailable("hub-error", f"the hub refused {name}")
        result = answer.get("result") or {}
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            try:
                structured = json.loads(((result.get("content") or [{}])[0]).get("text") or "{}")
            except (ValueError, AttributeError, IndexError):
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


def request_access(hub: dict, *, timeout: float = 5.0, polls: int = 5) -> str:
    """Ask once through the door; collect the binding credential on Allow. Raises when not allowed."""
    door = _Session(hub, _door_token(hub), timeout).open()
    pending: dict = {}
    try:
        pending = json.loads(private_io.read(_pending_file()))
    except (OSError, ValueError, RuntimeError):
        pending = {}
    refused_at = pending.get("refusedAt") if isinstance(pending, dict) else None
    if (isinstance(refused_at, (int, float)) and pending.get("pid") == hub["pid"]
            and time.time() - refused_at < REASK_SECONDS):
        # Fabric said no to this same process recently: asking every tick would only fill its journal.
        # A restarted (perhaps upgraded) Fabric is asked at once.
        raise HubUnavailable("access-refused", "Fabric refused the access request; asked again after "
                                               f"{REASK_SECONDS // 3600} h or when Fabric restarts")
    if not (isinstance(pending, dict) and pending.get("requestId") and pending.get("pollSecret")
            and pending.get("pid") == hub["pid"]):
        answer, failed = door.call("fabric.access.request", {
            "agentId": service_identity.SERVICE_ID, "callee": "fabric", "capabilities": [CAPABILITY],
            "resources": [RESOURCE],
            "reason": "Project Observatory reads which Fabric project each of its project folders belongs to, "
                      "and records the answer as an alias in its own store. Read-only; it never creates a project."})
        if failed or not answer.get("requestId") or answer.get("status") == "denied":
            private_io.write(_pending_file(), json.dumps({"refusedAt": time.time(), "pid": hub["pid"]}))
            raise HubUnavailable("access-refused", "Fabric refused the access request"
                                 if answer.get("status") != "denied" else "the operator denied this access in Fabric")
        if not answer.get("pollSecret"):
            raise HubUnavailable("access-pending", "an earlier request is waiting; its poll secret is not here")
        pending = {"requestId": answer["requestId"], "pollSecret": answer["pollSecret"], "pid": hub["pid"]}
        private_io.write(_pending_file(), json.dumps(pending))
    for attempt in range(max(1, polls)):
        status, failed = door.call("fabric.access.status", {"requestId": pending["requestId"],
                                                            "pollSecret": pending["pollSecret"]})
        if failed:
            _pending_file().unlink(missing_ok=True)
            raise HubUnavailable("access-refused", "Fabric does not know the access request any more")
        state = status.get("status")
        credential = status.get("credential")
        if state == "allowed" and isinstance(credential, str) and credential:
            _save_binding(credential, hub)
            _pending_file().unlink(missing_ok=True)
            return credential
        if state in ("denied", "expired") or (state == "allowed" and not credential):
            _pending_file().unlink(missing_ok=True)
            raise HubUnavailable("access-refused", f"the access request is {state}")
        if attempt + 1 < polls:
            time.sleep(0.2)
    raise HubUnavailable("access-pending", "the access request waits for the operator in Fabric")


class Resolver:
    """`fabric.project.resolve` over one hub session, opened on first use."""

    def __init__(self, *, timeout: float = 5.0, ask_access: bool = True) -> None:
        self.timeout, self.ask_access = timeout, ask_access
        self.hub: dict | None = None
        self.session: _Session | None = None

    def _open(self) -> _Session:
        hub = read_hub()
        credential = _load_binding(hub)
        if credential is None:
            if not self.ask_access:
                raise HubUnavailable("access-pending", "Observatory holds no Fabric credential yet; the next tick asks")
            credential = request_access(hub, timeout=self.timeout)
        # Read hub.json again right before the credential leaves: the pid must still be alive and the
        # origin the same one (a restarted Fabric on another port is a new hub).
        again = read_hub()
        if again["origin"] != hub["origin"] or again["pid"] != hub["pid"]:
            raise HubUnavailable("hub-dead", "Fabric restarted while Observatory was asking")
        try:
            self.session = _Session(again, credential, self.timeout).open()
        except HubUnavailable as exc:
            if exc.code == "access-refused":
                forget_binding()  # revoked: the next tick asks again
            raise
        self.hub = again
        return self.session

    def resolve(self, path: str) -> dict:
        """`{"ok": True, projectId, name, root, how, remoteKey}` or `{"ok": False, "refusal": code}`."""
        session = self.session or self._open()
        try:
            answer, failed = session.call(TOOL, {"path": path})
        except HubUnavailable as exc:
            if exc.code == "access-refused":
                forget_binding()
            raise
        if failed:
            code = str(((answer.get("error") or {}) if isinstance(answer.get("error"), dict) else {}).get("code") or "")
            if code in ("access-required", "unauthorized", "forbidden"):
                forget_binding()
                raise HubUnavailable("access-refused", "Fabric refused the call: access is required")
            return {"ok": False, "refusal": code if code in REFUSALS else "hub-error"}
        pid = answer.get("projectId")
        if not isinstance(pid, str) or not re.fullmatch(r"[0-9A-Za-z-]{8,64}", pid):
            return {"ok": False, "refusal": "hub-error"}
        return {"ok": True, "projectId": pid, "name": answer.get("name"), "root": answer.get("root"),
                "how": answer.get("how"), "remoteKey": answer.get("remoteKey")}
