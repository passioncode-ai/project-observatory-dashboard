# PB-137 N-015: HTTP transport pin, measured

The decision is in [design/HTTP-TRANSPORT.md](../../design/HTTP-TRANSPORT.md). This folder
holds its receipt:

- [experiment.py](experiment.py) serves the engine's real MCP server
  (`observatory/engine/mcp/server.py`) through `streamable_http_app` on 127.0.0.1. It runs
  in a synthetic workspace that `full init` creates in a temporary directory, and removes
  that directory afterwards. It reads no credential.
- [results.json](results.json) is the run of 2026-10-04 with mcp 2.2.0, uvicorn 0.53.0 and
  starlette 1.7.0:
  - a 2026-07-28 client, a legacy handshake client (2025-11-25) and `auto` (which picks
    2026-07-28) each list 28 tools and call `observatory_overview`;
  - a raw modern POST returns 200 with no session header;
  - a foreign Host gets 421;
  - a foreign Origin gets 403;
  - 5 MiB announced in `Content-Length` gets 413 before the body.

Reproduce from the repository root with the engine's environment:
`python docs/runs/2026-10-04-http-transport-pin/experiment.py out.json`.
