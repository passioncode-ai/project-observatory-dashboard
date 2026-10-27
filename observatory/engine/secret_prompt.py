"""The native secret prompt, the node's half (OBS-260; docs/design/PERSON-SURFACES.md D4; scenario OSS-84).

An agent that needs a key the vault does not hold calls `observatory_secret_request` instead of asking the person
for it in a chat. The stdio MCP server, a child of the agent's process, shows nothing and never touches a value: it
files the request with the node (the engine's local server, `tools/serverd.py`), and this module is what the node
does with it. What it keeps, and why:

- **The agent is named by its binding, never by its words.** A local agent gets a binding when the person connects
  it (`project-observatory full agent-binding issue NAME`, at a terminal): a random bearer in an owner-only file
  under `config/agent-bindings/`, whose SHA-256 alone is kept in `config/agent-bindings.json`. The stdio server
  started with `--agent NAME` presents that bearer on `POST /mcp`, and `agent_for` maps the hash back to NAME. A
  request whose text claims another agent still shows the binding's agent. The limit, stated as D4 states it: the
  token file is owner-only, so any agent running as the same user can read another agent's token and file a request
  under its name. The prompt still shows the slot and the reason, and nothing is stored without the person typing it.
- **The queue is the node's alone.** `<state>/secret-requests/queue.json`, owner-only, changed under one lock. One
  open request per slot (the same agent asking again joins it; another agent is told the slot is busy), five new
  requests per agent per hour, ten minutes for the person to answer (`MAX_DEADLINE_SECONDS`, contract draft DEC-0143
  SP-5). A slot that already holds a value is refused until the agent asks with `replace: true`, and the prompt then
  asks the person whether to replace it, naming its last change.
- **No value ever reaches this module.** The prompt window belongs to Observatory's own signed app, which writes the
  value into the vault slot in its own process (D4). The node only reads the slot's metadata afterwards: `stored` is
  reported when the slot was written after the request was filed, and `unconfirmed` when the app said stored but the
  slot does not show it (SP-5). Nothing here takes, logs, journals or returns a value.
- **Every final state is named.** `stored`, `declined`, `no-answer` (the deadline passed), `unconfirmed`,
  `no-client` (no genuine window could be shown) and `cancelled` (the agent withdrew it). Refusals that file
  nothing: `no-desktop` (a server or a remote shell, D4: the person runs `vault.py put` in their own shell),
  `no-client` before filing, `slot-filled`, `slot-busy`, `rate-limited`, `invalid-argument`.
- **The window is the Mac app's, opened through the lifecycle broker.** The local server installs `BrokerPrompter`:
  on macOS, when the person enrolled Observatory's own app (`ai.passioncode.observatory`) with the node's lifecycle
  broker, a filed request asks the broker to start that app (or find it running), never a direct launch. The app
  reads the waiting requests through its door below (`full assistant secret`), shows its `SecureField` window with
  the person's phrase, and writes the value itself. Not enrolled, held off, or the broker not set up: `no-client`
  with the person's step, before anything is filed. **Windows has no window in this release:** the node has no
  lifecycle broker there yet (OBS-277), and D4 opens the window only through it, so the answer is `no-client`,
  reason `broker-unsupported`. On Linux the node can never verify that a window is the signed app (DEC-0143 SP-2), so
  the answer stays `no-client`, reason `secret-ui-unavailable`. Outside the local server `PROMPTER` stays
  `NotBuilt`, which offers no window and fakes none. The queue, its states and its outcomes are already the ones
  the contract's `secret.prompt` channel method serves (draft DEC-0143), so the channel can replace the broker path
  without changing the agent's tool.
- **The app's door reads and answers only for the engine inside the app.** `client_requests`, `client_store`,
  `client_decline` and `client_unavailable` run in the engine process the app itself started (`full assistant secret`,
  its JSON on that process's stdin). Before any of them reads a request's text or touches a slot, `CLIENT_CHECK`
  requires two things: this module's own file lies inside the enrolled app's bundle (the engine bundled in the app,
  not one a preference names), and this process's immediate parent, while running, satisfies the code requirement of
  the enrolled app (its team and bundle id, `codesign --verify -R` on the parent's pid). A grandparent is never
  enough: the app also runs the engine its settings name for other actions, and that engine's own child would find
  the app two levels up. Anything else is refused `client-unverified`. `client_store` writes the value into the slot
  here, as `vault.py put` (an empty slot) or `rotate` (a replacement) writes it, so the value travels only on the pipe
  between the app and its own child and never through the node, an argument, the environment, a log, the journal or
  an answer.
- **Only through the engine inside the signed app.** The app never runs the `executable` preference, PATH or a
  launcher for this door: it runs only the engine inside its own bundle (`BUNDLED_ENGINE`), by an absolute path it
  computes, after checking its own static signature (sealed resources, against the team its sealed Info.plist names)
  immediately before each call. **No release bundles that engine yet**, so the node answers every request
  `no-client`, reason `engine-untrusted`, with the person's `vault.py put` step, before filing it.
- **What this does not protect against, stated plainly.**
  - The signature check hashes the bundle once; the Python engine then reads its `.py` files as it imports them
    (`client_store` imports the vault writer after the value arrived on stdin). A program of the same user that may
    write the bundle (a drag-installed app is the user's; App Management grants it) could swap a file after the check
    and restore it afterwards. Before an engine is bundled it must be one that cannot change after the check (copied
    into storage only the app can write, or a hardened-runtime binary with library validation), or the bundle must
    be one the user cannot write; and its workspace (`OBSERVATORY_HOME`, today from the app's settings), interpreter
    and dependencies must be inside the sealed bundle too.
  - The queue (`queue.json`) is an owner-only file, which means any program running as the same user can read it
    and write it: it can read every request's text, file requests under any agent's name with any reason, and set
    outcomes, bypassing the agent binding and the `engine-untrusted` gate. What it cannot do is see a value typed into
    the genuine window. Once stored, the slot is readable by any program of the same user, which is D4's stated
    property (out of the chat, transcripts, logs and tool results, not out of reach of a program that goes looking).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets as _secrets
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import paths
import private_io

#: The bindings registry's schema and file, under `config/`.
BINDINGS_SCHEMA = "agent-bindings/1"
BINDINGS_FILE = "agent-bindings.json"
TOKENS_FOLDER = "agent-bindings"
QUEUE_SCHEMA = "secret-requests/1"
AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
REQUEST_ID = re.compile(r"^sreq_[0-9a-f]{24}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
#: The person has this long to answer; contract draft DEC-0143 SP-5 caps a secret request's deadline at 10 minutes.
MAX_DEADLINE_SECONDS = 600
#: New requests one agent may file in an hour (a join is not a new request).
PER_AGENT_PER_HOUR = 5
WINDOW_SECONDS = 3600
REASON_MAX = 300
#: Final requests are kept this long for `secret.get`, then dropped by the next write.
KEEP_SECONDS = 7 * 86400
#: The journal is rotated to `events.jsonl.1` past this size: names only, but still bounded.
JOURNAL_MAX_BYTES = 1_000_000

OPEN = "waiting"
OUTCOMES = ("stored", "declined", "no-answer", "unconfirmed", "no-client", "cancelled")
TOOLS = ("secret.request", "secret.get", "secret.cancel")
MCP_REVISION = "2026-07-28"
HANDSHAKE_REVISIONS = ("2025-03-26", "2025-06-18", "2025-11-25", MCP_REVISION)

#: What each answer means to the agent and what to do next. One line per code; tests pin that every code has one.
MESSAGES = {
    "stored": "the person stored the value in the vault slot; use it through use_secret.py, never ask for the value",
    "declined": "the person pressed Cancel; do not ask again for the same reason",
    "no-answer": "the person did not answer within 10 minutes",
    "unconfirmed": "the app reported the value stored, but the vault slot does not show a write after the request",
    "no-client": "Observatory's own prompt window cannot be shown here",
    "cancelled": "the request was withdrawn",
    "no-desktop": "no desktop to ask the person on",
    "slot-filled": "the slot already holds a value",
    "slot-busy": "another agent's request for this slot is waiting for the person",
    "rate-limited": "an agent may file five secret requests an hour",
    "invalid-argument": "the arguments do not match the tool's input schema",
    "unknown-request": "no request of yours has that id (final requests are kept 7 days)",
    "unavailable": "the request queue cannot be read; nothing was filed",
    "client-unverified": "only Observatory's own signed app may read or answer a secret request",
    "request-closed": "the request is no longer waiting",
    "not-stored": "the value was not written to the vault slot",
    "value-empty": "the value is empty; nothing was written",
    "value-multiline": "the value carries a line break; one secret per slot, nothing was written",
    "value-unchanged": "the new value equals the one the slot holds; nothing was written",
    "slot-filled-now": "the slot was filled after the request without asking to replace it; nothing was written",
}
#: Why a window cannot be shown (`no-client`), by reason code.
NO_CLIENT = {
    "window-not-built": "this release has the request queue but not the prompt window yet (OBS-260)",
    "secret-ui-unavailable": "on this system the node cannot verify that a prompt window is Observatory's own "
                             "signed app, so it shows none",
    "app-not-started": "Observatory's app did not start",
    "app-not-enrolled": "Observatory's app is not enrolled with this node's lifecycle broker, which is the only way "
                        "the node opens it",
    "app-held-off": "Observatory's app is held off with the lifecycle broker, so the node does not open it",
    "broker-unsupported": "this release has no lifecycle broker on Windows (OBS-277), and the prompt window is opened "
                          "only through it",
    "phrase-store-unavailable": "this release of Observatory's app cannot keep the person's phrase yet (its "
                                "signing carries no provisioning profile for the keychain group), and a window "
                                "without the phrase is not shown",
    "engine-untrusted": "Observatory's app takes a key only through the engine inside its own signed app, and this "
                        "release's app carries none, so no window is shown",
}
#: The steps that make a genuine window possible, by reason code; shown beside the person's vault.py step.
NO_CLIENT_FIX = {
    "app-not-enrolled": ("enroll the app once, at your terminal: project-observatory full lifecycle enroll "
                         "observatory.app --kind app --path PATH --caller service:observatory --startup-effect TEXT, "
                         "with PATH the installed Project Observatory.app"),
    "app-held-off": "release it at your terminal: project-observatory full lifecycle hold observatory.app --release",
}
#: Observatory's own Mac app, the only app the node opens for a prompt (macos/scripts/build-app.sh).
OBSERVATORY_BUNDLE_ID = "ai.passioncode.observatory"
#: What the app may say when it cannot show a genuine window (`client_unavailable`).
CLIENT_REASONS = ("phrase-store-unavailable",)
#: Where the signed app carries its own engine (macos `SecretEngine.relativePath`). The window takes a key only through
#: it; no release bundles one yet, so the node answers `engine-untrusted` before filing.
BUNDLED_ENGINE = "Contents/Resources/engine/bin/project-observatory"


class BindingsError(ValueError):
    """`config/agent-bindings.json` is not a valid agent-bindings/1; no binding in it is honoured."""


class Refused(Exception):
    """A request the node answers with a code and does not file (or a request id it does not know)."""

    def __init__(self, code: str, **detail: Any) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


# ── time ────────────────────────────────────────────────────────────────────
def _stamp(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ── agent bindings ──────────────────────────────────────────────────────────
def bindings_path() -> Path:
    return paths.CONFIG / BINDINGS_FILE


def token_path(name: str) -> Path:
    if not AGENT_NAME.match(name or ""):
        raise BindingsError("an agent name is 1 to 64 lowercase letters, digits, '.', '_' or '-'")
    return paths.CONFIG / TOKENS_FOLDER / f"{name}.token"


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def read_bindings() -> dict:
    """The registry, validated; an absent file is an empty registry. A broken one raises: none of it applies."""
    path = bindings_path()
    try:
        raw = private_io.read(path)
    except FileNotFoundError:
        return {"schema": BINDINGS_SCHEMA, "revision": 0, "bindings": []}
    except (OSError, RuntimeError) as exc:
        raise BindingsError(f"the agent bindings cannot be read ({type(exc).__name__}); "
                            f"the file must be owner-only and reached through no link") from None
    try:
        doc = json.loads(raw)
    except ValueError:
        raise BindingsError("config/agent-bindings.json is not JSON") from None
    if not isinstance(doc, dict) or doc.get("schema") != BINDINGS_SCHEMA \
            or type(doc.get("revision")) is not int or not isinstance(doc.get("bindings"), list):
        raise BindingsError(f"config/agent-bindings.json must be {{schema: {BINDINGS_SCHEMA}, revision, bindings}}")
    names, digests = set(), set()
    for row in doc["bindings"]:
        if not isinstance(row, dict) or set(row) != {"name", "digest", "issuedAt", "revokedAt"} \
                or not AGENT_NAME.match(str(row.get("name"))) or not DIGEST.match(str(row.get("digest"))) \
                or _seconds(row.get("issuedAt")) is None \
                or (row.get("revokedAt") is not None and _seconds(row.get("revokedAt")) is None):
            raise BindingsError("a binding must be {name, digest, issuedAt, revokedAt}")
        if row["name"] in names or row["digest"] in digests:
            raise BindingsError("two bindings share a name or a credential")
        names.add(row["name"])
        digests.add(row["digest"])
    return doc


def _write_bindings(doc: dict) -> None:
    doc["revision"] = int(doc["revision"]) + 1
    private_io.write(bindings_path(), json.dumps(doc, indent=1) + "\n")


def issue(name: str, *, now: float | None = None) -> tuple[Path, bool]:
    """(token file, newly issued). An agent with a live binding and its token file keeps them: issuing again is
    how the person reconnects an agent, and a new token would cut the one already configured off."""
    path = token_path(name)
    with private_io.lock(paths.CONFIG / f"{TOKENS_FOLDER}.lock"):
        doc = read_bindings()
        live = next((b for b in doc["bindings"] if b["name"] == name and b["revokedAt"] is None), None)
        if live is not None and path.is_file():
            try:
                if digest(private_io.read(path).strip()) == live["digest"]:
                    return path, False
            except (OSError, RuntimeError):
                pass
        token = _secrets.token_urlsafe(32)
        private_io.write(path, token + "\n")
        doc["bindings"] = [b for b in doc["bindings"] if b["name"] != name]
        doc["bindings"].append({"name": name, "digest": digest(token),
                                "issuedAt": _stamp(now if now is not None else time.time()), "revokedAt": None})
        _write_bindings(doc)
        return path, True


def revoke(name: str, *, now: float | None = None) -> bool:
    """Revoke the agent's binding and remove its token file. False when it had no live binding."""
    path = token_path(name)
    with private_io.lock(paths.CONFIG / f"{TOKENS_FOLDER}.lock"):
        doc = read_bindings()
        found = False
        for row in doc["bindings"]:
            if row["name"] == name and row["revokedAt"] is None:
                row["revokedAt"] = _stamp(now if now is not None else time.time())
                found = True
        if found:
            _write_bindings(doc)
        path.unlink(missing_ok=True)
        return found


