#!/usr/bin/env python3
"""PB-137 N-015: what the pinned SDK (mcp 2.2.0) does over loopback HTTP, measured.

Serves the engine's real MCP server (`observatory/engine/mcp/server.py`) with
`MCPServer.streamable_http_app` on 127.0.0.1 in a synthetic, empty workspace, then asks:

- does a 2026-07-28 client work without `initialize` and without a session id;
- does a legacy client (initialize handshake, 2025-11-25) still work;
- does `auto` negotiate, and to what;
- is a foreign Host or Origin refused (DNS rebinding);
- is an oversized body refused before parsing;
- does a modern response carry no `Mcp-Session-Id`.

Run from the repository root with the engine's environment:
    python docs/runs/2026-10-04-http-transport-pin/experiment.py OUT.json
Nothing here reads a credential or the operator's workspace: OBSERVATORY_HOME is a
temporary directory removed at the end.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[3]
ENGINE = ROOT / "observatory" / "engine"
TMP = pathlib.Path(tempfile.mkdtemp(prefix="observatory-n015-")).resolve()
HOME = TMP / "workspace"                    # `full init` wants a home that does not exist yet
os.environ["OBSERVATORY_HOME"] = str(HOME)
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "mcp"))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def raw_post(url: str, body: bytes, headers: dict) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def init_workspace() -> str:
    """`full init` into the temporary home, so the tools answer from an empty estate."""
    import subprocess
    r = subprocess.run([sys.executable, str(ENGINE / "observatory.py"), "init"],
                       capture_output=True, text=True, env=dict(os.environ), timeout=300)
    return f"exit {r.returncode}"


def main(out_path: str) -> int:
    import importlib.metadata as md
    initialized = init_workspace()
    import uvicorn
    from mcp import Client
    from mcp.server.transport_security import TransportSecuritySettings
    import server as obs                      # the engine's MCP server, unchanged

    port = free_port()
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}"]
    app = obs.server.streamable_http_app(
        json_response=True, stateless_http=True, host="127.0.0.1",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts,
            allowed_origins=[f"http://127.0.0.1:{port}", f"http://localhost:{port}"]))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}/mcp"
    results: dict = {"sdk": {"mcp": md.version("mcp"), "mcp_types": md.version("mcp-types"),
                             "uvicorn": md.version("uvicorn"), "starlette": md.version("starlette")},
                     "server": {"json_response": True, "stateless_http": True, "bind": "127.0.0.1"},
                     "workspace": {"init": initialized, "estate": "empty, synthetic"},
                     "checks": {}}
    C = results["checks"]

    async def with_client(mode: str) -> dict:
        try:
            async with Client(url, mode=mode) as c:
                tools = await c.list_tools()
                names = sorted(t.name for t in tools.tools)
                call = await c.call_tool("observatory_overview", {})
                text = " ".join(getattr(x, "text", "") for x in (call.content or []))[:300]
                return {"ok": True, "negotiated": c.protocol_version, "tools": len(names),
                        "has_search": "observatory_search" in names,
                        "overview_is_error": bool(getattr(call, "is_error", False)),
                        "overview_text": text}
        except Exception as exc:                                                      # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    C["modern_2026_07_28"] = asyncio.run(with_client("2026-07-28"))
    C["legacy_handshake"] = asyncio.run(with_client("legacy"))
    C["auto"] = asyncio.run(with_client("auto"))

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {
        "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                  "io.modelcontextprotocol/clientCapabilities": {},
                  "io.modelcontextprotocol/clientInfo": {"name": "n015-raw", "version": "0"}}}}).encode()
    # The 2026-07-28 envelope: the protocol version in a header AND in _meta, the client's
    # capabilities in _meta, and an Mcp-Method header that matches the body's method.
    base = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"}
    st, hd, raw = raw_post(url, body, base)
    C["modern_raw_post"] = {"status": st, "session_id_header": any(k.lower() == "mcp-session-id" for k in hd),
                            "body": raw.decode("utf-8", "replace")[:300]}
    st, _, _ = raw_post(url, body, {**base, "Host": "evil.example"})
    C["foreign_host_refused"] = {"status": st}
    st, _, _ = raw_post(url, body, {**base, "Origin": "http://evil.example"})
    C["foreign_origin_refused"] = {"status": st}
    # The size check reads Content-Length and answers before the body: send the headers
    # of a 5 MiB request and read the status without sending the body at all.
    with socket.create_connection(("127.0.0.1", port), timeout=20) as sock:
        head = (f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\n"
                f"MCP-Protocol-Version: 2026-07-28\r\nContent-Length: {5 * 1024 * 1024}\r\n\r\n")
        sock.sendall(head.encode("ascii"))
        reply = sock.recv(256).decode("latin-1", "replace")
    status = int(reply.split()[1]) if reply.startswith("HTTP/") else None
    C["oversized_body_refused"] = {"status": status, "limit_bytes": 4 * 1024 * 1024,
                                   "answered_before_body": True}

    srv.should_exit = True
    t.join(timeout=10)
    pathlib.Path(out_path).write_text(json.dumps(results, indent=1) + "\n")
    print(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    try:
        rc = main(sys.argv[1])
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(rc)
