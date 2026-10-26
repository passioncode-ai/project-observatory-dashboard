"""`node.lifecycle`: the node wakes, stops and restarts enrolled desktop apps and background services for agents
(OBS-79; the port plan is docs/design/LIFECYCLE.md).

`broker.py` is the platform-neutral half (step L1): the closed registry of enrolled targets, the authority that
says which caller may use which tool on which target, durable operations with idempotency receipts, the effect
journal, crash circuits, the always_on restart budget, and the idle and takeover rules. It acts on a target only
through an adapter (`broker.Adapter`). `macos.py` is the macOS adapter (step L2): signatures checked against a
designated requirement built from team and bundle id, trusted installed places, Launch Services through the Swift
helper `native_app.swift`, launchd jobs, and the product readiness probes; `macos.platform_adapter` answers
`unsupported_platform` on every other system. The MCP tools are step L3, the supervisor thread step L4, and
`importer.py` step L5: the operator's import of another broker's enrollments and grants (`full lifecycle import`).
"""