def agent_for(authorization: Optional[str]) -> Optional[str]:
    """The agent a presented `Authorization: Bearer ...` header names, or None. The bearer is hashed here and goes
    no further. A broken registry names nobody, so a caller is then refused rather than guessed."""
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer "):].strip()
    if not token:
        return None
    try:
        doc = read_bindings()
    except BindingsError:
        return None
    wanted = digest(token)
    # Every live binding is compared, each in constant time, so the answer's timing says nothing about which digest
    # came closest (the review of #378).
    found = None
    for row in doc["bindings"]:
        if hmac.compare_digest(row["digest"], wanted) and row["revokedAt"] is None:
            found = row["name"]
    return found


# ── the desktop and the window ──────────────────────────────────────────────
def desktop(platform: str | None = None, env: dict | None = None) -> tuple[bool, str]:
    """(a person can be shown a window on this machine now, how that was decided).

    macOS: the console belongs to this user (`/dev/console`'s owner), which is false over SSH with nobody logged in
    at the screen and for another user's session. Linux: the session is X11 or Wayland. Windows: the process is not
    in session 0, where services run with no desktop."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    if platform == "darwin":
        try:
            import osprivacy
            return osprivacy.owned_by_me("/dev/console"), "console-owner"
        except OSError:
            return False, "console-unreadable"
    if platform.startswith("linux"):
        kind = str(env.get("XDG_SESSION_TYPE") or "").lower()
        if kind in ("x11", "wayland") or env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"):
            return True, "graphical-session"
        return False, "no-graphical-session"
    if platform == "win32":
        try:
            import ctypes
            session = ctypes.c_ulong(0)
            ok = ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))  # type: ignore[attr-defined]
            return (bool(ok) and session.value != 0), "session-id"
        except (OSError, AttributeError):
            return False, "session-unreadable"
    return False, "unsupported-platform"


def verifiable_client(platform: str | None = None) -> bool:
    """Whether the node can check, by the operating system, that a prompt window is Observatory's own signed app
    (contract draft DEC-0143 SP-2): macOS (audit token and designated requirement) and Windows (Authenticode on the
    held process). On Linux and anything else it cannot, so it never shows a window there, whatever ships."""
    return (platform or sys.platform) in ("darwin", "win32")


class NotBuilt:
    """The prompter of this release: no window exists yet, so none is offered and none is faked."""

    def available(self, platform: str | None = None) -> Optional[str]:
        platform = platform or sys.platform
        return "window-not-built" if platform in ("darwin", "win32") else "secret-ui-unavailable"

    def open(self, request: dict) -> bool:          # never reached: available() is never None
        return False

    def withdraw(self, request: dict) -> None:
        return None


def observatory_target(document: dict) -> Optional[dict]:
    """The enrolled target that is Observatory's own app (by its bundle id), or None. `document` is the node's
    lifecycle registry (`config/node-lifecycle.json`)."""
    for target in (document.get("targets") or {}).values():
        identity = target.get("identity") if isinstance(target, dict) else None
        if target.get("kind") == "app" and isinstance(identity, dict) \
                and identity.get("bundleId") == OBSERVATORY_BUNDLE_ID and target.get("approved") is True:
            return target
    return None


def enrolled_app_intact(target: dict) -> bool:
    """The enrolled app's bundle, deep, satisfies its enrolled code requirement now (`codesign --verify --deep
    --strict -R`): a file planted in it fails. macOS only; a check that cannot run is not a pass."""
    if sys.platform != "darwin":
        return False
    try:
        from node.lifecycle import macos
        requirement = macos.requirement_for(target["identity"]["bundleId"], target["identity"]["teamId"])
        return macos.satisfies(str(target["path"]), requirement, deep=True, run=macos.Runner())
    except Exception:  # noqa: BLE001
        return False


#: Whether the enrolled app is still the signed app it was enrolled as; tests replace it.
APP_VERIFIED: Callable[[dict], bool] = enrolled_app_intact


class BrokerPrompter:
    """The prompter the local server installs (D4): Observatory's signed app, opened through the node's lifecycle
    broker and never directly. `host` answers the server's `node.lifecycle` Host (or None when it has none).

    `open` asks the broker to start the enrolled app, or to find it running, idempotent per request. The app shows the
    window when it reads the queue (it reads it at launch and every few seconds), so the request id travels through
    the queue, not through an argument. A start the broker refuses, or that ends without the app running, closes the
    request `no-client` (reason `app-not-started`). `withdraw` has nothing to close: the app reads the queue again and
    takes down a window whose request is no longer waiting."""

    #: How often the watch reads the broker's operation, and for how long at most.
    WATCH_SECONDS = 1.0

    def __init__(self, host: Callable[[], Any]) -> None:
        self.host = host

    def _service(self):
        host = self.host()
        try:
            return host.service() if host is not None else None
        except Exception:  # noqa: BLE001 - a broker that cannot start is "not set up", never a crash of a request
            return None

    def _target(self, service) -> tuple[Optional[dict], Optional[str]]:
        if service is None:
            return None, "app-not-enrolled"
        try:
            target = observatory_target(service.read())
        except Exception:  # noqa: BLE001 - an unreadable registry opens nothing
            return None, "app-not-enrolled"
        if target is None:
            return None, "app-not-enrolled"
        if target.get("desiredState") == "held_off":
            return None, "app-held-off"
        try:
            bundled = (Path(str(target.get("path") or "")) / BUNDLED_ENGINE).is_file()
        except OSError:
            bundled = False
        if not bundled or not APP_VERIFIED(target):
            # The app's window runs only the engine inside the signed app; without one, or in a bundle that is no
            # longer the enrolled signed app (a planted file), it would read nothing, so nothing is filed and the agent
            # hears it now, not after ten minutes.
            return None, "engine-untrusted"
        return target, None

    def available(self, platform: str | None = None) -> Optional[str]:
        platform = platform or sys.platform
        if platform == "win32":
            return "broker-unsupported"
        if platform != "darwin":
            return "secret-ui-unavailable"
        return self._target(self._service())[1]

    def open(self, request: dict) -> bool:
        service = self._service()
        target, _why = self._target(service)
        if target is None:
            return False
        try:
            receipt = service.broker.open_for_prompt(target["targetId"], request["requestId"])
        except Exception:  # noqa: BLE001 - a refused start (held_off, busy, denied) shows no window
            return False
        state = receipt.get("state")
        if state in ("ready", "already_ready"):
            return True
        if state != "running":
            return False
        threading.Thread(target=self._watch, args=(service, receipt["operationId"], request["requestId"]),
                         name="secret-prompt-open", daemon=True).start()
        return True

    def _watch(self, service, operation_id: str, request_id: str) -> None:
        """Follow the broker's start; a start that fails closes the request at once instead of after ten minutes."""
        end = time.monotonic() + MAX_DEADLINE_SECONDS
        while time.monotonic() < end:
            try:
                state = service.broker.prompt_operation(operation_id).get("state")
            except Exception:  # noqa: BLE001 - the broker closed: the request keeps its own deadline
                return
            if state in ("ready", "already_ready"):
                return
            if state != "running":
                try:
                    _finish(request_id, "no-client", why=NO_CLIENT["app-not-started"], reason="app-not-started")
                except Refused:
                    pass
                return
            time.sleep(self.WATCH_SECONDS)

    def withdraw(self, request: dict) -> None:
        return None


#: The prompter in force: `NotBuilt` outside the local server, `BrokerPrompter` inside it (`tools/serverd.py`).
#: `available()` None means a genuine window can be shown, `open` asks for it, `withdraw` takes it down.
PROMPTER: Any = NotBuilt()
#: How the node decides there is a desktop; tests replace it.
DESKTOP: Callable[[], tuple[bool, str]] = desktop
#: Whether a signed client can be verified on this system; tests replace it.
VERIFIABLE: Callable[[], bool] = verifiable_client
CLOCK: Callable[[], float] = time.time


def fix(reason: Optional[str]) -> dict:
    """The step that makes a genuine window possible next time, when there is one."""
    return {"fix": NO_CLIENT_FIX[reason]} if reason in NO_CLIENT_FIX else {}


def step(project: str, env: str, name: str) -> str:
    """What the person can do instead, in their own shell (D4): never an answer an agent can type for them."""
    return (f"ask the person to run, in their own shell: python \"$(project-observatory full-path)/tools/vault.py\" "
            f"put {project} {env} {name} < FILE, with the value in a protected FILE (stdin only, never an argument "
            f"or a chat message)")


# ── the vault slot, read by its metadata only ───────────────────────────────
def _vault():
    tools = str(Path(__file__).resolve().parent / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import vault
    return vault


def slot_state(project: str, env: str, name: str) -> dict:
    """Whether the slot holds a value and when it last changed, from its existence and `meta.json`. The value file
    is never opened."""
    vault = _vault()
    slot = vault._slot(project, env, name)
    meta = vault._read_meta(slot) if slot.is_file() else {}
    changes = [meta.get(k) for k in ("rotated", "updated", "created") if isinstance(meta.get(k), str)]
    last = max(changes, key=lambda v: _seconds(v) or 0) if changes else None
    return {"filled": slot.is_file(), "lastChange": last}


def validate(arguments: Any) -> dict:
    """The request's arguments, checked, or `Refused('invalid-argument')`. Any field naming an agent is refused:
    the agent comes from the binding."""
    if not isinstance(arguments, dict):
        raise Refused("invalid-argument", field="arguments")
    allowed = {"project", "env", "slot", "provider", "why", "replace"}
    extra = sorted(set(arguments) - allowed)
    if extra:
        raise Refused("invalid-argument", field=extra[0])
    for field in ("project", "env", "slot", "why"):
        if not isinstance(arguments.get(field), str) or not arguments[field].strip():
            raise Refused("invalid-argument", field=field)
    vault = _vault()
    try:
        # PROJECT as `vault.py put` takes it: the folder name, or the registry id normalised to that folder.
        project, _said = vault.project_folder(arguments["project"], arguments["env"], arguments["slot"], verb="put")
        vault.validate_names(project)
    except vault.VaultBoundaryError as exc:
        raise Refused("invalid-argument", field="project", why=str(exc)) from None
    try:
        vault.validate_names(project, arguments["env"])
    except vault.VaultBoundaryError as exc:
        raise Refused("invalid-argument", field="env", why=str(exc)) from None
    try:
        vault.validate_names(project, arguments["env"], arguments["slot"])
    except vault.VaultBoundaryError as exc:
        raise Refused("invalid-argument", field="slot", why=str(exc)) from None
    import credential_shape
    provider = arguments.get("provider")
    if provider is not None and (not isinstance(provider, str) or not PROVIDER.match(provider)
                                 or credential_shape.find(provider)):
        raise Refused("invalid-argument", field="provider")
    reason = arguments["why"].strip()
    # One plain line: no control, format or separator character (U+2028, U+2029, the bidi overrides and isolates),
    # which could reorder or hide what the window shows as the agent's words.
    if len(reason) > REASON_MAX or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in reason):
        raise Refused("invalid-argument", field="why", why=f"one plain line of at most {REASON_MAX} characters")
    if credential_shape.find(reason):
        raise Refused("invalid-argument", field="why", why="it looks like it carries a credential; say why the "
                                                          "key is needed, never a value")
    replace = arguments.get("replace", False)
    if not isinstance(replace, bool):
        raise Refused("invalid-argument", field="replace")
    return {"project": project, "env": arguments["env"], "name": arguments["slot"],
            "provider": provider, "reason": reason, "replace": replace}


# ── the queue ───────────────────────────────────────────────────────────────
def state_dir() -> Path:
    return paths.STATE / "secret-requests"


def _queue_path() -> Path:
    return state_dir() / "queue.json"


def _read_queue() -> dict:
    try:
        doc = json.loads(private_io.read(_queue_path()))
    except FileNotFoundError:
        return {"schema": QUEUE_SCHEMA, "requests": []}
    except (OSError, RuntimeError, ValueError):
        raise Refused("unavailable") from None
    if not isinstance(doc, dict) or doc.get("schema") != QUEUE_SCHEMA or not isinstance(doc.get("requests"), list):
        raise Refused("unavailable")
    return doc


def _write_queue(doc: dict, now: float) -> None:
    doc["requests"] = [r for r in doc["requests"]
                       if r.get("state") == OPEN or now - (_seconds(r.get("answeredAt")) or now) < KEEP_SECONDS]
    private_io.write(_queue_path(), json.dumps(doc, indent=1) + "\n")


def _locked():
    return private_io.lock(state_dir() / "queue.lock")


def journal(event: str, **fields: Any) -> None:
    """One line per filing, answer and refusal: names, slots and codes, never a value."""
    assert "value" not in fields, "a value never enters the journal"
    path = state_dir() / "events.jsonl"
    private_io.parent(path.parent)
    try:
        if path.is_file() and path.stat().st_size > JOURNAL_MAX_BYTES:
            os.replace(path, path.with_name("events.jsonl.1"))
    except OSError:
        pass
    import osprivacy
    line = json.dumps({"at": _stamp(CLOCK()), "event": event, **fields}, ensure_ascii=False) + "\n"
    fd = osprivacy.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | osprivacy.NOFOLLOW, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _slot_text(r: dict) -> str:
    return f"{r['project']}/{r['env']}/{r['name']}"


def _reconcile(r: dict, now: float) -> bool:
    """Close a request whose deadline passed (`no-answer`). True when it changed."""
    if r.get("state") == OPEN and now >= (_seconds(r.get("deadline")) or 0):
        r.update(state="no-answer", answeredAt=_stamp(now))
        return True
    return False


def view(r: dict) -> dict:
    """What the agent may read about its request: no value, ever, and no other agent's request."""
    out = {"requestId": r["id"], "agent": r["agent"], "slot": _slot_text(r), "replaces": r["replaces"],
           "state": r["state"], "createdAt": r["createdAt"], "deadline": r["deadline"]}
    if r["state"] != OPEN:
        out["outcome"] = r["state"]
        out["message"] = MESSAGES[r["state"]]
        out["answeredAt"] = r.get("answeredAt")
        if r.get("why"):
            out["why"] = r["why"]
        if r["state"] in ("no-answer", "no-client", "unconfirmed"):
            out["step"] = step(r["project"], r["env"], r["name"])
        if r.get("reason"):
            out["reason"] = r["reason"]
            out.update(fix(r["reason"]))
    return out


def prompt_view(r: dict) -> dict:
    """What the signed app shows (DEC-0143 `secretPrompt`): the agent from its binding, the project and the slot, the
    reason as the agent's own words, whether it replaces a value. No value travels in either direction."""
    return {"requestId": r["id"], "agent": r["agent"], "project": r["project"],
            "slot": {"env": r["env"], "name": r["name"]}, "provider": r.get("provider"),
            "reason": r["reason"], "replaces": r["replaces"], "lastChange": r.get("lastChange"),
            "deadline": r["deadline"]}


def file(agent: str, arguments: Any) -> dict:
    """File one request for `agent` (from its binding) and offer it to the person. Returns `{"request": view,
    "joined": bool}`; raises `Refused` for anything it does not file."""
    if not AGENT_NAME.match(agent or ""):
        raise Refused("invalid-argument", field="agent")
    try:
        args = validate(arguments)
    except Refused as exc:
        # Journalled like every refusal (ONBOARDING): the agent and the field only, never the arguments' text,
        # which may be what the shape check refused.
        journal("refused", code=exc.code, agent=agent, field=exc.detail.get("field"))
        raise
    where = {"agent": agent, "slot": f"{args['project']}/{args['env']}/{args['name']}"}
    try:
        return _file(agent, args, where)
    except Refused as exc:
        journal("refused", code=exc.code, **where)
        raise


def _file(agent: str, args: dict, where: dict) -> dict:
    ok, _how = DESKTOP()
    if not ok:
        raise Refused("no-desktop", step=step(args["project"], args["env"], args["name"]))
    blocked = "secret-ui-unavailable" if not VERIFIABLE() else PROMPTER.available()
    if blocked is not None:
        raise Refused("no-client", why=NO_CLIENT.get(blocked, blocked), reason=blocked,
                      step=step(args["project"], args["env"], args["name"]), **fix(blocked))
    state = slot_state(args["project"], args["env"], args["name"])
    if state["filled"] and not args["replace"]:
        raise Refused("slot-filled", lastChange=state["lastChange"],
                      why="ask again with replace: true and the person will be asked whether to replace it")
    now = CLOCK()
    with _locked():
        doc = _read_queue()
        changed = any([_reconcile(r, now) for r in doc["requests"]])
        same = next((r for r in doc["requests"] if r["state"] == OPEN and r["project"] == args["project"]
                     and r["env"] == args["env"] and r["name"] == args["name"]), None)
        if same is not None:
            if changed:
                _write_queue(doc, now)
            if same["agent"] != agent:
                raise Refused("slot-busy")
            return {"request": view(same), "joined": True}
        recent = sorted(_seconds(r["createdAt"]) or 0 for r in doc["requests"]
                        if r["agent"] == agent and now - (_seconds(r["createdAt"]) or 0) < WINDOW_SECONDS)
        if len(recent) >= PER_AGENT_PER_HOUR:
            if changed:
                _write_queue(doc, now)
            raise Refused("rate-limited", nextAt=_stamp(recent[-PER_AGENT_PER_HOUR] + WINDOW_SECONDS))
        request = {"id": "sreq_" + _secrets.token_hex(12), "agent": agent, "project": args["project"],
                   "env": args["env"], "name": args["name"], "provider": args["provider"],
                   "reason": args["reason"], "replaces": state["filled"], "lastChange": state["lastChange"],
                   "state": OPEN, "createdAt": _stamp(now), "deadline": _stamp(now + MAX_DEADLINE_SECONDS),
                   "answeredAt": None, "why": None}
        doc["requests"].append(request)
        _write_queue(doc, now)
    journal("filed", requestId=request["id"], replaces=request["replaces"], **where)
    if not PROMPTER.open(prompt_view(request)):
        request = _finish(request["id"], "no-client", why=NO_CLIENT["app-not-started"], reason="app-not-started")
    return {"request": view(request), "joined": False}


def _finish(request_id: str, outcome: str, *, why: str | None = None, agent: str | None = None,
            reason: str | None = None) -> dict:
    now = CLOCK()
    with _locked():
        doc = _read_queue()
        r = next((x for x in doc["requests"] if x["id"] == request_id
                  and (agent is None or x["agent"] == agent)), None)
        if r is None:
            raise Refused("unknown-request")
        _reconcile(r, now)
        if r["state"] == OPEN:
            r.update(state=outcome, answeredAt=_stamp(now), why=why)
            if reason is not None:
                r["reason"] = reason
            journal("answered", requestId=r["id"], outcome=outcome, agent=r["agent"], slot=_slot_text(r))
        _write_queue(doc, now)
        return r


def get(agent: str, request_id: Any) -> dict:
    if not isinstance(request_id, str) or not REQUEST_ID.match(request_id):
        raise Refused("unknown-request")
    now = CLOCK()
    with _locked():
        doc = _read_queue()
        r = next((x for x in doc["requests"] if x["id"] == request_id and x["agent"] == agent), None)
        if r is None:
            raise Refused("unknown-request")
        if _reconcile(r, now):
            journal("answered", requestId=r["id"], outcome=r["state"], agent=agent, slot=_slot_text(r))
            _write_queue(doc, now)
        return view(r)


def cancel(agent: str, request_id: Any) -> dict:
    """The agent withdraws its request (its job's `fabric.job.cancel`); a final one is returned unchanged."""
    if not isinstance(request_id, str) or not REQUEST_ID.match(request_id):
        raise Refused("unknown-request")
    r = _finish(request_id, "cancelled", agent=agent)
    if r["state"] == "cancelled":
        try:
            PROMPTER.withdraw(prompt_view(r))
        except Exception:  # noqa: BLE001 - withdrawing a window is best effort; the request is cancelled anyway
            pass
    return view(r)


def answer(request_id: str, outcome: str) -> dict:
    """The signed app's report, `stored` or `declined` (DEC-0143 `secretResult`). A late answer is ignored (the
    request already reads `no-answer`). `stored` counts only when the slot shows a write at or after the filing;
    otherwise the request reads `unconfirmed`. The app's own door to this function ships with the windows."""
    if outcome not in ("stored", "declined"):
        raise Refused("invalid-argument", field="outcome")
    if outcome == "stored":
        with _locked():
            doc = _read_queue()
            r = next((x for x in doc["requests"] if x["id"] == request_id), None)
        if r is None:
            raise Refused("unknown-request")
        state = slot_state(r["project"], r["env"], r["name"])
        written = _seconds(state["lastChange"])
        filed = _seconds(r["createdAt"]) or 0
        confirmed = state["filled"] and written is not None and written >= filed
        outcome = "stored" if confirmed else "unconfirmed"
    return view(_finish(request_id, outcome))


def waiting() -> list[dict]:
    """The open requests, as the signed app will read them (`prompt_view`)."""
    now = CLOCK()
    with _locked():
        doc = _read_queue()
        if any([_reconcile(r, now) for r in doc["requests"]]):
            _write_queue(doc, now)
        return [prompt_view(r) for r in doc["requests"] if r["state"] == OPEN]


# ── the signed app's door (`full assistant secret`) ─────────────────────────
def _running_ppid() -> int:
    return os.getppid()


def signed_app_parent(platform: str | None = None, *, document: dict | None = None, run: Any = None,
                      parent: int | None = None, source: str | None = None) -> bool:
    """Whether this door runs as the engine bundled inside the enrolled Observatory app, started by that app: this
    module's file lies inside the enrolled bundle, and the immediate parent, while running, satisfies the enrolled code
    requirement (an Apple-issued certificate of its team, its bundle id; `codesign --verify --strict -R` on the pid
    checks the running code, not a file on disk). Only the parent: the app runs other engines for other actions, and
    their children would find it as a grandparent. macOS only; anywhere else nothing is verified.

    The limit: a pid read here could in principle be reused between the read and the check; the app waits on the
    child it started, so its pid cannot be reused while this process runs."""
    if (platform or sys.platform) != "darwin":
        return False
    try:
        from node.lifecycle import macos
        from node.lifecycle import service as lifecycle
        target = observatory_target(document if document is not None else lifecycle.read_config())
        if target is None:
            return False
        bundle = Path(str(target["path"])).resolve()
        here = Path(source or __file__).resolve()
        if bundle not in here.parents:
            return False
        requirement = macos.requirement_for(target["identity"]["bundleId"], target["identity"]["teamId"])
        runner = run or macos.Runner()
        pid = _running_ppid() if parent is None else parent
        return bool(pid and pid > 1 and macos.satisfies(str(pid), requirement, deep=False, run=runner))
    except Exception:  # noqa: BLE001 - no registry, no team, no requirement, a check that could not run: no pass
        return False


#: Whether the process asking is the signed app's own child; tests replace it.
CLIENT_CHECK: Callable[[], bool] = signed_app_parent


def _client() -> None:
    if not CLIENT_CHECK():
        raise Refused("client-unverified")


def client_requests() -> list[dict]:
    """The waiting requests, as the signed app shows them. An empty queue is answered without the check (it says
    nothing); a request's text is read only for the verified app."""
    if not waiting_count():
        return []
    _client()
    return waiting()


def waiting_count() -> int:
    """How many requests wait, without their text."""
    now = CLOCK()
    with _locked():
        doc = _read_queue()
        return sum(1 for r in doc["requests"] if r["state"] == OPEN and now < (_seconds(r.get("deadline")) or 0))


def _open_request(request_id: Any) -> dict:
    if not isinstance(request_id, str) or not REQUEST_ID.match(request_id):
        raise Refused("unknown-request")
    now = CLOCK()
    with _locked():
        doc = _read_queue()
        r = next((x for x in doc["requests"] if x["id"] == request_id), None)
        if r is None:
            raise Refused("unknown-request")
        if _reconcile(r, now):
            journal("answered", requestId=r["id"], outcome=r["state"], agent=r["agent"], slot=_slot_text(r))
            _write_queue(doc, now)
        if r["state"] != OPEN:
            raise Refused("request-closed", outcome=r["state"])
        return dict(r)


def client_store(request_id: Any, value: Any) -> dict:
    """The person pressed Save: the value goes into the slot here, in the process the app started, then the request
    is answered (`stored` only when the slot shows the write). The value is never returned, logged or journalled."""
    _client()
    r = _open_request(request_id)
    if not isinstance(value, str):
        raise Refused("invalid-argument", field="value")
    vault = _vault()
    try:
        vault.store_value(r["project"], r["env"], r["name"], value, replace=bool(r["replaces"]),
                          tool="secret-prompt")
    except vault.VaultBoundaryError as exc:
        # The vault's reason by its code (an empty value, a line break, the same value, a slot filled since); its
        # messages name the rule and the slot, never the value.
        raise Refused(getattr(exc, "code", None) or "not-stored", why=str(exc)) from None
    finally:
        value = None
    return answer(r["id"], "stored")


def client_decline(request_id: Any) -> dict:
    """The person pressed Cancel or closed the window."""
    _client()
    r = _open_request(request_id)
    return answer(r["id"], "declined")


def client_unavailable(request_id: Any, reason: Any) -> dict:
    """The app cannot show a genuine window (no place for the phrase): the request ends `no-client` with the
    person's vault.py step, at once rather than after ten minutes."""
    _client()
    if reason not in CLIENT_REASONS:
        raise Refused("invalid-argument", field="reason")
    r = _open_request(request_id)
    return view(_finish(r["id"], "no-client", why=NO_CLIENT[reason], reason=reason))


# ── the MCP surface on the node's POST /mcp ─────────────────────────────────
TOOL_DEFINITIONS = [
    {"name": "secret.request",
     "description": ("File a request for a key the vault does not hold; the person is asked in Observatory's own "
                     "window (the Mac app, opened through the node's lifecycle broker; elsewhere answered no-client "
                     "with the person's vault.py step before filing) and the value is written into the slot without "
                     "passing through you. Answers the request id; follow it with secret.get. The agent is named "
                     "from your binding."),
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["project", "env", "slot", "why"],
                     "properties": {"project": {"type": "string"}, "env": {"enum": ["local", "stage", "prod"]},
                                    "slot": {"type": "string"}, "provider": {"type": "string"},
                                    "why": {"type": "string", "maxLength": REASON_MAX},
                                    "replace": {"type": "boolean"}}}},
    {"name": "secret.get",
     "description": "Read the state of a secret request you filed: waiting, or its outcome. Never a value.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["requestId"],
                     "properties": {"requestId": {"type": "string"}}}},
    {"name": "secret.cancel",
     "description": "Withdraw a secret request you filed; a final one is answered unchanged.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["requestId"],
                     "properties": {"requestId": {"type": "string"}}}},
]


def refusal(exc: Refused) -> dict:
    return {"error": exc.code, "message": MESSAGES.get(exc.code, exc.code), **exc.detail}


def _rpc(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _tool_result(structured: dict, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False)}],
            "structuredContent": structured, "isError": is_error}


def handle(agent: str, message: Any) -> Optional[dict]:
    """One JSON-RPC message from a caller whose bearer named `agent`; None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                "error": {"code": -32600, "message": "Invalid request."}}
    if "id" not in message:
        return None
    rid, method, params = message["id"], message["method"], message.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        return _rpc(rid, {"protocolVersion": asked if asked in HANDSHAKE_REVISIONS else MCP_REVISION,
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "project-observatory-secret-requests", "version": "1"}})
    if method == "ping":
        return _rpc(rid, {})
    if method == "tools/list":
        return _rpc(rid, {"tools": TOOL_DEFINITIONS})
    if method != "tools/call":
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method {method} is not served here."}}
    name = params.get("name") if isinstance(params, dict) else None
    arguments = (params.get("arguments") or {}) if isinstance(params, dict) else {}
    try:
        if name == "secret.request":
            out = file(agent, arguments)
        elif name == "secret.get":
            out = {"request": get(agent, (arguments or {}).get("requestId") if isinstance(arguments, dict) else None)}
        elif name == "secret.cancel":
            out = {"request": cancel(agent, (arguments or {}).get("requestId") if isinstance(arguments, dict) else None)}
        else:
            return _rpc(rid, _tool_result({"error": "unknown-tool", "message": "the secret tools are "
                                           + ", ".join(TOOLS)}, is_error=True))
    except Refused as exc:
        return _rpc(rid, _tool_result(refusal(exc), is_error=exc.code == "invalid-argument"))
    return _rpc(rid, _tool_result(out))
