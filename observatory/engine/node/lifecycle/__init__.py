"""`node.lifecycle`: the node wakes, stops and restarts enrolled desktop apps and background services for agents
(OBS-79; the port plan is docs/design/LIFECYCLE.md).

`broker.py` is the platform-neutral half (step L1): the closed registry of enrolled targets, the authority that
says which caller may use which tool on which target, durable operations with idempotency receipts, the effect
journal, crash circuits, the always_on restart budget, and the idle and takeover rules. It acts on a target only
through an adapter (`broker.Adapter`); the macOS adapter is step L2, the MCP tools step L3, the supervisor thread
step L4.
"""
