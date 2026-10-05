#!/usr/bin/env python3
"""The loopback HTTP memory service (PB-137 N-016).

The same MCP server as stdio (`mcp/server.py`: the same tools, schemas and answers),
served over Streamable HTTP on 127.0.0.1 with the transport pinned by N-015
(docs/design/HTTP-TRANSPORT.md): the 2026-07-28 protocol plus the handshake revisions,
stateless JSON, Host/Origin checked, 4 MiB bodies.

    project-observatory full memory-http [--port 47313]

What this file adds is the door in front of it, and the order of its checks is the
design:

1. **Host and Origin** first. A request naming another host (DNS rebinding) is refused
   421 and one from a foreign browser origin 403, before anything about it is read.
2. **The binding** next. The bearer in `Authorization` is hashed at once and resolved
   against `config/access-bindings.json` (access-bindings/1, N-007/N-008). No bearer, no
   binding, another audience, a revoked or expired binding, or an unreadable registry
   is a 401 with the typed refusal envelope — before the body is parsed, so no action
   of the request can have an effect. The bearer goes nowhere further: it is not
   logged, not stored, not passed upstream.
3. **A rate limit per binding** (`--rate`, default 120 requests a minute). Over it, 429
   with the same envelope shape and a `Retry-After`.
4. The request then runs inside `memory_access.channel(...)`, so every tool asks again
   for its own scope, project, effect and owner (N-008), and every memory tool outside
   the family is refused `local-only`.

The process calls `memory_access.serve_http()` first: nothing in it inherits the stdio
default, so a call that somehow ran without a channel is refused, not served as the
local agent. The service keeps no state of its own — no session, no journal beyond the
engine's `store/logs/access.jsonl` and `retrieval.jsonl` — so a restart loses nothing.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import threading
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

DEFAULT_PORT = 47313
DEFAULT_RATE = 120          # requests per minute per binding
HOST = "127.0.0.1"


class RateLimiter:
    """A token bucket per binding: `rate` requests a minute, bursts up to `rate`."""

    def __init__(self, rate_per_minute: int) -> None:
        self.capacity = float(rate_per_minute)
        self.refill = rate_per_minute / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> tuple[bool, int]:
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, at = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - at) * self.refill)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return True, 0
            self._buckets[key] = (tokens, now)
            return False, max(1, int((1.0 - tokens) / self.refill + 0.999))


async def _answer(send, status: int, body: dict, extra: list[tuple[bytes, bytes]] = ()) -> None:
    raw = json.dumps(body).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(raw)).encode()), *extra]})
    await send({"type": "http.response.body", "body": raw})


class BindingGate:
    """The ASGI door: Host/Origin, then the binding, then the rate, then the channel."""

    def __init__(self, app, *, port: int, rate: int, audience: str | None = None) -> None:
        import memory_access as MA
        self.MA = MA
        self.app = app
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        self.audience = audience
        self.limiter = RateLimiter(rate)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        if headers.get("host") not in self.hosts:
            return await _answer(send, 421, {"error": "misdirected request",
                                             "detail": "this service answers on loopback only"})
        origin = headers.get("origin")
        if origin is not None and origin not in self.origins:
            return await _answer(send, 403, {"error": "forbidden origin",
                                             "detail": "a browser page of another origin may "
                                                       "not call the memory service"})
        auth = headers.get("authorization", "")
        bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else None
        observed = self.MA.http_channel(bearer=bearer, audience=self.audience,
                                        fabric_projects=headers.get("x-fabric-projects"))
        del bearer, auth
        with self.MA.channel(observed):
            try:
                binding = self.MA.caller()
            except self.MA.Refused as exc:
                return await _answer(send, 401, exc.envelope,
                                     [(b"www-authenticate", b'Bearer realm="observatory-memory"')])
            allowed, wait = self.limiter.allow(binding.binding_id)
            if not allowed:
                return await _answer(send, 429, {
                    "error": "binding refused", "code": "rate-limited",
                    "detail": f"{binding.binding_id}: over {int(self.limiter.capacity)} requests "
                              f"a minute", "hint": f"retry after {wait} s", "degraded": []},
                    [(b"retry-after", str(wait).encode())])
            await self.app(scope, receive, send)


def build_app(*, port: int, rate: int = DEFAULT_RATE, audience: str | None = None):
    """The ASGI app: the engine's MCP server behind the binding gate."""
    import memory_access as MA
    import server as obs
    from mcp.server.transport_security import TransportSecuritySettings
    MA.serve_http()
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}"]
    inner = obs.server.streamable_http_app(
        json_response=True, stateless_http=True, host=HOST,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts,
            allowed_origins=[f"http://{h}" for h in hosts]))
    return BindingGate(inner, port=port, rate=rate, audience=audience)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="project-observatory full memory-http",
                                description="Serve agent memory over loopback HTTP to callers "
                                            "holding an access binding.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    p.add_argument("--rate", type=int, default=DEFAULT_RATE,
                   help=f"requests a minute per binding; default {DEFAULT_RATE}")
    return p


def main(argv: list[str]) -> int:
    a = parser().parse_args(argv)
    if not 1 <= a.port <= 65535 or a.rate < 1:
        raise SystemExit("memory-http: --port must be 1..65535 and --rate at least 1")
    import configuration
    configuration.validate_workspace(required=True)
    import memory_access as MA
    import paths
    import uvicorn
    app = build_app(port=a.port, rate=a.rate)
    state = MA.status(pathlib.Path(paths.CONFIG), pathlib.Path(paths.STATE))
    print(f"memory-http: http://{HOST}:{a.port}/mcp · audience {MA.own_audience()} · "
          f"bindings in force: {state.get('inForce', 0)} · {a.rate}/min per binding. "
          f"Issue one with `project-observatory full access-binding issue PRINCIPAL --project "
          f"PROJECT --token-file PATH`.", flush=True)
    uvicorn.run(app, host=HOST, port=a.port, log_level="warning", lifespan="on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
