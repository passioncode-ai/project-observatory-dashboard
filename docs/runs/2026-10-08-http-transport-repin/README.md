# 2026-10-08 — the HTTP transport re-pinned to mcp 2.3.0

Dependabot proposed `mcp` 2.3.0 (#179). `docs/design/HTTP-TRANSPORT.md` pins the SDK the memory
service's HTTP transport was measured on, and `tests/test_http_transport_pin.py` refuses a lock
that moves it — so the move is a re-measurement, not a bump.

The measurement of 2026-10-04 (`../2026-10-04-http-transport-pin/experiment.py`) was run again,
unchanged, with `mcp` 2.3.0, `mcp-types` 2.3.0, `uvicorn` 0.54.0 and `starlette` 1.7.0
installed from the refreshed `requirements-full.lock`. Result: [results.json](results.json).

Compared with [the 2.2.0 results](../2026-10-04-http-transport-pin/results.json), key by key:
every check answers the same — a 2026-07-28 client without `initialize` or a session id, a
2025-11-25 handshake client, `auto` negotiation, the foreign Host and Origin refusals, the 4 MiB
body limit answered before the body is read, and no `Mcp-Session-Id` on a modern response. What
differs: the timestamps, the SDK versions, and the tool count (28 → 39), which is the engine's own
growth since 2026-10-04, not the SDK's.

The pin moves to `mcp` 2.3.0, `mcp-types` 2.3.0, `uvicorn` 0.54.0 in the design document and in
`tests/test_http_transport_pin.py`; `starlette` stays 1.7.0.
