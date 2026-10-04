# HTTP transport for the memory service: the pinned protocol

Decision record for PB-137 N-015, 2026-10-04. The memory service (N-016) will serve MCP over
HTTP on loopback beside stdio. This page pins the protocol revisions, the SDK and the
transport limits. The decision rests on a measurement, not on reading the specification
alone: [experiment.py](../runs/2026-10-04-http-transport-pin/experiment.py) serves the
engine's real MCP server with the pinned SDK, and its results are in
[results.json](../runs/2026-10-04-http-transport-pin/results.json).

## The decision

| Item | Pinned | Why |
|---|---|---|
| SDK | `mcp` 2.2.0, `mcp-types` 2.2.0, `uvicorn` 0.53.0, `starlette` 1.7.0, already in `requirements-full.lock` | No new dependency. The SDK serves both protocol eras from one app. |
| Protocol | **2026-07-28** (modern), plus the handshake revisions 2024-11-05 … 2025-11-25 for existing clients | A modern request is a self-contained POST with no `initialize` and no session. Clients that still handshake keep working. |
| App | `server.streamable_http_app(json_response=True, stateless_http=True, host="127.0.0.1", transport_security=…)` | Stateless: nothing on the transport carries authority or continuity. |
| Bind | `127.0.0.1` only | No public ingress. External access goes through Fabric's gateway (plan D-2, N-020). |
| Host / Origin | `enable_dns_rebinding_protection=True`; `allowed_hosts` = `127.0.0.1:<port>`, `localhost:<port>`; `allowed_origins` = the same two as `http://…` | DNS rebinding: a browser page cannot reach the service under another name. |
| Body limit | 4 MiB (the SDK default) | Answered from `Content-Length`, before the body is read. |
| Continuity | Explicit handles only: `workflowId` and `leaseId` (AGENT-MEMORY.md) | A transport session never identifies the executor. A handoff moves a lease, not a connection. |

## What the experiment measured

All checks below ran against the engine's own server in an empty synthetic workspace. The
workspace was created with `full init` in a temporary directory and removed afterwards.

| Check | Result |
|---|---|
| A 2026-07-28 client (`mcp.Client(url, mode="2026-07-28")`) | works; 28 tools; `observatory_overview` answers |
| A handshake client (`mode="legacy"`) | negotiates 2025-11-25 and works |
| `mode="auto"` | negotiates 2026-07-28 |
| A raw modern POST | 200, with no `Mcp-Session-Id` header in the answer |
| A foreign `Host` | 421 |
| A foreign `Origin` | 403 |
| `Content-Length` of 5 MiB | 413, before any body byte is sent |

**What a raw modern request needs**, measured from the server's own refusals:
- the `MCP-Protocol-Version: 2026-07-28` header;
- `_meta` keys `io.modelcontextprotocol/protocolVersion`, `io.modelcontextprotocol/clientCapabilities` and `io.modelcontextprotocol/clientInfo`;
- an `Mcp-Method` header equal to the body's `method`.

Without the capabilities key the server answers 400 (`-32602`). When `Mcp-Method` and the
body disagree it answers 400 (`-32020`). The SDK client sends all of this itself.

## What this decision leaves to N-016

- **Authentication.** Every HTTP request presents a bearer. The enforcement it plugs into
  exists since N-008 ([ACCESS-BINDING.md](ACCESS-BINDING.md#enforcement-n-008)):
  - the HTTP process calls `memory_access.serve_http()`, so nothing inherits the stdio
    default;
  - each request runs inside `memory_access.channel(memory_access.http_channel(bearer=…,
    fabric_projects=<X-Fabric-Projects>))`;
  - every tool then asks `memory_access`, which hashes the bearer, resolves the binding and
    refuses without one before any handler runs. stdio stays `local:stdio`.
- **Cancellation** is the client closing the response stream (2026-07-28). Long work stays a
  `fabric.job`, as today.
- **Rate limits** are not a transport feature of this SDK. N-016 sets one per binding and says
  so in its refusal envelope.

## Not done here

No HTTP endpoint ships in this change, and nothing listens. `tests/test_http_transport_pin.py`
pins the SDK properties this decision relies on, so an SDK upgrade that changes them fails a
test instead of changing the service silently.
