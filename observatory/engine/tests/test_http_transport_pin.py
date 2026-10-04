#!/usr/bin/env python3
"""The SDK properties the HTTP memory service relies on (PB-137 N-015).

docs/design/HTTP-TRANSPORT.md pins mcp 2.2.0: the 2026-07-28 per-request protocol beside
the handshake revisions, stateless JSON serving, Host/Origin checks and a 4 MiB body limit.
An SDK upgrade that changes any of these must fail here, not change the service quietly.
The measurement itself is docs/runs/2026-10-04-http-transport-pin/experiment.py.
"""
from __future__ import annotations

import inspect
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class SdkPin(unittest.TestCase):
    def setUp(self):
        try:
            import mcp                                                                 # noqa: F401
        except ImportError:
            self.skipTest("mcp is in the [full] extra; the base install has no MCP server")

    def test_the_lock_pins_the_sdk_the_decision_measured(self):
        lock = (ROOT / "requirements-full.lock").read_text(encoding="utf-8")
        for line in ("mcp==2.2.0", "mcp-types==2.2.0", "uvicorn==0.53.0", "starlette==1.7.0"):
            self.assertIn(line, lock)

    def test_both_protocol_eras_are_served(self):
        from mcp_types import version
        self.assertIn("2026-07-28", version.MODERN_PROTOCOL_VERSIONS)
        self.assertIn("2025-11-25", version.HANDSHAKE_PROTOCOL_VERSIONS)

    def test_the_app_takes_the_pinned_settings(self):
        from mcp.server import MCPServer
        params = inspect.signature(MCPServer.streamable_http_app).parameters
        for name in ("json_response", "stateless_http", "max_request_body_size",
                     "transport_security", "host"):
            self.assertIn(name, params)
        self.assertEqual(params["host"].default, "127.0.0.1", "loopback is the default bind")

    def test_dns_rebinding_protection_and_the_body_limit(self):
        from mcp.server import transport_security as ts
        s = ts.TransportSecuritySettings()
        self.assertTrue(s.enable_dns_rebinding_protection)
        self.assertEqual(ts.DEFAULT_MAX_REQUEST_BODY_SIZE, 4 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
