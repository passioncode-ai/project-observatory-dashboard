"""The stdio transport of the session MCP server, able to move onto an installed release (OBS-159).

The SDK's own `stdio_server()` reads stdin through a buffered text layer: bytes it has
read but not yet handed on live in that buffer, and an `os.execv` would drop them. This
relay owns the wire instead:

- fds 0 and 1 are duplicated onto private non-inheritable descriptors, and 0/1 are pointed
  at the null device and at stderr while it serves (as the SDK does), so a handler's
  stray print never reaches the wire;
- stdin is read with `select` and `os.read` into the relay's own buffer under a lock,
  so at any moment it knows every byte it has taken off the pipe;
- each line is classified before the SDK sees it: a request (method and id) is counted
  in flight until its answer is written or the SDK settles it unanswered (a request the
  client cancelled, by whatever spelling of its id); the client's `initialize` parameters
  and its `notifications/initialized` are remembered;
- a `subscriptions/listen` request (2026-07-28) is a stream, not a unit of work: the
  client keeps it open for the whole session and nothing answers it until the server
  ends it. It is kept apart (`listens`) and never holds a swap back (OBS-228: counted
  as running, it refused every swap of every modern session on a live machine);
- every answer the SDK writes goes through the relay, which removes it from the
  in-flight set and swallows the answer to the replayed handshake (SYNTHETIC_ID).

At a request that finds the code on disk replaced (`code_freshness`), the relay holds
that request and every later one, lets the in-flight ones answer, runs the installed
release's preflight (`session_reload.preflight`), then points 0/1 back at the wire and
`execv`s the same server path with the state file inherited. The held requests and the
unread bytes go into that file; the new process replays the handshake to its SDK and
serves them, so the triggering request is answered once, by the new release. Just
before `execv` the relay ends each open listen stream the spec's graceful way, with its
result stamped with the subscription id, as the last frame the old process writes. When
the tool set changes, each stream that asked for tool-list changes first carries a
`notifications/tools/list_changed` stamped with its subscription id: at 2026-07-28 that
is the only way a change reaches a modern client, which has no stream left once the swap
is done. Whether the client then listens again is the client's choice (the pinned SDK's
own client does not re-listen by itself): a client that listens again is acknowledged by
the new release and hears later changes; one that does not hears none until it does. A
client on an earlier revision (it sent `initialize`) gets the bare notification from the
new process instead (`_resume`). A refused or failed swap
puts 0/1 back on the null device and stderr, routes the held requests to this process's
SDK in order (they answer `stale-server` with the reason) and retries only when the
reason can clear: at a later request, and, when requests still running were the reason,
the moment the last of them answers (the first idle moment), with no request needed.

Notifications the client sends while a swap is pending are forwarded at once, except a
`notifications/cancelled` for a held request, which drops that request (the client
expects no answer to it).
"""
from __future__ import annotations

import errno
import json
import os
import select
import sys
import threading
import time
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel.server import NotificationOptions
from mcp.shared._context_streams import create_context_streams
from mcp.shared.message import ServerMessageMetadata, SessionMessage
from mcp.shared.subscriptions import SUBSCRIPTION_ID_META_KEY
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

import code_freshness
import configuration
import osprivacy
import session_reload

#: The `mcp` release this relay was written and tested against. It reaches three SDK
#: internals (`mcp.shared._context_streams.create_context_streams`, `MCPServer._lowlevel_server`
#: and the lowlevel server's `run`), so tests/test_session_reload.py fails when the lock moves
#: `mcp` to another version: read those three again before changing this pin.
TESTED_MCP = "2.3.0"
#: Retry a transient refusal (an update running, requests still in flight) no sooner than this.
RETRY_SECONDS = float(os.environ.get("OBSERVATORY_RELOAD_RETRY_SECONDS", "15"))
#: A test-only fault: `fail` makes `execv` raise, so the failed-exec branch runs for real.
TEST_EXEC_ENV = "OBSERVATORY_TEST_RELOAD_EXEC"
#: The long-lived request a 2026-07-28 client keeps open for change notifications.
LISTEN = "subscriptions/listen"
#: The refusal that clears by itself once the requests running answer (`_drained`).
BUSY = "requests were still running after {seconds} s"


