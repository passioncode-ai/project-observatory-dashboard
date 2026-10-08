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
| SDK | `mcp` 2.3.0, `mcp-types` 2.3.0, `uvicorn` 0.54.0, `starlette` 1.7.0 in `requirements-full.lock` (re-measured 2026-10-08 against the 2.2.0 pin of 2026-10-04: every property unchanged, [run record](../runs/2026-10-08-http-transport-repin/README.md)) | No new dependency. The SDK serves both protocol eras from one app. |
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
- **Rate limits** are not a transport feature of this SDK. N-016 sets one per binding (done: `--rate`, 429) and says
  so in its refusal envelope.

## The service (N-016)

`project-observatory full memory-http [--port 47313] [--rate 120]` serves the same MCP server
as stdio on `127.0.0.1` ([`mcp/http_service.py`](../../observatory/engine/mcp/http_service.py)).
It is a foreground process that the operator starts. Nothing installs it as a background job.

Its door checks in this order, before the body is read:

| Check | Refusal |
|---|---|
| `Host` is `127.0.0.1:<port>` or `localhost:<port>` | 421 |
| `Origin`, when sent, is one of those as `http://` | 403 |
| `Authorization: Bearer …` resolves to an access binding: present, known, this workspace's audience, unexpired, unrevoked, registry readable | 401 with the access-binding refusal envelope and `WWW-Authenticate` |
| the binding's request rate (`--rate` a minute, a token bucket per binding) | 429 with `Retry-After` |

The SDK's own Host/Origin check (above) is a second layer behind the door. Bodies over
4 MiB are 413. Then the request runs inside its own channel (`X-Fabric-Projects`
included), and every tool authorizes again (N-008).

- **The bearer.** It is hashed at the door and goes no further: it is not logged, not stored and not forwarded upstream.
- **Isolation.** The process calls `memory_access.serve_http()`, so nothing in it runs as the local agent.
- **No state of its own.** The service keeps no session and no journal. Decisions go to `store/logs/access.jsonl` and receipts to `retrieval.jsonl`, as on stdio, so a restart loses nothing.
- **`/mcp/`** answers 307 to `/mcp` on the same loopback address. The door has already confined the `Host`, so the redirect cannot point anywhere else.

Proved end to end by [`tests/test_memory_http.py`](../../observatory/engine/tests/test_memory_http.py):
a real uvicorn server and a real 2026-07-28 client, 11 tests. Five planted defects in the door
were each caught.

`tests/test_http_transport_pin.py` pins the SDK properties this decision relies on, so an SDK
upgrade that changes them fails a test instead of changing the service silently.