class Relay:
    def __init__(self, server: Any, *, script: Path, argv: list[str], protocol: str,
                 freshness: code_freshness.Freshness, home: Path, resumed: dict | None = None,
                 execv=os.execv) -> None:
        self.server = server
        self.script = Path(script)
        self.argv = [a for a in argv if a != "--reload-check"]
        self.protocol = protocol
        self.freshness = freshness
        self.home = Path(home)
        self.resumed = resumed
        self.execv = execv
        self.read_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.buffer = bytearray()
        self.paused = False
        self.eof = False
        self.inflight: dict = {}               # validated request id -> when it was handed on
        self.listens: dict = {}                # open subscriptions/listen streams: key -> the id as sent
        self.listen_tools: set = set()         # keys of the listen streams that asked for tool-list changes
        self.retry_when_idle = False           # a swap refused for running requests: retry once they answer
        self.held: list[bytes] = []
        self.swapping = False
        self.blocked: tuple | None = None      # marker identity a permanent refusal was made at
        self.retry_at = 0.0
        self.refusals_logged: set = set()
        self.init_params: dict | None = None
        self.init_id: Any = None
        self.negotiated: str | None = None
        self.initialized = False
        self.to_sdk = None
        self.tg = None

    # --- the wire ------------------------------------------------------------------

    def _claim(self) -> None:
        # os.dup gives a non-inheritable descriptor, and with 0, 1 and 2 open it lands above 2.
        self.wire_in = os.dup(0)
        self.wire_out = os.dup(1)
        self._divert()

    def _divert(self) -> None:
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        null = osprivacy.open(os.devnull, os.O_RDONLY)
        try:
            os.dup2(null, 0)
        finally:
            os.close(null)
        os.dup2(2, 1)

    def _restore_wire(self) -> None:
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        os.dup2(self.wire_in, 0)
        os.dup2(self.wire_out, 1)

    def _read_once(self) -> bytes | None:
        """One read from the wire into the buffer: the bytes, b"" at EOF, None for nothing yet."""
        try:
            ready, _, _ = select.select([self.wire_in], [], [], 0.25)
        except (OSError, ValueError):
            ready = [self.wire_in]
        with self.read_lock:
            if self.paused or not ready:
                return None
            try:
                chunk = os.read(self.wire_in, 1 << 16)
            except BlockingIOError:
                return None
            except OSError:
                chunk = b""
            self.buffer += chunk
            return chunk

    def _write(self, data: bytes) -> None:
        with self.write_lock:
            view = memoryview(data)
            while view:
                view = view[os.write(self.wire_out, view):]

    async def _send_wire(self, obj: dict) -> None:
        await anyio.to_thread.run_sync(self._write, (json.dumps(obj) + "\n").encode("utf-8"))

    # --- inbound ---------------------------------------------------------------------

    async def _reader(self) -> None:
        while True:
            chunk = await anyio.to_thread.run_sync(self._read_once, abandon_on_cancel=True)
            if chunk is None:
                continue
            await self._lines(final=chunk == b"")
            if chunk == b"":
                self.eof = True
                await self.to_sdk.aclose()
                return

    async def _lines(self, final: bool = False) -> None:
        while True:
            with self.read_lock:
                at = self.buffer.find(b"\n")
                if at < 0:
                    if not final or not self.buffer.strip():
                        return
                    at = len(self.buffer)
                line = bytes(self.buffer[:at])
                del self.buffer[:at + 1]
            await self._route(line)

    async def _deliver(self, line: bytes, request: bool = False) -> None:
        """Hand one frame to the SDK. A request is counted in flight only once it validated,
        under its validated id: the SDK drops a frame that does not validate without an
        answer, and an id counted for it would stall every later swap."""
        try:
            # Decoded as the SDK's own transport decodes: invalid UTF-8 becomes U+FFFD.
            valid = types.jsonrpc_message_adapter.validate_json(line.decode("utf-8", errors="replace"),
                                                                 by_name=False)
            message: Any = SessionMessage(valid)
        except Exception as exc:  # noqa: BLE001: the SDK answers a malformed frame itself
            valid, message = None, exc
        counted = request and valid is not None and getattr(valid, "method", None) is not None \
            and getattr(valid, "id", None) is not None
        if counted:
            if valid.method == "initialize" and isinstance(getattr(valid, "params", None), dict):
                self.init_params, self.init_id = valid.params, valid.id
            key = _key(valid.id)
            if valid.method == LISTEN:
                self.listens[key] = valid.id
                if _wants_tools(getattr(valid, "params", None)):
                    self.listen_tools.add(key)
            else:
                self.inflight[key] = time.monotonic()
            # The SDK runs this when it settles the request without an answer (the client
            # cancelled it): it correlates a cancel by its own rules ("7" names request 7),
            # so the relay learns it from the SDK rather than matching ids itself.
            message.metadata = ServerMessageMetadata(on_request_unanswered=self._settled(key))
        try:
            await self.to_sdk.send(message)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            if counted:
                self._forget(_key(valid.id))

    def _settled(self, key: tuple):
        async def unanswered() -> None:
            self._forget(key)
        return unanswered

    def _forget(self, key: tuple) -> None:
        """A request that will get no further answer: no longer running or listening."""
        self.inflight.pop(key, None)
        self.listens.pop(key, None)
        self.listen_tools.discard(key)
        self._idle_retry()

    def _idle_retry(self) -> None:
        """The first idle moment after a swap was refused for running requests: try again now."""
        if self.retry_when_idle and not self.inflight and not self.swapping and not self.eof \
                and self.tg is not None and self._should_swap(idle=True):
            self.retry_when_idle = False
            self.swapping = True
            self.tg.start_soon(self._swap)

    async def _route(self, line: bytes, from_held: bool = False) -> None:
        if not line.strip():
            return
        try:
            obj = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            await self._deliver(line)
            return
        method, has_id = obj.get("method"), "id" in obj
        if isinstance(method, str) and has_id:
            if self.swapping and not from_held:
                self.held.append(line)
                return
            if not from_held and self._should_swap():
                self.swapping = True
                self.held.append(line)
                self.tg.start_soon(self._swap)
                return
            await self._deliver(line, request=True)
            return
        elif isinstance(method, str):
            if method == "notifications/initialized":
                self.initialized = True
            elif method == "notifications/cancelled":
                target = _key((obj.get("params") or {}).get("requestId"))
                kept = [h for h in self.held if _key(_id_of(h)) != target]
                if len(kept) != len(self.held):
                    self.held = kept
                    return
                self._forget(target)
        await self._deliver(line)

    # --- outbound --------------------------------------------------------------------

    async def _writer(self, stream) -> None:
        async with stream:
            async for item in stream:
                message = item.message
                mid = getattr(message, "id", None)
                answer = getattr(message, "method", None) is None and mid is not None
                if answer and mid == session_reload.SYNTHETIC_ID:
                    error = getattr(message, "error", None)
                    if error is not None:
                        session_reload.record(self.home, "resume-failed", to=configuration.VERSION,
                                              reason=session_reload.clean_reason(getattr(error, "message", "")))
                    continue
                if answer and self.init_id is not None and _key(mid) == _key(self.init_id):
                    result = getattr(message, "result", None)
                    if isinstance(result, dict):
                        self.negotiated = result.get("protocolVersion")
                text = message.model_dump_json(by_alias=True, exclude_unset=True)
                await anyio.to_thread.run_sync(self._write, (text + "\n").encode("utf-8"))
                if answer:
                    # Only once it is on the wire: a swap waits for an empty set, and an
                    # answer pulled but not yet written would die with the old process.
                    self._forget(_key(mid))

    # --- the swap --------------------------------------------------------------------

    def _identity(self):
        return code_freshness._identity(self.freshness.marker)

    def _should_swap(self, idle: bool = False) -> bool:
        if self.freshness.check() is None:
            return False
        if self.blocked is not None and self.blocked == self._identity():
            return False
        return idle or time.monotonic() >= self.retry_at

    async def _drained(self) -> bool:
        """Whether every request handed on was answered within DRAIN_SECONDS.

        Only validated requests are counted (`_deliver`), and each leaves the set when its
        answer is written or the SDK settles it unanswered; open listen streams are not
        counted (they never end by themselves). A request still running at the bound keeps
        the swap deferred, refused as transient and tried again the moment the set empties
        or at a later request: dropping its id would let the next attempt exec while it runs
        and lose its answer. Stops at once when the client left."""
        deadline = time.monotonic() + session_reload.DRAIN_SECONDS
        while self.inflight and not self.eof:
            if time.monotonic() > deadline:
                return False
            await anyio.sleep(0.05)
        return not self.eof

    async def _swap(self) -> None:
        try:
            outcome = await self._attempt()
        except Exception as exc:  # noqa: BLE001: a swap that breaks must still release the held requests
            outcome = (f"the swap failed ({type(exc).__name__})", False)
        if self.eof:
            # The client closed the session: nobody waits for the held requests.
            self.held.clear()
            self.swapping = False
            return
        if outcome is not None:
            reason, transient = outcome
            session_reload.REFUSAL = reason
            if transient:
                self.retry_at = time.monotonic() + RETRY_SECONDS
                self.retry_when_idle = reason == BUSY.format(seconds=int(session_reload.DRAIN_SECONDS))
            else:
                self.blocked = self._identity()
            stale = self.freshness.check() or {}
            key = (reason, self._identity())
            if key not in self.refusals_logged:
                self.refusals_logged.add(key)
                session_reload.record(self.home, "refused", **{"from": configuration.VERSION,
                                      "to": stale.get("installed"), "reason": session_reload.clean_reason(reason)})
        while self.held:
            await self._route(self.held.pop(0), from_held=True)
        self.swapping = False
        # The requests that refused this attempt may all have answered while it was refused.
        self._idle_retry()

    async def _attempt(self) -> tuple[str, bool] | None:
        """None when no swap is needed after all; (reason, transient) when it was refused.
        A swap that succeeds does not return: this process becomes the installed release."""
        stale = self.freshness.check()
        if stale is None:
            return None
        if stale["installed"] is None:
            return stale["reason"], False
        if session_reload.update_lock_held(self.home):
            return "an update of this workspace is in progress", True
        if not await self._drained():
            return BUSY.format(seconds=int(session_reload.DRAIN_SECONDS)), True
        if self.eof:
            return "the client closed the session", True
        before = self._identity()
        answer, why = await anyio.to_thread.run_sync(
            session_reload.preflight, sys.executable, self.script, self.argv)
        if why is None:
            why = session_reload.judge(answer, protocol=self.protocol)
        if why is not None:
            return why, False
        if self.eof:
            return "the client closed the session", True
        if self._identity() != before:
            return "the installed release changed during its start check", True
        if session_reload.update_lock_held(self.home):
            return "an update of this workspace is in progress", True
        tools = sorted(t.name for t in await self.server.list_tools())
        state = {"reload": session_reload.RELOAD_PROTOCOL, "from": configuration.VERSION,
                 "to": answer["version"], "initialize": self.init_params, "initialized": self.initialized,
                 "negotiated": self.negotiated, "tools_changed": tools != sorted(answer["tools"])}
        ended: list = []
        outcome = self._exec(state, ended)
        # Still here: the exec failed after the streams were ended on the wire. Those streams
        # are over for the client, so the handlers serving them are cancelled and write
        # nothing more on them; a client that listens again gets a new stream here.
        for sid in ended:
            self.listens.pop(_key(sid), None)
            self.listen_tools.discard(_key(sid))
            await self._deliver(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                            "params": {"requestId": sid}}).encode())
        return outcome

    def _end_listens(self, ended: list, tools_changed: bool = False) -> None:
        """End every open listen stream gracefully (SubscriptionsListenResult, spec 2026-07-28).

        Called under the write lock just before `execv`, so it is the last frame the old
        process writes for each stream: the SDK's writes wait on the lock and die with it.
        When the tool set changes, a stream that asked for tool-list changes first carries
        `notifications/tools/list_changed` stamped with its subscription id: the stream is
        the only channel a 2026-07-28 client hears changes on, and after the swap it holds
        none, so this is where it learns the list changed. Should the exec then fail, the
        event was early but harmless: the client refetches the list and finds it as it was."""
        for key, sid in list(self.listens.items()):
            meta = {SUBSCRIPTION_ID_META_KEY: sid}
            frames = []
            if tools_changed and key in self.listen_tools:
                frames.append({"jsonrpc": "2.0", "method": "notifications/tools/list_changed",
                               "params": {"_meta": meta}})
            result = types.SubscriptionsListenResult(_meta=meta)
            frames.append({"jsonrpc": "2.0", "id": sid,
                           "result": result.model_dump(by_alias=True, mode="json", exclude_none=True)})
            data = b"".join((json.dumps(frame) + "\n").encode("utf-8") for frame in frames)
            view = memoryview(data)
            while view:
                view = view[os.write(self.wire_out, view):]
            ended.append(sid)

    def _exec(self, state: dict, ended: list) -> tuple[str, bool]:
        with self.read_lock:
            self.paused = True
            try:
                with self.write_lock:
                    pending = b"".join(line + b"\n" for line in self.held) + bytes(self.buffer)
                    state["pending"] = session_reload.encode(pending)
                    try:
                        fd = session_reload.write_state(state)
                    except OSError as exc:
                        return f"the session state could not be handed over ({exc.strerror or type(exc).__name__})", False
                    try:
                        self._end_listens(ended, bool(state.get("tools_changed")))
                        self._restore_wire()
                        os.environ[session_reload.STATE_ENV] = str(fd)
                        if os.environ.get(TEST_EXEC_ENV) == "fail":
                            raise OSError(errno.EPERM, "refused by the test hook")
                        self.execv(sys.executable, [sys.executable, str(self.script), *self.argv])
                    except OSError as exc:
                        os.environ.pop(session_reload.STATE_ENV, None)
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        self._divert()
                        return (f"the installed release could not replace this process "
                                f"({exc.strerror or type(exc).__name__})"), False
                    return "the installed release could not replace this process", False
            finally:
                self.paused = False

    async def _resume(self) -> None:
        """In the process a swap started: replay the handshake, queue what was held."""
        state = self.resumed or {}
        if isinstance(state.get("initialize"), dict):
            self.init_params = state["initialize"]
            await self._deliver(json.dumps({"jsonrpc": "2.0", "id": session_reload.SYNTHETIC_ID,
                                            "method": "initialize", "params": state["initialize"]}).encode())
            if state.get("initialized"):
                self.initialized = True
                await self._deliver(b'{"jsonrpc": "2.0", "method": "notifications/initialized"}')
        self.negotiated = state.get("negotiated")
        with self.read_lock:
            self.buffer[:0] = session_reload.decode(state.get("pending"))
        session_reload.record(self.home, "swapped", **{"from": state.get("from"), "to": configuration.VERSION})
        # The bare notification is the pre-2026-07-28 form, for a client that made the
        # `initialize` handshake. A modern client heard the change on its listen stream,
        # stamped, just before the swap (`_end_listens`); unstamped it rides no stream.
        if state.get("tools_changed") and self.initialized \
                and self.negotiated not in MODERN_PROTOCOL_VERSIONS:
            await self._send_wire({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

    # --- serving ---------------------------------------------------------------------

    async def run(self) -> None:
        self._claim()
        lowlevel = self.server._lowlevel_server
        options = lowlevel.create_initialization_options(NotificationOptions(tools_changed=True))
        to_sdk, sdk_in = create_context_streams[SessionMessage | Exception](1024)
        sdk_out, from_sdk = create_context_streams[SessionMessage](0)
        self.to_sdk = to_sdk
        async with anyio.create_task_group() as tg:
            self.tg = tg
            tg.start_soon(self._writer, from_sdk)
            # The SDK reads before anything is fed: a hand-over can carry more held lines
            # than the inbound stream holds, and feeding them first would deadlock.
            tg.start_soon(lowlevel.run, sdk_in, sdk_out, options)
            if self.resumed is not None:
                await self._resume()
                await self._lines()
            tg.start_soon(self._reader)


def _key(value: Any) -> tuple:
    """A request id as the wire compares it: 7 and 7.0 are one id, "7" another."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return (type(value).__name__, value)


def _wants_tools(params: Any) -> bool:
    """Whether a listen request's filter asks for tool-list changes (`toolsListChanged`)."""
    if not isinstance(params, dict):
        return False
    wanted = params.get("notifications")
    return isinstance(wanted, dict) and wanted.get("toolsListChanged") is True


def _id_of(line: bytes) -> Any:
    try:
        return json.loads(line).get("id")
    except (ValueError, AttributeError):
        return None
